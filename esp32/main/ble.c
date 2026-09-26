// Bluetooth LE keyboard (HID over GATT) and the typing queue.
//
// Pairing is "Just Works" with bonding: the user adds the board once in the
// target computer's Bluetooth settings, and it reconnects by itself after.
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "esp_bt.h"
#include "esp_hidd.h"
#include "esp_log.h"
#include "esp_nimble_hci.h"
#include "host/ble_hs.h"
#include "host/ble_store.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "vk.h"

static const char *TAG = "ble";

#define KEY_DELAY_MS 10       // between reports; the host polls every 7.5–15 ms
#define REPORT_ID 1
#define APPEARANCE_KEYBOARD 0x03C1

void ble_store_config_init(void);

// Standard boot-compatible keyboard: modifiers, reserved, six keys; LEDs out.
static const uint8_t report_map[] = {
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,  // Generic Desktop, Keyboard, Collection (Application)
    0x85, REPORT_ID,                     //   Report ID
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,  //   Keyboard usages E0–E7 (modifiers)
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,  // 8 × 1 bit, Input
    0x95, 0x01, 0x75, 0x08, 0x81, 0x03,  //   reserved byte
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,  // 5 LEDs, Output
    0x95, 0x01, 0x75, 0x03, 0x91, 0x03,  //   LED padding
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x73,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x73, 0x81, 0x00,  // 6 key codes, Input (Array)
    0xC0,
};

static esp_hid_raw_report_map_t report_maps[] = {{.data = report_map, .len = sizeof report_map}};

static esp_hid_device_config_t hid_config = {
    .vendor_id = 0x16C0,  // pid.codes shared test VID/PID for keyboards
    .product_id = 0x05DF,
    .version = 0x0100,
    .device_name = "Voice Keyboard",
    .manufacturer_name = "Voice Keyboard",
    .serial_number = "1",
    .report_maps = report_maps,
    .report_maps_len = 1,
};

static esp_hidd_dev_t *hid;
static volatile bool connected;
static uint8_t own_addr_type;
static char name[VK_NAME_MAX + 1] = "Voice Keyboard";

// ---- Advertising ----

static int gap_event(struct ble_gap_event *event, void *arg);

static void advertise(void)
{
    if (connected || !ble_hs_synced()) {
        return;
    }
    ble_gap_adv_stop();
    // The name goes in the scan response: 31 bytes are not enough for both.
    struct ble_hs_adv_fields fields = {0};
    ble_uuid16_t hid_uuid = BLE_UUID16_INIT(0x1812);
    fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
    fields.appearance = APPEARANCE_KEYBOARD;
    fields.appearance_is_present = 1;
    fields.uuids16 = &hid_uuid;
    fields.num_uuids16 = 1;
    fields.uuids16_is_complete = 1;
    int rc = ble_gap_adv_set_fields(&fields);
    struct ble_hs_adv_fields rsp = {0};
    rsp.name = (uint8_t *)name;
    rsp.name_len = strlen(name);
    rsp.name_is_complete = 1;
    if (!rc) rc = ble_gap_adv_rsp_set_fields(&rsp);
    struct ble_gap_adv_params params = {
        .conn_mode = BLE_GAP_CONN_MODE_UND,
        .disc_mode = BLE_GAP_DISC_MODE_GEN,
        .itvl_min = BLE_GAP_ADV_ITVL_MS(30),
        .itvl_max = BLE_GAP_ADV_ITVL_MS(50),
    };
    if (!rc) rc = ble_gap_adv_start(own_addr_type, NULL, BLE_HS_FOREVER, &params, gap_event, NULL);
    if (rc && rc != BLE_HS_EALREADY) {
        ESP_LOGE(TAG, "advertising failed: %d", rc);
        return;
    }
    vk_status_t *s = status_begin();
    s->ble = "advertising";
    s->ble_peer[0] = '\0';
    status_end();
}

