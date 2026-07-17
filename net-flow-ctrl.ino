// net-flow-ctrl — ESP32 dual-mode WiFi router with per-device uplink limits.
//
//   * AP + STA at once: clients join the hotspot, NAPT forwards them to the
//     upstream AP.
//   * Each client can be given two independent limits: an allowed time window
//     and a daily cap on cumulative online minutes. Tripping either one cuts
//     that device off from the uplink until the daily reset (default 05:00).
//   * A blocked device stays associated and can still open the config portal
//     at http://192.168.4.1 -- only forwarded traffic is dropped.
//
// Build: arduino-cli compile -b esp32:esp32:esp32
#include "nfc_config.h"

#include <WiFi.h>
#include <time.h>
#include "esp_wifi.h"
#include "esp_wifi_types.h"

static uint32_t s_lastTick = 0;
static bool s_naptOn = false;
static bool s_dnsPushed = false;

// ---------------------------------------------------------------- time -----

static bool timeLooksValid() {
  return time(nullptr) > 1600000000;  // anything past 2020 means NTP landed
}

// Identifies the "logical day" a counter belongs to: the clock is shifted back
// by the reset offset, so the day rolls over exactly at g_cfg.resetMin. Deriving
// this from the wall clock (instead of a timer) means a reboot or a missed
// reset still lands on the right day.
static uint32_t currentDayKey() {
  time_t shifted = time(nullptr) - (time_t)g_cfg.resetMin * 60;
  struct tm t;
  localtime_r(&shifted, &t);
  return (uint32_t)(t.tm_year + 1900) * 10000u + (uint32_t)(t.tm_mon + 1) * 100u + (uint32_t)t.tm_mday;
}

static uint16_t nowMinutes() {
  time_t now = time(nullptr);
  struct tm t;
  localtime_r(&now, &t);
  return (uint16_t)(t.tm_hour * 60 + t.tm_min);
}

void nfcApplyTimeCfg() {
  configTzTime(g_cfg.tz, g_cfg.ntp);
}

// ---------------------------------------------------------------- wifi -----

void nfcStaConnect() {
  if (strlen(g_cfg.staSsid) == 0) {
    log_w("no upstream SSID configured");
    return;
  }
  log_i("connecting uplink: %s", g_cfg.staSsid);
  s_naptOn = false;
  s_dnsPushed = false;
  WiFi.disconnect();
  WiFi.begin(g_cfg.staSsid, g_cfg.staPass);
}

// ------------------------------------------------------------- rules -------

void nfcSyncFilter() {
  uint16_t nm = nowMinutes();
  bool quotaJustHit = false;
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      nfcFilterRemove(i);
      continue;
    }
    BlockReason r = nfcEvaluate(i, nm);
    if (r == NFC_BLOCK_QUOTA && g_rt[i].reason != NFC_BLOCK_QUOTA) {
      quotaJustHit = true;
    }
    g_rt[i].reason = r;
    // NO_UPLINK is a state of the world, not a verdict on the device: leave
    // the packets alone and let them fail on their own.
    nfcFilterSetBlocked(i, r != NFC_ALLOWED && r != NFC_BLOCK_NO_UPLINK);
  }
  // Exhausting a quota is the one moment where losing the last few minutes of
  // counting would hand back a whole allowance, so checkpoint it immediately.
  if (quotaJustHit) {
    nfcStoreSaveUsage(true);
  }
}

void nfcResetUsage() {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    g_dev[i].usedSec = 0;
    g_dev[i].upBytes = 0;
    g_dev[i].downBytes = 0;
  }
  nfcSyncFilter();
  nfcStoreSaveUsage(true);
  log_i("daily counters reset");
}

