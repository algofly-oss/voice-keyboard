// Wi-Fi and the connection to the backend's /v1/keyboard socket.
//
// Protocol (the desktop client's, see desktop/client.go):
//   client -> {"type":"hello","client":"<name>","machine":"…","platform":"…","version":"…"}
//   server -> {"type":"ready","device":7,"credential":"…","selected":true}
//   server -> {"type":"selected","selected":false}
//   server -> {"type":"segment","text":"…"} / {"type":"key","key":"enter","state":"press"}
// The first connection uses the account's install token; the credential in
// "ready" replaces it, so a new install token later does not unpair the board.
//
// One task (net_task) starts and stops the socket; the socket's own event
// handler only reports to it, as the client cannot be stopped from there.
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_timer.h"
#include "esp_tls.h"
#include "esp_websocket_client.h"
#include "esp_wifi.h"
#include "sdkconfig.h"
#include "vk.h"

static const char *TAG = "net";

#define MESSAGE_MAX (32 * 1024)  // a long dictation arrives as one segment
#define CLOSE_BAD_CREDENTIAL 4401
#define REJECTED_RETRY_MS (60 * 1000)
// Roaming: with a weak signal, move to an access point of the same network
// that is clearly stronger (mesh systems and extenders share one name).
// Heartbeat: a proxy in between (Cloudflare) can keep our side of a dead
// connection open after the server restarts; no pong in time, reconnect.
#define PING_EVERY_MS (25 * 1000)
#define PONG_WITHIN_MS (15 * 1000)
#define ROAM_CHECK_MS (30 * 1000)
#define ROAM_WEAK_RSSI (-67)
#define ROAM_MARGIN_DB 8

typedef enum {
    EV_WIFI_UP,
    EV_WIFI_DOWN,
    EV_APPLY_WIFI,
    EV_APPLY_SERVER,
    EV_CREDENTIAL,   // the server issued a credential; reconnect with it
    EV_REJECTED,     // install token or credential no longer valid
    EV_TLS_FAILED,
    EV_ROAM_CHECK,
    EV_BLE,          // Bluetooth connected or lost: tell the server
    EV_PING,         // heartbeat timer
} net_event_t;

static QueueHandle_t events;
static esp_websocket_client_handle_t ws;
static char *ws_uri, *ws_headers;
static esp_timer_handle_t reconnect_timer, roam_timer, ping_timer;
static int64_t ping_sent_us;  // an unanswered ping, or 0
static volatile bool wifi_up, scanning;
static int wifi_failures;
static bool use_ca = true;  // with a saved CA, try it first; fall back to public CAs
static char machine[32];
static volatile bool ready;  // the server accepted us ("ready"); status messages may go out

// Received message being reassembled from frames.
static char *message;
static size_t message_len;

static void post(net_event_t ev)
{
    if (events) {  // Bluetooth starts before the network
        xQueueSend(events, &ev, 0);
    }
}

void net_ble_changed(void)
{
    post(EV_BLE);
}

static const char *chip_name(void);

// Error and warning lines for the server (GET /api/client-errors). The log
// hook only copies them here; net_task sends them, so sending cannot recurse.
#define LOG_SLOTS 8
#define LOG_LINE_MAX 200
static char log_ring[LOG_SLOTS][LOG_LINE_MAX];
static int log_head, log_count;
static portMUX_TYPE log_lock = portMUX_INITIALIZER_UNLOCKED;

void net_report_log(const char *line)
{
    char clean[LOG_LINE_MAX];
    size_t n = 0;
    for (const char *p = line; *p && n < sizeof clean - 1; p++) {
        if (*p == '\x1b') {  // drop colour codes
            while (*p && *p != 'm') p++;
            if (!*p) break;
            continue;
        }
        if (*p != '\n' && *p != '\r') clean[n++] = *p;
    }
    clean[n] = '\0';
    taskENTER_CRITICAL(&log_lock);
    int last = (log_head + log_count - 1 + LOG_SLOTS) % LOG_SLOTS;
    // The same message again (a retry loop): skip it; the timestamp differs, so compare after it.
    const char *a = strchr(clean, ')'), *b = log_count ? strchr(log_ring[last], ')') : NULL;
    if (!(a && b && strcmp(a, b) == 0)) {
        if (log_count == LOG_SLOTS) {  // full: drop the oldest
            log_head = (log_head + 1) % LOG_SLOTS;
            log_count--;
        }
        strlcpy(log_ring[(log_head + log_count) % LOG_SLOTS], clean, LOG_LINE_MAX);
        log_count++;
    }
    taskEXIT_CRITICAL(&log_lock);
}

