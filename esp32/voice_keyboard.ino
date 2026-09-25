#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>
#include <ESPmDNS.h>
#include <Preferences.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <esp_random.h>
#include <mbedtls/sha256.h>

#include "firmware_config.h"
#include "web_app.h"

// Defaults used until they are changed from the web settings page. Real values
// come from the ignored firmware_config.h (generated from esp32/.env).
#ifndef VK_DEFAULT_WIFI_NAME
#define VK_DEFAULT_WIFI_NAME ""
#endif
#ifndef VK_DEFAULT_WIFI_PASSWORD
#define VK_DEFAULT_WIFI_PASSWORD ""
#endif
#ifndef VK_DEFAULT_SERVER_URL
#define VK_DEFAULT_SERVER_URL ""
#endif
#ifndef VK_DEFAULT_API_KEY
#define VK_DEFAULT_API_KEY "change-me"
#endif
#ifndef VK_DEFAULT_WEB_PASSWORD
#define VK_DEFAULT_WEB_PASSWORD "change-me"
#endif

constexpr char DEFAULT_WIFI_NAME[] = VK_DEFAULT_WIFI_NAME;
constexpr char DEFAULT_WIFI_PASSWORD[] = VK_DEFAULT_WIFI_PASSWORD;
constexpr char DEFAULT_HOSTNAME[] = "voice-keyboard";
constexpr char DEFAULT_BLE_NAME[] = "ESP32 Voice Keyboard";
constexpr char DEFAULT_SERVER_URL[] = VK_DEFAULT_SERVER_URL;
constexpr char DEFAULT_WHISPER_MODEL[] = "deepdml/faster-whisper-large-v3-turbo-ct2";
// Must match API_KEY in backend/.env.
constexpr char DEFAULT_API_KEY[] = VK_DEFAULT_API_KEY;
// Password for the web app; any string, changeable in Settings > Device.
constexpr char DEFAULT_WEB_PASSWORD[] = VK_DEFAULT_WEB_PASSWORD;
constexpr char SETUP_AP_NAME[] = "VoiceKeyboard-Setup";
constexpr char SETUP_AP_PASSWORD[] = "voicekeyboard";

// The browser streams audio straight to the transcription server; the ESP32
// only serves the page, stores these settings and types the result.
struct Settings {
  String wifiName, wifiPassword, hostname, bleName;
  String serverUrl, model, language, prompt, apiKey;
  String webPassword;
  String afterText;  // "none", "space" or "enter"
  bool liveTyping;   // Type each phrase while speaking instead of at the end.
  int keyDelayMs;
  int maxSeconds;
};

struct TypingJob {
  char *text;
  bool applyAfterText;
};

Settings settings;
Preferences preferences;
bool setupAccessPoint = false;
String accessPointsSeen;  // JSON array of the router's access points found at boot
String sessionSalt, sessionToken, sessionCookie;  // See "Login" below.
uint32_t failedLogins = 0;

NimBLEServer *bleServer = nullptr;
NimBLEHIDDevice *hid = nullptr;
NimBLECharacteristic *keyboardInput = nullptr;
volatile bool bleConnected = false;
// Turned off from the web app so the iPad shows its on-screen keyboard again.
volatile bool bleEnabled = true;
QueueHandle_t typingQueue;
SemaphoreHandle_t keyLock;
// Flow control for key reports: a slot is taken per notification and returned
// when the stack reports it sent. Without this, reports queued faster than
// connection events are silently dropped (lost spaces, merged words).
constexpr UBaseType_t TX_SLOTS = 3;
SemaphoreHandle_t txSlots;
volatile bool cancelTyping = false;
volatile bool typing = false;

// Hold-to-repeat keys (backspace, arrows). The browser renews the hold every
// ~150 ms, so a lost "release" can never leave a key repeating.
constexpr uint32_t HOLD_TIMEOUT_MS = 500, REPEAT_DELAY_MS = 400, REPEAT_INTERVAL_MS = 45;
volatile uint8_t heldKey = 0;  // HID key code, 0 when nothing is held
volatile uint32_t holdDeadline = 0, nextRepeatAt = 0;

