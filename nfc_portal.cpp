// net-flow-ctrl — HTTP config portal (served on both the AP and STA sides).
#include "nfc_config.h"
#include "nfc_page.h"

#include <ArduinoJson.h>
#include <WebServer.h>
#include <WiFi.h>

static WebServer s_srv(80);

static String ipToStr(uint32_t lwipAddr) {
  if (lwipAddr == 0) {
    return String();
  }
  char b[16];
  snprintf(b, sizeof(b), "%u.%u.%u.%u", (unsigned)(lwipAddr & 0xFF), (unsigned)((lwipAddr >> 8) & 0xFF), (unsigned)((lwipAddr >> 16) & 0xFF), (unsigned)((lwipAddr >> 24) & 0xFF));
  return String(b);
}

static void handleRoot() {
  s_srv.send_P(200, "text/html; charset=utf-8", NFC_PAGE);
}

static void handleStatus() {
  JsonDocument doc;
  bool up = WiFi.STA.connected() && WiFi.STA.hasIP();
  doc["staUp"] = up;
  doc["staSsid"] = g_cfg.staSsid;
  doc["staIp"] = up ? WiFi.STA.localIP().toString() : String();
  doc["rssi"] = up ? WiFi.RSSI() : 0;
  doc["apSsid"] = g_cfg.apSsid;
  doc["apIp"] = WiFi.AP.localIP().toString();
  doc["resetMin"] = g_cfg.resetMin;
  doc["tz"] = g_cfg.tz;
  doc["ntp"] = g_cfg.ntp;
  doc["defaultAllow"] = g_cfg.defaultAllow;
  doc["timeValid"] = g_timeValid;

  char ts[32] = "";
  if (g_timeValid) {
    time_t now = time(nullptr);
    struct tm lt;
    localtime_r(&now, &lt);
    strftime(ts, sizeof(ts), "%m/%d %H:%M:%S", &lt);
  }
  doc["time"] = ts;

  int online = 0;
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (g_dev[i].used && g_rt[i].online) {
      online++;
    }
  }
  doc["online"] = online;

  String out;
  serializeJson(doc, out);
  s_srv.send(200, "application/json", out);
}

static void handleDevices() {
  JsonDocument doc;
  JsonArray arr = doc["devices"].to<JsonArray>();
  char mac[18];
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      continue;
    }
    nfcMacToStr(g_dev[i].mac, mac);
    JsonObject o = arr.add<JsonObject>();
    o["mac"] = mac;
    o["name"] = g_dev[i].name;
    o["ip"] = ipToStr(g_rt[i].ip);
    o["online"] = g_rt[i].online;
    o["approved"] = g_dev[i].approved;
    o["reason"] = (uint8_t)g_rt[i].reason;
    o["usedSec"] = g_dev[i].usedSec;
    o["winEnabled"] = g_dev[i].winEnabled;
    o["winStart"] = g_dev[i].winStart;
    o["winEnd"] = g_dev[i].winEnd;
    o["quotaEnabled"] = g_dev[i].quotaEnabled;
    o["quotaMin"] = g_dev[i].quotaMin;
    o["manualBlock"] = g_dev[i].manualBlock;
    o["up"] = g_dev[i].upBytes;
    o["down"] = g_dev[i].downBytes;
  }
  String out;
  serializeJson(doc, out);
  s_srv.send(200, "application/json", out);
}

static bool readBody(JsonDocument &doc) {
  if (!s_srv.hasArg("plain")) {
    s_srv.send(400, "text/plain", "missing body");
    return false;
  }
  if (deserializeJson(doc, s_srv.arg("plain")) != DeserializationError::Ok) {
    s_srv.send(400, "text/plain", "bad json");
    return false;
  }
  return true;
}

static uint16_t clampMin(int v) {
  if (v < 0) {
    return 0;
  }
  if (v > 1439) {
    return 1439;
  }
  return (uint16_t)v;
}