static void send_log_reports(void)
{
    while (ws && ready && esp_websocket_client_is_connected(ws)) {
        char line[LOG_LINE_MAX];
        taskENTER_CRITICAL(&log_lock);
        bool any = log_count > 0;
        if (any) {
            memcpy(line, log_ring[log_head], sizeof line);
            log_head = (log_head + 1) % LOG_SLOTS;
            log_count--;
        }
        taskEXIT_CRITICAL(&log_lock);
        if (!any) {
            return;
        }
        char message[LOG_LINE_MAX + 48];
        snprintf(message, sizeof message, "%s %s: %s", esp_app_get_description()->version, chip_name(), line);
        cJSON *m = cJSON_CreateObject();
        cJSON_AddStringToObject(m, "type", "log");
        cJSON_AddStringToObject(m, "level", "error");
        cJSON_AddStringToObject(m, "message", message);
        char *text = cJSON_PrintUnformatted(m);
        cJSON_Delete(m);
        if (text) {
            esp_websocket_client_send_text(ws, text, strlen(text), pdMS_TO_TICKS(2000));
            free(text);
        }
    }
}

static void send_ble_status(void)
{
    if (!ws || !ready || !esp_websocket_client_is_connected(ws)) {
        return;
    }
    char text[64];
    int n = snprintf(text, sizeof text, "{\"type\":\"status\",\"bluetooth\":\"%s\"}",
                     status_ble_connected() ? "connected" : "waiting");
    esp_websocket_client_send_text(ws, text, n, pdMS_TO_TICKS(2000));
}

const char *vk_machine_id(void)
{
    if (!machine[0]) {
        uint8_t mac[6];
        esp_read_mac(mac, ESP_MAC_WIFI_STA);
        snprintf(machine, sizeof machine, "esp32-%02x%02x%02x%02x%02x%02x",
                 mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    }
    return machine;
}

static const char *chip_name(void)
{
#if CONFIG_IDF_TARGET_ESP32C3
    return "ESP32-C3";
#else
    return "ESP32";
#endif
}

// ---- Wi-Fi ----

static const char *wifi_reason(uint8_t reason)
{
    switch (reason) {
    case WIFI_REASON_NO_AP_FOUND:
        return "network not found";
    case WIFI_REASON_AUTH_FAIL:
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_HANDSHAKE_TIMEOUT:
    case WIFI_REASON_MIC_FAILURE:
        return "wrong password";
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD:
        return "network security not supported";
    case WIFI_REASON_ASSOC_LEAVE:
        return "";
    default:
        return NULL;
    }
}

static void wifi_connect(void)
{
    if (!vk_cfg.ssid[0] || scanning) {
        return;
    }
    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, vk_cfg.ssid, sizeof wc.sta.ssid);
    strlcpy((char *)wc.sta.password, vk_cfg.pass, sizeof wc.sta.password);
    wc.sta.threshold.authmode = WIFI_AUTH_OPEN;  // open networks too
    wc.sta.pmf_cfg.capable = true;
    wc.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    wc.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    esp_wifi_set_config(WIFI_IF_STA, &wc);
    vk_status_t *s = status_begin();
    if (strcmp(s->wifi, "failed") != 0) {
        s->wifi = "connecting";
    }
    status_end();
    esp_wifi_connect();
}

static void reconnect_cb(void *arg)
{
    wifi_connect();
}

static void roam_cb(void *arg)
{
    post(EV_ROAM_CHECK);
}

static void ping_cb(void *arg)
{
    post(EV_PING);
}