// Standard keyboard: modifier byte, reserved byte, six key codes, LED output.
const uint8_t keyboardReportDescriptor[] = {
  0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, 0x01,
  0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00,
  0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
  0x95, 0x01, 0x75, 0x08, 0x81, 0x01, 0x95, 0x05,
  0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05,
  0x91, 0x02, 0x95, 0x01, 0x75, 0x03, 0x91, 0x01,
  0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
  0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
  0xC0
};

struct KeyboardReport {
  uint8_t modifiers;
  uint8_t reserved;
  uint8_t keys[6];
} __attribute__((packed));

// ---------- Settings ----------

void loadSettings() {
  preferences.begin("voicekb", true);
  settings.wifiName = preferences.getString("wifiName", DEFAULT_WIFI_NAME);
  settings.wifiPassword = preferences.getString("wifiPass", DEFAULT_WIFI_PASSWORD);
  settings.hostname = preferences.getString("hostname", DEFAULT_HOSTNAME);
  settings.bleName = preferences.getString("bleName", DEFAULT_BLE_NAME);
  settings.serverUrl = preferences.getString("server", DEFAULT_SERVER_URL);
  settings.model = preferences.getString("model", DEFAULT_WHISPER_MODEL);
  settings.language = preferences.getString("language", "en");
  settings.prompt = preferences.getString("prompt", "");
  settings.apiKey = preferences.getString("apiKey", DEFAULT_API_KEY);
  settings.webPassword = preferences.getString("webPass", DEFAULT_WEB_PASSWORD);
  settings.afterText = preferences.getString("afterText", "none");
  settings.liveTyping = preferences.getBool("live", true);
  settings.keyDelayMs = preferences.getInt("keyDelay", 0);  // 0 = Ultra fast
  settings.maxSeconds = preferences.getInt("maxSec", 600);
  preferences.end();
}

void saveSettings() {
  preferences.begin("voicekb", false);
  preferences.putString("wifiName", settings.wifiName);
  preferences.putString("wifiPass", settings.wifiPassword);
  preferences.putString("hostname", settings.hostname);
  preferences.putString("bleName", settings.bleName);
  preferences.putString("server", settings.serverUrl);
  preferences.putString("model", settings.model);
  preferences.putString("language", settings.language);
  preferences.putString("prompt", settings.prompt);
  preferences.putString("apiKey", settings.apiKey);
  preferences.putString("webPass", settings.webPassword);
  preferences.putString("afterText", settings.afterText);
  preferences.putBool("live", settings.liveTyping);
  preferences.putInt("keyDelay", settings.keyDelayMs);
  preferences.putInt("maxSec", settings.maxSeconds);
  preferences.end();
}

// ---------- Bluetooth keyboard ----------

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &info) override {
    bleConnected = true;
    // 15-30 ms interval (units of 1.25 ms; within Apple's accessory limits):
    // fast enough for typing while leaving Wi-Fi some airtime on the shared radio.
    server->updateConnParams(info.getConnHandle(), 12, 24, 0, 400);
  }
  void onDisconnect(NimBLEServer *, NimBLEConnInfo &, int) override {
    bleConnected = false;
    heldKey = 0;
    while (uxSemaphoreGetCount(txSlots) < TX_SLOTS) xSemaphoreGive(txSlots);
    if (bleEnabled) NimBLEDevice::startAdvertising();
  }
};

class ReportCallbacks : public NimBLECharacteristicCallbacks {
  void onStatus(NimBLECharacteristic *, NimBLEConnInfo &, int) override { xSemaphoreGive(txSlots); }
};

// Queues one report, waiting for a free slot and retrying if the stack is out
// of buffers, so no key press or release is lost.
void sendReport(const KeyboardReport &report) {
  for (int attempt = 0; attempt < 50 && bleConnected; attempt++) {
    // A missing status callback must not stall typing forever.
    xSemaphoreTake(txSlots, pdMS_TO_TICKS(250));
    if (keyboardInput->notify((const uint8_t *)&report, sizeof(report))) return;
    xSemaphoreGive(txSlots);
    delay(10);
  }
}