// Poll the association list rather than reacting to WiFi events: it runs in
// loop context, so the device table needs no locking.
static void refreshOnline() {
  wifi_sta_list_t list;
  if (esp_wifi_ap_get_sta_list(&list) != ESP_OK) {
    return;
  }
  bool seen[NFC_MAX_DEVICES] = {false};
  bool dirty = false;
  for (int n = 0; n < list.num; n++) {
    int idx = nfcFindByMac(list.sta[n].mac);
    if (idx < 0) {
      idx = nfcRegister(list.sta[n].mac);
      if (idx < 0) {
        log_w("device table full, ignoring new client");
        continue;
      }
      nfcFilterSetIdentity(idx, g_dev[idx].mac);
      dirty = true;
    }
    seen[idx] = true;
    g_rt[idx].online = true;
    g_rt[idx].ip = nfcFilterIp(idx);  // learned from the client's own packets
  }
  bool sessionEnded = false;
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (g_dev[i].used && !seen[i] && g_rt[i].online) {
      g_rt[i].online = false;
      g_rt[i].ip = 0;
      sessionEnded = true;
      // The identity stays published: a blocked device that re-associates must
      // be blocked from its very first packet, not a tick later.
    }
  }
  if (dirty) {
    nfcStoreSaveDevices();
  } else if (sessionEnded) {
    // A device leaving is a natural checkpoint: bank what it used before a
    // power cut can roll the counter back to the last periodic write.
    nfcStoreSaveUsage(true);
  }
}

// Counters in the filter are free-running 32-bit and may wrap; unsigned
// subtraction against the last snapshot stays correct across the wrap.
static void collectBytes() {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      continue;
    }
    uint32_t up = nfcFilterUpBytes(i);
    uint32_t down = nfcFilterDownBytes(i);
    g_dev[i].upBytes += up - g_rt[i].upSnapshot;
    g_dev[i].downBytes += down - g_rt[i].downSnapshot;
    g_rt[i].upSnapshot = up;
    g_rt[i].downSnapshot = down;
  }
}

// Quota burns only while a device is actually using the uplink, so a phone
// idling in a pocket overnight does not eat someone's whole day.
static void accumulateUsage() {
  uint32_t now = millis();
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used || !g_rt[i].online || g_rt[i].reason != NFC_ALLOWED) {
      continue;
    }
    uint32_t last = nfcFilterLastActiveMs(i);
    if (last != 0 && (now - last) < (NFC_IDLE_GRACE_SEC * 1000UL)) {
      g_dev[i].usedSec++;
    }
  }
}

static void checkDailyReset() {
  if (!g_timeValid) {
    return;
  }
  uint32_t key = currentDayKey();
  if (g_dayKey == 0) {
    g_dayKey = key;  // first boot with a valid clock: adopt, do not wipe
    nfcStoreSaveUsage(true);
    return;
  }
  if (key != g_dayKey) {
    g_dayKey = key;
    nfcResetUsage();
  }
}

static void tick() {
  g_timeValid = timeLooksValid();
  g_uplinkUp = WiFi.STA.connected() && WiFi.STA.hasIP();

  if (g_uplinkUp && !s_naptOn) {
    nfcNaptEnable();
    s_naptOn = true;
  }
  if (g_uplinkUp && !s_dnsPushed) {
    nfcApplyUpstreamDns();
    s_dnsPushed = true;
    nfcApplyTimeCfg();
  }

  refreshOnline();
  checkDailyReset();
  collectBytes();
  accumulateUsage();  // may push a device over its quota...
  nfcSyncFilter();    // ...which this turns into a block on the same tick
  nfcStoreSaveUsage(false);
}

// ---------------------------------------------------------------- main -----

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[net-flow-ctrl] booting");

  nfcStoreBegin();
  nfcStoreLoadCfg();
  nfcStoreLoadDevices();
  nfcFilterSetDefaultAllow(g_cfg.defaultAllow);
  // Republish the rules restored from NVS before the radio comes up, so a
  // device that was blocked yesterday is still blocked on its first packet.
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (g_dev[i].used) {
      nfcFilterSetIdentity(i, g_dev[i].mac);
    }
  }
  nfcSyncFilter();

  WiFi.mode(WIFI_AP_STA);
  WiFi.setAutoReconnect(true);
  WiFi.softAP(g_cfg.apSsid, g_cfg.apPass);
  delay(100);  // let esp_netif finish bringing the AP up before we wrap it
  nfcFilterInstall();

  Serial.printf("[net-flow-ctrl] AP \"%s\" at %s\n", g_cfg.apSsid, WiFi.AP.localIP().toString().c_str());
  nfcStaConnect();
  nfcApplyTimeCfg();
  nfcPortalBegin();
  Serial.println("[net-flow-ctrl] portal ready on http://192.168.4.1");
}

void loop() {
  nfcPortalLoop();
  uint32_t now = millis();
  if (now - s_lastTick >= 1000) {
    s_lastTick = now;
    tick();
  }
  delay(2);
}