// Called from net_task. wifi_connect() always joins the strongest access point
// (all-channel scan, sorted by signal), so moving is a disconnect and rejoin.
static void roam_check(void)
{
    wifi_ap_record_t cur;
    if (!wifi_up || scanning || esp_wifi_sta_get_ap_info(&cur) != ESP_OK) {
        return;
    }
    vk_status_t *s = status_begin();
    s->rssi = cur.rssi;
    status_end();  // keeps the page's signal reading current
    if (cur.rssi >= ROAM_WEAK_RSSI) {
        return;
    }
    wifi_scan_config_t sc = {.ssid = (uint8_t *)vk_cfg.ssid};
    uint16_t n = 0;
    wifi_ap_record_t *aps = NULL;
    scanning = true;
    if (esp_wifi_scan_start(&sc, true) == ESP_OK && esp_wifi_scan_get_ap_num(&n) == ESP_OK && n) {
        aps = calloc(n, sizeof *aps);
        if (!aps || esp_wifi_scan_get_ap_records(&n, aps) != ESP_OK) {
            n = 0;
        }
    } else {
        esp_wifi_clear_ap_list();
    }
    scanning = false;
    const wifi_ap_record_t *best = NULL;
    for (int i = 0; i < n; i++) {
        if (memcmp(aps[i].bssid, cur.bssid, 6) != 0 && (!best || aps[i].rssi > best->rssi)) {
            best = &aps[i];
        }
    }
    if (best && best->rssi >= cur.rssi + ROAM_MARGIN_DB) {
        ESP_LOGI(TAG, "roaming: %d dBm here, %d dBm at " MACSTR " (channel %d)", cur.rssi, best->rssi,
                 MAC2STR(best->bssid), best->primary);
        esp_wifi_disconnect();  // on_wifi reconnects, to the strongest
    }
    free(aps);
}

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        wifi_event_sta_disconnected_t *d = data;
        bool was_up = wifi_up;
        wifi_up = false;
        if (was_up) {
            post(EV_WIFI_DOWN);
        }
        const char *why = wifi_reason(d->reason);
        if ((d->reason == WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY ||
             d->reason == WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD) && !vk_cfg.pass[0]) {
            why = "this network needs a password";
        }
        vk_status_t *s = status_begin();
        s->ip[0] = '\0';
        if (vk_cfg.ssid[0]) {
            if (why && !*why) {
                s->wifi_error[0] = '\0';  // we disconnected on purpose
            } else if (why) {
                strlcpy(s->wifi_error, why, sizeof s->wifi_error);
            } else {
                snprintf(s->wifi_error, sizeof s->wifi_error, "disconnected (reason %d)", d->reason);
            }
            s->wifi = ++wifi_failures >= 3 && s->wifi_error[0] ? "failed" : "connecting";
        } else {
            s->wifi = "off";
        }
        status_end();
        if (vk_cfg.ssid[0] && !scanning) {
            esp_timer_stop(reconnect_timer);
            esp_timer_start_once(reconnect_timer, (wifi_failures < 5 ? 2000 : 10000) * 1000LL);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        wifi_ap_record_t ap;
        wifi_up = true;
        wifi_failures = 0;
        vk_status_t *s = status_begin();
        s->wifi = "connected";
        s->wifi_error[0] = '\0';
        snprintf(s->ip, sizeof s->ip, IPSTR, IP2STR(&e->ip_info.ip));
        s->rssi = esp_wifi_sta_get_ap_info(&ap) == ESP_OK ? ap.rssi : 0;
        status_end();
        ESP_LOGI(TAG, "Wi-Fi connected, IP %s", s->ip);
        post(EV_WIFI_UP);
    }
}

cJSON *net_scan(void)
{
    cJSON *list = cJSON_CreateArray();
    // A scan fails while the station is trying to connect, so pause that.
    scanning = true;
    if (!wifi_up) {
        esp_timer_stop(reconnect_timer);
        esp_wifi_disconnect();
    }
    wifi_scan_config_t sc = {0};
    esp_err_t err = esp_wifi_scan_start(&sc, true);
    uint16_t n = 0;
    wifi_ap_record_t *aps = NULL;
    if (err == ESP_OK && esp_wifi_scan_get_ap_num(&n) == ESP_OK && n) {
        n = n > 30 ? 30 : n;
        aps = calloc(n, sizeof *aps);
        if (!aps || esp_wifi_scan_get_ap_records(&n, aps) != ESP_OK) {
            n = 0;
        }
    } else if (err != ESP_OK) {
        ESP_LOGW(TAG, "scan failed: %s", esp_err_to_name(err));
    }
    // Strongest first; one entry per network name.
    for (int i = 0; i < n; i++) {
        const char *ssid = (const char *)aps[i].ssid;
        bool seen = !ssid[0];
        for (int j = 0; j < i && !seen; j++) {
            seen = strcmp(ssid, (const char *)aps[j].ssid) == 0;
        }
        if (seen) {
            continue;
        }
        cJSON *ap = cJSON_CreateObject();
        cJSON_AddStringToObject(ap, "ssid", ssid);
        cJSON_AddNumberToObject(ap, "rssi", aps[i].rssi);
        cJSON_AddBoolToObject(ap, "secure", aps[i].authmode != WIFI_AUTH_OPEN);
        cJSON_AddItemToArray(list, ap);
    }
    free(aps);
    scanning = false;
    if (!wifi_up) {
        wifi_connect();
    }
    return list;
}