// Both the typing task and held-key repeat send keys, so reports are
// serialized to keep press/release pairs intact.
void sendLocked(const KeyboardReport &report) {
  xSemaphoreTake(keyLock, portMAX_DELAY);
  sendReport(report);
  xSemaphoreGive(keyLock);
}

void sendKey(uint8_t modifiers, uint8_t key) {
  if (!bleConnected) return;
  xSemaphoreTake(keyLock, portMAX_DELAY);
  sendReport(KeyboardReport{modifiers, 0, {key, 0, 0, 0, 0, 0}});
  delay(settings.keyDelayMs);
  sendReport(KeyboardReport{});
  delay(settings.keyDelayMs);
  xSemaphoreGive(keyLock);
}

// US layout. Returns false for characters the keyboard cannot type.
bool asciiToKey(char c, uint8_t &key, bool &shift) {
  static const char unshifted[] = "-=[]\\;'`,./";
  static const char shifted[] = "_+{}|:\"~<>?";
  static const uint8_t symbolKeys[] = {45, 46, 47, 48, 49, 51, 52, 53, 54, 55, 56};
  static const char shiftedDigits[] = "!@#$%^&*()";
  shift = false;
  if (c >= 'a' && c <= 'z') { key = 4 + c - 'a'; return true; }
  if (c >= 'A' && c <= 'Z') { key = 4 + c - 'A'; shift = true; return true; }
  if (c >= '1' && c <= '9') { key = 30 + c - '1'; return true; }
  if (c == '0') { key = 39; return true; }
  if (c == ' ') { key = 44; return true; }
  if (c == '\n') { key = 40; return true; }
  if (c == '\t') { key = 43; return true; }
  if (const char *p = strchr(shiftedDigits, c)) { key = 30 + (p - shiftedDigits); shift = true; return true; }
  if (const char *p = strchr(unshifted, c)) { key = symbolKeys[p - unshifted]; return true; }
  if (const char *p = strchr(shifted, c)) { key = symbolKeys[p - shifted]; shift = true; return true; }
  return false;
}

// Whisper returns UTF-8; fold common punctuation and accented Latin letters to ASCII.
String toTypeableAscii(const String &input) {
  static const char latin1[] = "AAAAAAACEEEEIIIIDNOOOOOxOUUUUYTsaaaaaaaceeeeiiiidnooooo/ouuuuyty";
  String out;
  out.reserve(input.length());
  const uint8_t *s = (const uint8_t *)input.c_str();
  while (*s) {
    uint32_t cp;
    int len;
    if (*s < 0x80) { cp = *s; len = 1; }
    else if ((*s & 0xE0) == 0xC0) { cp = *s & 0x1F; len = 2; }
    else if ((*s & 0xF0) == 0xE0) { cp = *s & 0x0F; len = 3; }
    else { cp = *s & 0x07; len = 4; }
    for (int i = 1; i < len; i++) {
      if ((s[i] & 0xC0) != 0x80) { len = i; cp = '?'; break; }
      cp = (cp << 6) | (s[i] & 0x3F);
    }
    s += len;
    if (cp == '\r') continue;
    if (cp < 0x80) out += (char)cp;
    else if (cp >= 0xC0 && cp <= 0xFF) out += latin1[cp - 0xC0];
    else if (cp == 0x2018 || cp == 0x2019 || cp == 0x201B) out += '\'';
    else if (cp == 0x201C || cp == 0x201D) out += '"';
    else if (cp == 0x2013 || cp == 0x2014) out += '-';
    else if (cp == 0x2026) out += "...";
    else if (cp == 0x00A0) out += ' ';
  }
  return out;
}