static void handleDevice() {
  JsonDocument doc;
  if (!readBody(doc)) {
    return;
  }
  uint8_t mac[6];
  if (!nfcStrToMac(doc["mac"] | "", mac)) {
    s_srv.send(400, "text/plain", "bad mac");
    return;
  }
  int idx = nfcFindByMac(mac);
  if (idx < 0) {
    s_srv.send(404, "text/plain", "unknown device");
    return;
  }

  if (doc["remove"].is<bool>() && doc["remove"].as<bool>()) {
    nfcFilterRemove(idx);
    memset(&g_dev[idx], 0, sizeof(DeviceRule));
    memset(&g_rt[idx], 0, sizeof(DeviceRt));
  } else {
    strlcpy(g_dev[idx].name, doc["name"] | g_dev[idx].name, NFC_NAME_LEN);
    g_dev[idx].approved = doc["approved"] | false;
    g_dev[idx].winEnabled = doc["winEnabled"] | false;
    g_dev[idx].winStart = clampMin(doc["winStart"] | 0);
    g_dev[idx].winEnd = clampMin(doc["winEnd"] | 0);
    g_dev[idx].quotaEnabled = doc["quotaEnabled"] | false;
    int q = doc["quotaMin"] | 480;
    g_dev[idx].quotaMin = (uint16_t)constrain(q, 1, 1440);
    g_dev[idx].manualBlock = doc["manualBlock"] | false;
  }
  nfcStoreSaveDevices();
  nfcSyncFilter();
  s_srv.send(200, "application/json", "{\"ok\":true}");
}

static void handleGlobal() {
  JsonDocument doc;
  if (!readBody(doc)) {
    return;
  }
  String ssid = doc["staSsid"] | "";
  String pass = doc["staPass"] | "";
  bool wifiChanged = false;

  if (ssid.length() && ssid != g_cfg.staSsid) {
    strlcpy(g_cfg.staSsid, ssid.c_str(), sizeof(g_cfg.staSsid));
    wifiChanged = true;
  }
  if (pass.length()) {  // blank means "keep the stored password"
    strlcpy(g_cfg.staPass, pass.c_str(), sizeof(g_cfg.staPass));
    wifiChanged = true;
  }

  g_cfg.resetMin = clampMin(doc["resetMin"] | (int)g_cfg.resetMin);
  bool timeChanged = false;
  String tz = doc["tz"] | "";
  String ntp = doc["ntp"] | "";
  if (tz.length() && tz != g_cfg.tz) {
    strlcpy(g_cfg.tz, tz.c_str(), sizeof(g_cfg.tz));
    timeChanged = true;
  }
  if (ntp.length() && ntp != g_cfg.ntp) {
    strlcpy(g_cfg.ntp, ntp.c_str(), sizeof(g_cfg.ntp));
    timeChanged = true;
  }
  g_cfg.defaultAllow = doc["defaultAllow"] | true;
  nfcFilterSetDefaultAllow(g_cfg.defaultAllow);
  nfcStoreSaveCfg();

  // Answer before touching the radio: reassociating drops this very socket.
  s_srv.send(200, "application/json", "{\"ok\":true}");
  if (timeChanged) {
    nfcApplyTimeCfg();
  }
  if (wifiChanged) {
    nfcStaConnect();
  }
}

static void handleScan() {
  JsonDocument doc;
  JsonArray arr = doc["nets"].to<JsonArray>();
  int n = WiFi.scanNetworks();
  for (int i = 0; i < n && i < 20; i++) {
    JsonObject o = arr.add<JsonObject>();
    o["ssid"] = WiFi.SSID(i);
    o["rssi"] = WiFi.RSSI(i);
  }
  WiFi.scanDelete();
  String out;
  serializeJson(doc, out);
  s_srv.send(200, "application/json", out);
}

static void handleResetUsage() {
  nfcResetUsage();
  s_srv.send(200, "application/json", "{\"ok\":true}");
}

void nfcPortalBegin() {
  s_srv.on("/", HTTP_GET, handleRoot);
  s_srv.on("/api/status", HTTP_GET, handleStatus);
  s_srv.on("/api/devices", HTTP_GET, handleDevices);
  s_srv.on("/api/device", HTTP_POST, handleDevice);
  s_srv.on("/api/global", HTTP_POST, handleGlobal);
  s_srv.on("/api/scan", HTTP_GET, handleScan);
  s_srv.on("/api/reset-usage", HTTP_POST, handleResetUsage);
  s_srv.onNotFound([]() {
    s_srv.sendHeader("Location", "/");
    s_srv.send(302, "text/plain", "");
  });
  s_srv.begin();
}

void nfcPortalLoop() {
  s_srv.handleClient();
}