// ---- Backend socket ----

static void send_hello(void)
{
    cJSON *hello = cJSON_CreateObject();
    cJSON_AddStringToObject(hello, "type", "hello");
    cJSON_AddStringToObject(hello, "client", vk_cfg.name);
    cJSON_AddStringToObject(hello, "machine", vk_machine_id());
    cJSON_AddStringToObject(hello, "platform", chip_name());
    cJSON_AddStringToObject(hello, "version", esp_app_get_description()->version);
    char *text = cJSON_PrintUnformatted(hello);
    cJSON_Delete(hello);
    esp_websocket_client_send_text(ws, text, strlen(text), pdMS_TO_TICKS(5000));
    free(text);
}

static void handle_message(const char *text)
{
    cJSON *m = cJSON_Parse(text);
    const char *type = cJSON_GetStringValue(cJSON_GetObjectItem(m, "type"));
    if (!type) {
        cJSON_Delete(m);
        return;
    }
    if (strcmp(type, "segment") == 0) {
        const char *t = cJSON_GetStringValue(cJSON_GetObjectItem(m, "text"));
        if (t) {
            ble_type(t);
        }
    } else if (strcmp(type, "key") == 0) {
        const char *key = cJSON_GetStringValue(cJSON_GetObjectItem(m, "key"));
        const char *state = cJSON_GetStringValue(cJSON_GetObjectItem(m, "state"));
        if (key) {
            ble_key(key, state ? state : "press");
        }
    } else if (strcmp(type, "pointer") == 0) {
        const char *action = cJSON_GetStringValue(cJSON_GetObjectItem(m, "action"));
        if (action) {
            ble_pointer(action, (int)cJSON_GetNumberValue(cJSON_GetObjectItem(m, "dx")),
                        (int)cJSON_GetNumberValue(cJSON_GetObjectItem(m, "dy")),
                        cJSON_GetStringValue(cJSON_GetObjectItem(m, "button")));
        }
    } else if (strcmp(type, "pong") == 0) {
        ping_sent_us = 0;
    } else if (strcmp(type, "ready") == 0 || strcmp(type, "selected") == 0) {
        vk_status_t *s = status_begin();
        s->selected = cJSON_IsTrue(cJSON_GetObjectItem(m, "selected"));
        if (strcmp(type, "ready") == 0) {
            ready = true;
            if (!use_ca && vk_cfg.ca) {
                // The server's certificate is publicly trusted (the private CA failed):
                // forget the CA, so later connections skip the failing first attempt.
                vk_cfg.ca = NULL;  // not freed: the client config may still point at it
                config_save();
                ESP_LOGI(TAG, "public certificate works; the private CA is no longer used");
            }
            s->server = "connected";
            s->server_error[0] = '\0';
            s->device_id = (int)cJSON_GetNumberValue(cJSON_GetObjectItem(m, "device"));
        }
        status_end();
        if (strcmp(type, "ready") == 0) {
            post(EV_BLE);
        }
        const char *credential = cJSON_GetStringValue(cJSON_GetObjectItem(m, "credential"));
        if (credential && credential[0] && strcmp(credential, vk_cfg.token) != 0 &&
            strlen(credential) <= VK_TOKEN_MAX) {
            strlcpy(vk_cfg.token, credential, sizeof vk_cfg.token);
            config_save();
            ESP_LOGI(TAG, "paired; using the device credential from now on");
            post(EV_CREDENTIAL);
        }
    }
    cJSON_Delete(m);
}

static void set_server_state(const char *state, const char *error)
{
    vk_status_t *s = status_begin();
    s->server = state;
    if (error) {
        strlcpy(s->server_error, error, sizeof s->server_error);
    }
    if (strcmp(state, "connected") != 0) {
        s->selected = false;
    }
    status_end();
}