// "Ultra fast" (key delay 0) rolls from one key straight to the next, like a
// fast typist: pressing "b" in the report after "a" also releases "a", so each
// character is one report instead of a press and a release. A release is still
// sent before a repeated key, which would otherwise read as one long press.
void typeText(const String &text) {
  String ascii = toTypeableAscii(text);
  bool rollover = settings.keyDelayMs == 0;
  uint8_t held = 0;
  for (size_t i = 0; i < ascii.length() && !cancelTyping && bleConnected; i++) {
    uint8_t key;
    bool shift;
    if (!asciiToKey(ascii[i], key, shift)) continue;
    if (!rollover) {
      sendKey(shift ? 0x02 : 0x00, key);
      continue;
    }
    if (key == held) sendLocked(KeyboardReport{});
    sendLocked(KeyboardReport{(uint8_t)(shift ? 0x02 : 0x00), 0, {key, 0, 0, 0, 0, 0}});
    held = key;
  }
  if (held && bleConnected) sendLocked(KeyboardReport{});
}

void typingTask(void *) {
  TypingJob job;
  for (;;) {
    if (xQueueReceive(typingQueue, &job, portMAX_DELAY) != pdTRUE) continue;
    typing = true;
    cancelTyping = false;
    typeText(job.text);
    if (job.applyAfterText && !cancelTyping && settings.afterText == "space") typeText(" ");
    if (job.applyAfterText && !cancelTyping && settings.afterText == "enter") typeText("\n");
    free(job.text);
    typing = false;
  }
}

bool queueTyping(const String &text, bool applyAfterText) {
  TypingJob job{strdup(text.c_str()), applyAfterText};
  if (!job.text) return false;
  if (xQueueSend(typingQueue, &job, 0) != pdTRUE) {
    free(job.text);
    return false;
  }
  return true;
}

void stopTyping() {
  cancelTyping = true;
  TypingJob job;
  while (xQueueReceive(typingQueue, &job, 0) == pdTRUE) free(job.text);
}

// Runs from loop(): first press immediately, then auto-repeat while held.
void serviceHeldKey() {
  uint8_t key = heldKey;
  if (!key) return;
  uint32_t now = millis();
  if ((int32_t)(now - holdDeadline) > 0) {
    heldKey = 0;
    return;
  }
  if ((int32_t)(now - nextRepeatAt) < 0) return;
  bool first = nextRepeatAt == 0;
  sendKey(0, key);
  nextRepeatAt = millis() + (first ? REPEAT_DELAY_MS : REPEAT_INTERVAL_MS);
  if (!nextRepeatAt) nextRepeatAt = 1;
}

// Disconnecting makes the host drop back to its on-screen keyboard; enabling
// advertises again so the bonded host reconnects by itself.
void setBluetoothEnabled(bool enabled) {
  bleEnabled = enabled;
  if (enabled) {
    NimBLEDevice::startAdvertising();
    return;
  }
  stopTyping();
  heldKey = 0;
  NimBLEDevice::stopAdvertising();
  for (uint16_t handle : bleServer->getPeerDevices()) bleServer->disconnect(handle);
}

// NimBLE instead of the core's Bluedroid stack: it leaves roughly 100 KB more
// heap for Wi-Fi and the web server.
void setupBluetooth() {
  NimBLEDevice::init(settings.bleName.c_str());
  NimBLEDevice::setSecurityAuth(true, false, true);
  NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);
  NimBLEServer *server = bleServer = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());
  server->advertiseOnDisconnect(false);  // ServerCallbacks decides, based on bleEnabled.
  hid = new NimBLEHIDDevice(server);
  hid->setManufacturer("ESP32");
  hid->setPnp(0x02, 0x303A, 0x0002, 0x0100);
  hid->setHidInfo(0x00, 0x01);
  hid->setReportMap((uint8_t *)keyboardReportDescriptor, sizeof(keyboardReportDescriptor));
  keyboardInput = hid->getInputReport(1);
  keyboardInput->setCallbacks(new ReportCallbacks());
  hid->getOutputReport(1);  // Caps/Num Lock LEDs; accepted and ignored.
  hid->setBatteryLevel(100);
  server->start();

  NimBLEAdvertising *advertising = NimBLEDevice::getAdvertising();
  advertising->setAppearance(0x03C1);  // Generic HID keyboard.
  advertising->addServiceUUID(hid->getHidService()->getUUID());
  // The name does not fit in the 31-byte advertisement beside the UUID.
  NimBLEAdvertisementData scanResponse;
  scanResponse.setName(settings.bleName.c_str());
  advertising->setScanResponseData(scanResponse);
  advertising->enableScanResponse(true);
  NimBLEDevice::startAdvertising();
}