static int gap_event(struct ble_gap_event *event, void *arg)
{
    struct ble_gap_conn_desc desc;
    switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
        if (event->connect.status != 0) {
            ESP_LOGW(TAG, "connection failed: %d", event->connect.status);
            advertise();
        } else if (ble_gap_conn_find(event->connect.conn_handle, &desc) == 0) {
            // A computer paired before: encrypt right away with the stored keys,
            // so its first reads of the keyboard's attributes succeed.
            struct ble_store_key_sec key = {.peer_addr = desc.peer_id_addr};
            struct ble_store_value_sec bond;
            bool bonded = ble_store_read_peer_sec(&key, &bond) == 0;
            ESP_LOGI(TAG, "connection from %s computer", bonded ? "a paired" : "a new");
            if (bonded) {
                ble_gap_security_initiate(event->connect.conn_handle);
            }
        }
        return 0;
    case BLE_GAP_EVENT_DISCONNECT:
        ESP_LOGI(TAG, "disconnected, reason 0x%x", event->disconnect.reason);
        return 0;
    case BLE_GAP_EVENT_ENC_CHANGE:
        ESP_LOGI(TAG, "encryption %s (%d)", event->enc_change.status == 0 ? "on" : "failed", event->enc_change.status);
        if (event->enc_change.status == 0 && ble_gap_conn_find(event->enc_change.conn_handle, &desc) == 0) {
            vk_status_t *s = status_begin();
            s->ble = "connected";
            const uint8_t *a = desc.peer_id_addr.val;
            snprintf(s->ble_peer, sizeof s->ble_peer, "%02X:%02X:%02X:%02X:%02X:%02X",
                     a[5], a[4], a[3], a[2], a[1], a[0]);
            status_end();
        }
        return 0;
    case BLE_GAP_EVENT_REPEAT_PAIRING:
        // The computer forgot the board and pairs again: drop the old bond.
        if (ble_gap_conn_find(event->repeat_pairing.conn_handle, &desc) == 0) {
            ble_store_util_delete_peer(&desc.peer_id_addr);
        }
        return BLE_GAP_REPEAT_PAIRING_RETRY;
    case BLE_GAP_EVENT_ADV_COMPLETE:
        advertise();
        return 0;
    }
    return 0;
}

static void hid_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    switch ((esp_hidd_event_t)id) {
    case ESP_HIDD_START_EVENT:
        advertise();
        break;
    case ESP_HIDD_CONNECT_EVENT: {
        connected = true;
        vk_status_t *s = status_begin();
        s->ble = "connected";
        status_end();
        ESP_LOGI(TAG, "computer connected");
        break;
    }
    case ESP_HIDD_DISCONNECT_EVENT:
        connected = false;
        ESP_LOGI(TAG, "computer disconnected");
        advertise();
        break;
    default:
        break;
    }
}

static void host_task(void *param)
{
    nimble_port_run();
    nimble_port_freertos_deinit();
}

// ---- Typing ----

typedef struct {
    char *text;   // text to type, or NULL for a key
    int8_t key;   // HID usage of a named key
    char state;   // 'p'ress, 'd'own, 'u'p
} job_t;

static QueueHandle_t jobs;
static uint8_t held;  // a key kept down by "down"/"hold" until "up"

static void send_report(uint8_t mod, uint8_t code)
{
    uint8_t report[8] = {mod, 0, code, 0, 0, 0, 0, 0};
    for (int attempt = 0; attempt < 20; attempt++) {
        if (!connected) {
            return;
        }
        if (esp_hidd_dev_input_set(hid, 0, REPORT_ID, report, sizeof report) == ESP_OK) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(20));  // the stack's buffers are full; let them drain
    }
    vTaskDelay(pdMS_TO_TICKS(KEY_DELAY_MS));
}

static void stroke(uint8_t mod, uint8_t code)
{
    send_report(mod, code);
    send_report(0, held);
}

// Decodes one UTF-8 character; returns its length (1 for invalid bytes).
static int utf8_next(const char *s, uint32_t *cp)
{
    const uint8_t *u = (const uint8_t *)s;
    int len = u[0] < 0x80 ? 1 : (u[0] >> 5) == 6 ? 2 : (u[0] >> 4) == 14 ? 3 : (u[0] >> 3) == 30 ? 4 : 0;
    if (!len) {
        *cp = 0xFFFD;
        return 1;
    }
    *cp = len == 1 ? u[0] : u[0] & (0x7F >> len);
    for (int i = 1; i < len; i++) {
        if ((u[i] & 0xC0) != 0x80) {
            *cp = 0xFFFD;
            return i;
        }
        *cp = *cp << 6 | (u[i] & 0x3F);
    }
    return len;
}