static void on_ws(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_websocket_event_data_t *e = data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected to %s", vk_cfg.server);
        send_hello();
        break;
    case WEBSOCKET_EVENT_DATA:
        if (e->op_code == 0x08) {  // close; the payload starts with the code
            int code = e->data_len >= 2 ? ((uint8_t)e->data_ptr[0] << 8 | (uint8_t)e->data_ptr[1]) : 0;
            ESP_LOGW(TAG, "server closed the connection (%d)", code);
            if (code == CLOSE_BAD_CREDENTIAL) {
                post(EV_REJECTED);
            }
            break;
        }
        if (e->op_code != 0x01 && e->op_code != 0x00) {
            break;  // ping, pong, binary
        }
        if (e->payload_offset == 0) {
            message_len = 0;
        }
        if (e->payload_len > MESSAGE_MAX) {
            break;
        }
        if (!message) {
            message = malloc(MESSAGE_MAX + 1);
        }
        if (message && e->payload_offset + e->data_len <= MESSAGE_MAX) {
            memcpy(message + e->payload_offset, e->data_ptr, e->data_len);
            message_len = e->payload_offset + e->data_len;
            if (message_len >= (size_t)e->payload_len) {
                message[message_len] = '\0';
                handle_message(message);
            }
        }
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
    case WEBSOCKET_EVENT_ERROR: {
        ready = false;
        char why[64] = "could not reach the server";
        bool tls = false;
        if (e && e->error_handle.esp_tls_cert_verify_flags) {
            strlcpy(why, "server certificate not trusted", sizeof why);
            tls = true;
        } else if (e && e->error_handle.esp_tls_last_esp_err == ESP_ERR_MBEDTLS_SSL_HANDSHAKE_FAILED) {
            strlcpy(why, "secure connection failed", sizeof why);
            tls = true;
        } else if (e && e->error_handle.esp_ws_handshake_status_code) {
            snprintf(why, sizeof why, "server answered HTTP %d", e->error_handle.esp_ws_handshake_status_code);
        }
        if (id == WEBSOCKET_EVENT_ERROR) {
            ESP_LOGW(TAG, "connection error: %s", why);
            set_server_state("error", why);
            if (tls) {
                post(EV_TLS_FAILED);
            }
        } else {
            set_server_state("connecting", NULL);
        }
        break;
    }
    default:
        break;
    }
}

static void ws_stop(void)
{
    ready = false;
    ping_sent_us = 0;
    if (ws) {
        esp_websocket_client_stop(ws);
        esp_websocket_client_destroy(ws);
        ws = NULL;
    }
}

static void ws_start(void)
{
    ws_stop();
    if (!config_complete()) {
        set_server_state("off", "");
        return;
    }
    // https://host:port → wss://host:port/v1/keyboard
    const char *server = vk_cfg.server;
    bool secure = strncmp(server, "https://", 8) == 0;
    const char *rest = strstr(server, "://");
    rest = rest ? rest + 3 : server;
    size_t len = strlen(rest);
    while (len && rest[len - 1] == '/') {
        len--;
    }
    free(ws_uri);
    asprintf(&ws_uri, "%s://%.*s/v1/keyboard", secure ? "wss" : "ws", (int)len, rest);
    free(ws_headers);
    asprintf(&ws_headers, "Authorization: Bearer %s\r\n", vk_cfg.token);

    esp_websocket_client_config_t c = {
        .uri = ws_uri,
        .headers = ws_headers,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = 15000,
        .ping_interval_sec = 20,
        .pingpong_timeout_sec = 45,  // a dead link (sleep, network change) within ~a minute
        .buffer_size = 2048,
        .task_stack = 6144,
    };
    if (secure) {
        if (vk_cfg.ca && use_ca) {
            c.cert_pem = vk_cfg.ca;
        } else {
            c.crt_bundle_attach = esp_crt_bundle_attach;
        }
    }
    ws = esp_websocket_client_init(&c);
    if (!ws) {
        set_server_state("error", "out of memory");
        return;
    }
    esp_websocket_register_events(ws, WEBSOCKET_EVENT_ANY, on_ws, NULL);
    set_server_state("connecting", "");
    ESP_LOGI(TAG, "connecting to %s (%s)", ws_uri,
             !secure ? "no TLS" : vk_cfg.ca && use_ca ? "private CA" : "public CAs");
    esp_websocket_client_start(ws);
}