// ---------- Web server ----------
// ESPAsyncWebServer: it serves many connections at once (a synchronous server
// stalls for seconds on each idle connection a browser or proxy opens) and has
// no fixed limit on total header size (esp_http_server's 1024 bytes is exceeded
// behind Cloudflare with cookies). Handlers run in the network task and must
// not block.
AsyncWebServer web(80);
volatile uint32_t restartAt = 0;  // restarts from loop() once the reply is sent

String jsonEscape(const String &value) {
  String out;
  for (char c : value) {
    if (c == '"' || c == '\\') { out += '\\'; out += c; }
    else if (c == '\n') out += "\\n";
    else if ((uint8_t)c < 0x20) out += ' ';
    else out += c;
  }
  return out;
}

// Form fields (POST) or query parameters, already URL-decoded.
bool formValue(AsyncWebServerRequest *r, const char *key, String &out) {
  const AsyncWebParameter *p = r->hasParam(key, true) ? r->getParam(key, true) : r->getParam(key);
  if (!p) return false;
  out = p->value();
  out.trim();
  return true;
}

String query(AsyncWebServerRequest *r, const char *key) {
  const AsyncWebParameter *p = r->getParam(key);
  return p ? p->value() : String();
}

void sendText(AsyncWebServerRequest *r, int status, const String &text, const String &cookie = "") {
  AsyncWebServerResponse *res = r->beginResponse(status, "text/plain; charset=utf-8", text);
  res->addHeader("Cache-Control", "no-store");
  if (cookie.length()) res->addHeader("Set-Cookie", cookie);
  r->send(res);
}

void sendJson(AsyncWebServerRequest *r, const String &json) {
  AsyncWebServerResponse *res = r->beginResponse(200, "application/json", json);
  res->addHeader("Cache-Control", "no-store");
  r->send(res);
}

// Wi-Fi throughput drops sharply while a Bluetooth host is connected, so the
// page is stored gzipped and revalidated by ETag instead of re-sent.
void handleRoot(AsyncWebServerRequest *r) {
  bool cached = r->hasHeader("If-None-Match") && r->header("If-None-Match") == WEB_APP_ETAG;
  AsyncWebServerResponse *res = cached ? r->beginResponse(304)
                                       : r->beginResponse(200, "text/html; charset=utf-8", WEB_APP_GZ, WEB_APP_GZ_LEN);
  if (!cached) res->addHeader("Content-Encoding", "gzip");
  res->addHeader("ETag", WEB_APP_ETAG);
  res->addHeader("Cache-Control", "no-cache");
  r->send(res);
}

void handleStatus(AsyncWebServerRequest *r) {
  String json = String("{\"ble\":") + (bleConnected ? "true" : "false") +
                ",\"bleEnabled\":" + (bleEnabled ? "true" : "false") +
                ",\"typing\":" + (typing ? "true" : "false") +
                ",\"ip\":\"" + (setupAccessPoint ? WiFi.softAPIP() : WiFi.localIP()).toString() +
                "\",\"rssi\":" + WiFi.RSSI() + ",\"bssid\":\"" + WiFi.BSSIDstr() + "\",\"channel\":" + WiFi.channel() +
                ",\"accessPoints\":" + accessPointsSeen + ",\"freeHeap\":" + ESP.getFreeHeap() + "}";
  sendJson(r, json);
}