static void type_text(const char *text)
{
    int typed = 0, skipped = 0;
    for (const char *p = text; *p;) {
        uint32_t cp;
        p += utf8_next(p, &cp);
        vk_stroke_t strokes[3];
        int n = keymap_lookup(cp, strokes);
        if (!n) {
            skipped++;
            continue;
        }
        for (int i = 0; i < n; i++) {
            stroke(strokes[i].mod, strokes[i].code);
        }
        typed++;
    }
    // Only counts are logged: dictated text can contain passwords.
    if (skipped) {
        ESP_LOGW(TAG, "typed %d characters; %d have no key on a US keyboard", typed, skipped);
    } else {
        ESP_LOGI(TAG, "typed %d characters", typed);
    }
}

static void typing_task(void *arg)
{
    job_t job;
    for (;;) {
        xQueueReceive(jobs, &job, portMAX_DELAY);
        if (!connected) {
            ESP_LOGW(TAG, "not typed: no computer is connected over Bluetooth");
            free(job.text);
            continue;
        }
        if (job.text) {
            type_text(job.text);
            free(job.text);
        } else if (job.state == 'p') {
            stroke(0, job.key);
        } else if (job.state == 'd') {
            held = job.key;
            send_report(0, held);
        } else if (held == job.key) {
            held = 0;
            send_report(0, 0);
        }
    }
}

void ble_type(const char *text)
{
    job_t job = {.text = strdup(text)};
    if (job.text && xQueueSend(jobs, &job, 0) != pdTRUE) {
        free(job.text);
        ESP_LOGW(TAG, "typing queue full; text dropped");
    }
}

void ble_key(const char *key, const char *state)
{
    int code = keymap_named(key);
    if (code < 0) {
        return;
    }
    job_t job = {.key = code, .state = strcmp(state, "up") == 0 ? 'u'
                                    : (strcmp(state, "down") == 0 || strcmp(state, "hold") == 0) ? 'd' : 'p'};
    xQueueSend(jobs, &job, 0);
}

// ---- Setup ----

static void (*hid_sync)(void);  // esp_hidd's: it posts ESP_HIDD_START_EVENT

static void on_sync(void)
{
    ble_hs_util_ensure_addr(0);
    ble_hs_id_infer_auto(0, &own_addr_type);
    hid_sync();
}

void ble_set_name(const char *new_name)
{
    strlcpy(name, new_name && new_name[0] ? new_name : "Voice Keyboard", sizeof name);
    ble_svc_gap_device_name_set(name);
    advertise();  // takes effect for computers that have not paired yet
}

void ble_forget(void)
{
    ble_store_clear();
    struct ble_gap_conn_desc desc;
    for (uint16_t handle = 0; handle < 8; handle++) {
        if (ble_gap_conn_find(handle, &desc) == 0) {
            ble_gap_terminate(handle, BLE_ERR_REM_USER_CONN_TERM);
        }
    }
    advertise();
}

void ble_start(void)
{
    jobs = xQueueCreate(64, sizeof(job_t));
    xTaskCreate(typing_task, "typing", 4096, NULL, 6, NULL);
    if (vk_cfg.name[0]) {
        strlcpy(name, vk_cfg.name, sizeof name);
    }

    ESP_ERROR_CHECK(esp_bt_controller_mem_release(ESP_BT_MODE_CLASSIC_BT));
    esp_bt_controller_config_t bt = BT_CONTROLLER_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_bt_controller_init(&bt));
    ESP_ERROR_CHECK(esp_bt_controller_enable(ESP_BT_MODE_BLE));
    ESP_ERROR_CHECK(esp_nimble_init());

    ble_hs_cfg.sm_io_cap = BLE_SM_IO_CAP_NO_IO;
    ble_hs_cfg.sm_bonding = 1;
    ble_hs_cfg.sm_mitm = 0;
    ble_hs_cfg.sm_sc = 1;
    ble_hs_cfg.sm_our_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;
    ble_hs_cfg.sm_their_key_dist = BLE_SM_PAIR_KEY_DIST_ENC | BLE_SM_PAIR_KEY_DIST_ID;

    hid_config.device_name = name;
    ESP_ERROR_CHECK(esp_hidd_dev_init(&hid_config, ESP_HID_TRANSPORT_BLE, hid_event, &hid));
    // Resolve our address before esp_hidd's sync callback starts advertising.
    hid_sync = ble_hs_cfg.sync_cb;
    ble_hs_cfg.sync_cb = on_sync;
    ble_svc_gap_device_name_set(name);
    ble_svc_gap_device_appearance_set(APPEARANCE_KEYBOARD);
    ble_store_config_init();
    ble_hs_cfg.store_status_cb = ble_store_util_status_rr;
    ESP_ERROR_CHECK(esp_nimble_enable(host_task));
}