static void net_task(void *arg)
{
    bool rejected = false;
    for (;;) {
        net_event_t ev;
        if (!xQueueReceive(events, &ev, rejected ? pdMS_TO_TICKS(REJECTED_RETRY_MS) : portMAX_DELAY)) {
            rejected = false;  // retry: the user may have re-added the board
            if (wifi_up) {
                ws_start();
            }
            continue;
        }
        switch (ev) {
        case EV_WIFI_UP:
            if (!ws && !rejected) {
                ws_start();
            }
            break;
        case EV_WIFI_DOWN:
            ws_stop();
            set_server_state(config_complete() ? "connecting" : "off", NULL);
            break;
        case EV_APPLY_WIFI: {
            ws_stop();
            wifi_failures = 0;
            esp_timer_stop(reconnect_timer);
            vk_status_t *st = status_begin();  // a failure was about the old settings
            st->wifi = vk_cfg.ssid[0] ? "connecting" : "off";
            st->wifi_error[0] = '\0';
            status_end();
            if (wifi_up) {
                esp_wifi_disconnect();  // reconnects with the new settings on the event
            } else if (vk_cfg.ssid[0]) {
                wifi_connect();
            } else {
                vk_status_t *s = status_begin();
                s->wifi = "off";
                status_end();
            }
            rejected = false;
            break;
        }
        case EV_APPLY_SERVER:
            rejected = false;
            use_ca = true;
            if (wifi_up) {
                ws_start();
            } else {
                ws_stop();
            }
            break;
        case EV_CREDENTIAL:
            if (wifi_up) {
                ws_start();
            }
            break;
        case EV_REJECTED:
            ws_stop();
            rejected = true;
            set_server_state("rejected", "this board was removed or its setup token was replaced; set it up again");
            break;
        case EV_ROAM_CHECK:
            roam_check();
            break;
        case EV_BLE:  // also sent right after "ready": a good moment for the error reports too
            send_ble_status();
            send_log_reports();
            break;
        case EV_PING:
            if (!ws || !ready) {
                break;
            }
            send_log_reports();
            if (ping_sent_us && esp_timer_get_time() - ping_sent_us > PONG_WITHIN_MS * 1000LL) {
                ESP_LOGW(TAG, "no answer from the server; reconnecting");
                set_server_state("connecting", "no answer from the server");
                ws_start();
            } else if (!ping_sent_us) {
                ping_sent_us = esp_timer_get_time();
                esp_websocket_client_send_text(ws, "{\"type\":\"ping\"}", 15, pdMS_TO_TICKS(2000));
            }
            break;
        case EV_TLS_FAILED:
            if (vk_cfg.ca) {
                use_ca = !use_ca;
                if (wifi_up) {
                    ws_start();
                }
            }
            break;
        }
    }
}

void net_apply(bool wifi_changed, bool server_changed)
{
    if (wifi_changed) {
        post(EV_APPLY_WIFI);
    }
    if (server_changed || wifi_changed) {
        post(EV_APPLY_SERVER);
    }
}

void net_start(void)
{
    events = xQueueCreate(8, sizeof(net_event_t));
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    char host[32];
    snprintf(host, sizeof host, "vkeyboard-%s", vk_machine_id() + 12);  // last 3 MAC bytes
    esp_netif_set_hostname(netif, host);
    wifi_init_config_t wic = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&wic));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));  // our own NVS keys are the source of truth
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi, NULL));
    esp_timer_create_args_t t = {.callback = reconnect_cb, .name = "wifi_retry"};
    ESP_ERROR_CHECK(esp_timer_create(&t, &reconnect_timer));
    esp_timer_create_args_t r = {.callback = roam_cb, .name = "wifi_roam"};
    ESP_ERROR_CHECK(esp_timer_create(&r, &roam_timer));
    ESP_ERROR_CHECK(esp_timer_start_periodic(roam_timer, ROAM_CHECK_MS * 1000LL));
    esp_timer_create_args_t hb = {.callback = ping_cb, .name = "ws_ping"};
    ESP_ERROR_CHECK(esp_timer_create(&hb, &ping_timer));
    // Checked twice per interval, so a missing pong is noticed within ~40 s.
    ESP_ERROR_CHECK(esp_timer_start_periodic(ping_timer, PING_EVERY_MS / 2 * 1000LL));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    xTaskCreate(net_task, "net", 6144, NULL, 5, NULL);
    ESP_ERROR_CHECK(esp_wifi_start());  // also when unconfigured, so the page can scan
}