void handleGetSettings(AsyncWebServerRequest *r) {
  String json = "{\"serverUrl\":\"" + jsonEscape(settings.serverUrl) +
                "\",\"model\":\"" + jsonEscape(settings.model) +
                "\",\"language\":\"" + jsonEscape(settings.language) +
                "\",\"prompt\":\"" + jsonEscape(settings.prompt) +
                "\",\"apiKey\":\"" + jsonEscape(settings.apiKey) +
                "\",\"afterText\":\"" + jsonEscape(settings.afterText) +
                "\",\"liveTyping\":" + (settings.liveTyping ? "true" : "false") +
                ",\"keyDelayMs\":" + settings.keyDelayMs +
                ",\"maxSeconds\":" + settings.maxSeconds +
                ",\"bleName\":\"" + jsonEscape(settings.bleName) +
                "\",\"wifiName\":\"" + jsonEscape(settings.wifiName) +
                "\",\"hostname\":\"" + jsonEscape(settings.hostname) + "\"}";
  sendJson(r, json);
}

void handlePostSettings(AsyncWebServerRequest *r) {
  Settings updated = settings;
  String value;
  if (formValue(r, "serverUrl", value) && value.length()) {
    while (value.endsWith("/")) value.remove(value.length() - 1);
    updated.serverUrl = value;
  }
  if (formValue(r, "model", value) && value.length()) updated.model = value;
  if (formValue(r, "language", value)) updated.language = value;
  if (formValue(r, "prompt", value)) updated.prompt = value;
  if (formValue(r, "apiKey", value)) updated.apiKey = value;
  if (formValue(r, "afterText", value) && (value == "none" || value == "space" || value == "enter"))
    updated.afterText = value;
  if (formValue(r, "liveTyping", value)) updated.liveTyping = value == "1" || value == "true";
  if (formValue(r, "keyDelayMs", value)) updated.keyDelayMs = constrain(value.toInt(), 0, 200);
  if (formValue(r, "maxSeconds", value)) updated.maxSeconds = constrain(value.toInt(), 30, 3600);
  if (formValue(r, "bleName", value) && value.length()) updated.bleName = value.substring(0, 29);
  if (formValue(r, "wifiName", value) && value.length()) updated.wifiName = value;
  if (formValue(r, "wifiPassword", value) && value.length()) updated.wifiPassword = value;
  if (formValue(r, "hostname", value) && value.length()) updated.hostname = value;
  if (formValue(r, "webPassword", value) && value.length()) updated.webPassword = value;

  bool restart = updated.wifiName != settings.wifiName || updated.wifiPassword != settings.wifiPassword ||
                 updated.hostname != settings.hostname || updated.bleName != settings.bleName;
  bool newPassword = updated.webPassword != settings.webPassword;
  settings = updated;
  saveSettings();
  if (newPassword) updateSessionToken();  // Signs out every other browser; this one gets the new cookie.
  sendText(r, 200, restart ? "restart" : "saved", newPassword ? sessionCookie : "");
  if (restart) restartAt = millis() + 800;
}

// Collects a text/plain body; the library frees _tempObject with free().
void collectBody(AsyncWebServerRequest *r, uint8_t *data, size_t len, size_t index, size_t total) {
  if (total > 32768) return;
  if (index == 0) r->_tempObject = calloc(total + 1, 1);
  if (r->_tempObject) memcpy((char *)r->_tempObject + index, data, len);
}

// POST /api/type?after=1 with the text as body. "after" appends the
// configured space or Enter once the whole transcript is typed.
void handleType(AsyncWebServerRequest *r) {
  if (r->contentLength() > 32768) return sendText(r, 413, "Text too long");
  if (!bleConnected) return sendText(r, 503, "No Bluetooth device is connected");
  String text = r->_tempObject ? (const char *)r->_tempObject : "";
  if (!queueTyping(text, query(r, "after") == "1")) return sendText(r, 503, "Still typing, try again");
  sendText(r, 200, "queued");
}

uint8_t repeatableKey(const String &name) {
  if (name == "backspace") return 42;
  if (name == "right") return 79;
  if (name == "left") return 80;
  if (name == "down") return 81;
  if (name == "up") return 82;
  return 0;
}
// POST /api/key?k=backspace|left|right|up|down&s=down|hold|up to hold a key,
// or ?k=enter for a single press.
void handleKey(AsyncWebServerRequest *r) {
  String key = query(r, "k"), state = query(r, "s");
  if (!bleConnected) return sendText(r, 503, "No Bluetooth device is connected");
  if (key == "enter") {
    if (!queueTyping("\n", false)) return sendText(r, 503, "Still typing, try again");
    return sendText(r, 200, "ok");
  }
  uint8_t code = repeatableKey(key);
  if (!code) return sendText(r, 400, "Unknown key");
  if (state == "down") {
    stopTyping();  // Editing while text is still being typed would fight it.
    nextRepeatAt = 0;
    holdDeadline = millis() + HOLD_TIMEOUT_MS;
    heldKey = code;
  } else if (state == "hold") {
    if (heldKey == code) holdDeadline = millis() + HOLD_TIMEOUT_MS;
  } else if (heldKey == code) {
    heldKey = 0;
  }
  sendText(r, 200, "ok");
}

// POST /api/bluetooth?on=0|1
void handleBluetooth(AsyncWebServerRequest *r) {
  setBluetoothEnabled(query(r, "on") != "0");
  sendText(r, 200, bleEnabled ? "on" : "off");
}

void handleCancel(AsyncWebServerRequest *r) {
  stopTyping();
  sendText(r, 200, "cancelled");
}

// ---------- Login ----------
// The session cookie is SHA-256(device salt + password): it survives restarts
// and every session ends when the password changes. The salt is random per device.

void updateSessionToken() {
  String input = sessionSalt + settings.webPassword;
  uint8_t hash[32];
  mbedtls_sha256((const uint8_t *)input.c_str(), input.length(), hash, 0);
  char hex[33];
  for (int i = 0; i < 16; i++) sprintf(hex + i * 2, "%02x", hash[i]);
  sessionToken = hex;
  sessionCookie = "vk_session=" + sessionToken + "; Path=/; Max-Age=31536000; HttpOnly; SameSite=Lax";
}

void setupSessions() {
  preferences.begin("voicekb", false);
  sessionSalt = preferences.getString("salt", "");
  if (sessionSalt.length() != 32) {
    uint8_t salt[16];
    char hex[33];
    esp_fill_random(salt, sizeof(salt));
    for (int i = 0; i < 16; i++) sprintf(hex + i * 2, "%02x", salt[i]);
    sessionSalt = hex;
    preferences.putString("salt", sessionSalt);
  }
  preferences.end();
  updateSessionToken();
}

bool loggedIn(AsyncWebServerRequest *r) {
  if (!r->hasHeader("Cookie")) return false;
  String cookies = r->header("Cookie");
  int start = cookies.indexOf("vk_session=");
  if (start < 0) return false;
  start += 11;
  int end = cookies.indexOf(';', start);
  String value = cookies.substring(start, end < 0 ? cookies.length() : end);
  value.trim();
  return value == sessionToken;
}

// POST /api/login with password=... Repeated failures lock login briefly.
uint32_t loginLockedUntil = 0;
void handleLogin(AsyncWebServerRequest *r) {
  String password;
  if (!formValue(r, "password", password)) return sendText(r, 400, "Missing password");
  if ((int32_t)(millis() - loginLockedUntil) < 0) return sendText(r, 429, "Too many attempts, wait a moment");
  if (password != settings.webPassword) {
    failedLogins++;
    loginLockedUntil = millis() + min(failedLogins, (uint32_t)10) * 500;
    return sendText(r, 401, "Wrong password");
  }
  failedLogins = 0;
  sendText(r, 200, "ok", sessionCookie);
}

void handleLogout(AsyncWebServerRequest *r) {
  sendText(r, 200, "ok", "vk_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax");
}

typedef void (*Handler)(AsyncWebServerRequest *);

void route(const char *uri, WebRequestMethodComposite method, Handler handler, bool open = false, bool body = false) {
  auto guarded = [handler, open](AsyncWebServerRequest *r) {
    if (!open && !loggedIn(r)) return sendText(r, 401, "Login required");
    handler(r);
  };
  if (body) web.on(uri, method, guarded, nullptr, collectBody);
  else web.on(uri, method, guarded);
}

// Plain HTTP. Browsers only allow the microphone on HTTPS pages, so serve
// this through an HTTPS reverse proxy.
void setupWebServer() {
  // The page itself holds no secrets; it shows the login form when the API says 401.
  route("/", HTTP_GET, handleRoot, true);
  route("/api/login", HTTP_POST, handleLogin, true);
  route("/api/logout", HTTP_POST, handleLogout, true);
  route("/api/status", HTTP_GET, handleStatus);
  route("/api/settings", HTTP_GET, handleGetSettings);
  route("/api/settings", HTTP_POST, handlePostSettings);
  route("/api/type", HTTP_POST, handleType, false, true);
  route("/api/key", HTTP_POST, handleKey);
  route("/api/bluetooth", HTTP_POST, handleBluetooth);
  route("/api/cancel", HTTP_POST, handleCancel);
  web.onNotFound([](AsyncWebServerRequest *r) { sendText(r, 404, "Not found"); });
  web.begin();
}

void setupWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setHostname(settings.hostname.c_str());
  WiFi.setAutoReconnect(true);
  // With several access points (or a mesh) of the same name, join the strongest
  // one rather than the first one found.
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
  int found = WiFi.scanNetworks();
  accessPointsSeen = "[";
  for (int i = 0; i < found; i++) {
    if (WiFi.SSID(i) != settings.wifiName) continue;
    if (accessPointsSeen.length() > 1) accessPointsSeen += ",";
    accessPointsSeen += "{\"bssid\":\"" + WiFi.BSSIDstr(i) + "\",\"rssi\":" + WiFi.RSSI(i) + ",\"channel\":" + WiFi.channel(i) + "}";
  }
  accessPointsSeen += "]";
  WiFi.scanDelete();
  Serial.printf("Access points named %s: %s\n", settings.wifiName.c_str(), accessPointsSeen.c_str());
  WiFi.begin(settings.wifiName.c_str(), settings.wifiPassword.c_str());
  Serial.printf("Connecting to Wi-Fi %s", settings.wifiName.c_str());
  for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) {
    delay(500);
    Serial.print('.');
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\nOpen http://%s/\n", WiFi.localIP().toString().c_str());
  } else {
    // Keep retrying the router while offering a setup network to fix settings.
    setupAccessPoint = true;
    WiFi.mode(WIFI_AP_STA);
    WiFi.softAP(SETUP_AP_NAME, SETUP_AP_PASSWORD);
    Serial.printf("\nWi-Fi failed. Join %s (password %s) and open http://%s/\n", SETUP_AP_NAME,
                  SETUP_AP_PASSWORD, WiFi.softAPIP().toString().c_str());
  }
  if (MDNS.begin(settings.hostname.c_str())) {
    MDNS.addService("http", "tcp", 80);
    Serial.printf("Local name: http://%s.local/\n", settings.hostname.c_str());
  }
}

void setup() {
  Serial.begin(115200);
  keyLock = xSemaphoreCreateMutex();
  txSlots = xSemaphoreCreateCounting(TX_SLOTS, TX_SLOTS);
  typingQueue = xQueueCreate(8, sizeof(TypingJob));  // Live typing queues a phrase every few seconds.
  loadSettings();
  setupSessions();
  setupBluetooth();
  xTaskCreatePinnedToCore(typingTask, "typing", 4096, nullptr, 1, nullptr, 1);
  setupWiFi();
  setupWebServer();
}

// If the link has drifted to a far access point, reconnect: the driver then
// picks the strongest one. Checked once a minute, never while typing.
void keepStrongestAccessPoint() {
  static uint32_t lastCheck = 0;
  if (setupAccessPoint || typing || millis() - lastCheck < 60000) return;
  lastCheck = millis();
  if (WiFi.status() == WL_CONNECTED && WiFi.RSSI() < -78) {
    Serial.printf("Weak Wi-Fi (%d dBm on %s), reconnecting to the strongest access point\n", WiFi.RSSI(),
                  WiFi.BSSIDstr().c_str());
    WiFi.reconnect();
  }
}

void loop() {
  serviceHeldKey();
  keepStrongestAccessPoint();
  if (restartAt && (int32_t)(millis() - restartAt) > 0) ESP.restart();
  delay(5);
}
