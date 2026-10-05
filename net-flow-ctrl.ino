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
#include <ESPmDNS.h>
#include <time.h>
#include "esp_system.h"
#include "esp_task_wdt.h"
#include "esp_wifi.h"
#include "esp_wifi_types.h"

static uint32_t s_lastTick = 0;
static bool s_naptOn = false;
static bool s_dnsPushed = false;
static bool s_mdnsOn = false;
static uint8_t s_lowHeapSec = 0;

// Why we restarted ourselves. RTC_NOINIT survives a software restart, so the
// next boot can report it; the magic guards against power-on garbage.
#define NFC_REBOOT_MAGIC 0x4E464342u
RTC_NOINIT_ATTR static uint32_t s_rebootMagic;
RTC_NOINIT_ATTR static char s_rebootTag[16];
static char s_bootWhy[32] = "";

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
    if ((r == NFC_BLOCK_QUOTA || r == NFC_YT_QUOTA) && g_rt[i].reason != r) {
      quotaJustHit = true;
    }
    g_rt[i].reason = r;
    // NO_UPLINK is a state of the world, not a verdict on the device: leave
    // the packets alone and let them fail on their own. The YT_* reasons only
    // narrow the cut to YouTube; the rest of the uplink stays open.
    bool ytLimit = r == NFC_YT_WINDOW || r == NFC_YT_QUOTA;
    nfcFilterSetBlocked(i, r != NFC_ALLOWED && r != NFC_BLOCK_NO_UPLINK && !ytLimit);
    nfcFilterSetYoutubeBlocked(i, g_dev[i].blockYoutube || ytLimit);
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
    g_dev[i].ytUsedSec = 0;
    g_dev[i].upBytes = 0;
    g_dev[i].downBytes = 0;
    g_dev[i].extendMin = 0;  // a "today" extension does not cross the reset
    g_dev[i].extendDay = 0;
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
// subtraction against the last snapshot stays correct across the wrap. The
// per-tick delta is stashed for the activity test in accumulateUsage().
static void collectBytes() {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      continue;
    }
    uint32_t up = nfcFilterUpBytes(i);
    uint32_t down = nfcFilterDownBytes(i);
    uint32_t dUp = up - g_rt[i].upSnapshot;
    uint32_t dDown = down - g_rt[i].downSnapshot;
    g_dev[i].upBytes += dUp;
    g_dev[i].downBytes += dDown;
    g_rt[i].upSnapshot = up;
    g_rt[i].downSnapshot = down;
    g_rt[i].lastDeltaBytes = dUp + dDown;

    uint32_t ytUp = nfcFilterYtUpBytes(i);
    uint32_t ytDown = nfcFilterYtDownBytes(i);
    g_rt[i].lastYtDeltaBytes = (ytUp - g_rt[i].ytUpSnapshot) + (ytDown - g_rt[i].ytDownSnapshot);
    g_rt[i].ytUpSnapshot = ytUp;
    g_rt[i].ytDownSnapshot = ytDown;
  }
}

// Usage time accrues only while a device is genuinely moving data: a second
// counts when the trailing NFC_ACTIVE_WINDOW_SEC of traffic clears the byte
// threshold, so a TV box sitting idle does not burn the daily allowance. The
// window is rolled for every device (idle ones feed a 0) so it stays honest;
// the second is only banked for a device that is online and currently allowed.
static void accumulateUsage() {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      continue;
    }
    bool active = nfcActivityTick(g_rt[i].act, g_rt[i].lastDeltaBytes);
    if (active && g_rt[i].online && g_rt[i].reason == NFC_ALLOWED) {
      g_dev[i].usedSec++;
    }
    // YouTube time is informational and judged on its own share of the
    // traffic, whatever the device's verdict: a YouTube cut simply leaves no
    // video bytes to count.
    bool ytActive = nfcActivityTick(g_rt[i].ytAct, g_rt[i].lastYtDeltaBytes);
    if (ytActive && g_rt[i].online) {
      g_dev[i].ytUsedSec++;
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

// ------------------------------------------------------------- health ------

static void rebootNow(const char *tag) {
  log_w("restarting: %s (heap %u)", tag, (unsigned)ESP.getFreeHeap());
  nfcStoreSaveUsage(true);  // otherwise up to NFC_USAGE_SAVE_MS of usage is lost
  strlcpy(s_rebootTag, tag, sizeof(s_rebootTag));
  s_rebootMagic = NFC_REBOOT_MAGIC;
  delay(100);
  ESP.restart();
}

// Sketch-initiated restarts are named by their tag; anything else falls back to
// the hardware reset reason (task_wdt = loop() got stuck, panic = a crash).
static void captureBootWhy() {
  esp_reset_reason_t r = esp_reset_reason();
  if (r == ESP_RST_SW && s_rebootMagic == NFC_REBOOT_MAGIC) {
    strlcpy(s_bootWhy, s_rebootTag, sizeof(s_bootWhy));
  } else {
    const char *s = "other";
    switch (r) {
      case ESP_RST_POWERON: s = "power_on"; break;
      case ESP_RST_SW: s = "software"; break;
      case ESP_RST_PANIC: s = "panic"; break;
      case ESP_RST_INT_WDT: s = "int_wdt"; break;
      case ESP_RST_TASK_WDT: s = "task_wdt"; break;
      case ESP_RST_WDT: s = "wdt"; break;
      case ESP_RST_BROWNOUT: s = "brownout"; break;
      default: break;
    }
    strlcpy(s_bootWhy, s, sizeof(s_bootWhy));
  }
  s_rebootMagic = 0;
}

const char *nfcRebootWhy() { return s_bootWhy; }

// A planned restart needs the clock to know it is 01:00; the uptime guard stops
// the freshly booted box from firing again within the same minute.
static void checkHealth() {
  if (g_timeValid && millis() >= NFC_REBOOT_MIN_UPTIME_MS && nowMinutes() == NFC_DAILY_REBOOT_MIN) {
    rebootNow("daily");
  }
  // Brief dips (a page being served) are normal; only a sustained low counts.
  if (ESP.getFreeHeap() < NFC_HEAP_FLOOR_BYTES) {
    if (++s_lowHeapSec >= NFC_HEAP_FLOOR_SEC) {
      rebootNow("low_heap");
    }
  } else {
    s_lowHeapSec = 0;
  }
}

// The core already runs the task watchdog (5 s, panic on expiry); stretch it so
// a slow flash write or HTTP exchange is not mistaken for a hang, then put the
// loop task under it. The core feeds it once per loop() pass.
static void startLoopWatchdog() {
  esp_task_wdt_config_t cfg = {
    .timeout_ms = NFC_LOOP_WDT_SEC * 1000,
    .idle_core_mask = 1 << 0,  // keep the core's own idle-task check on CPU0
    .trigger_panic = true,
  };
  if (esp_task_wdt_reconfigure(&cfg) != ESP_OK) {
    log_e("task watchdog reconfigure failed");
  }
  enableLoopWDT();
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
  // Advertise the portal on the home LAN once the uplink is up, so it can be
  // reached at http://<host>.local without hunting for the DHCP-assigned IP.
  if (g_uplinkUp && !s_mdnsOn) {
    if (MDNS.begin(NFC_MDNS_HOST)) {
      MDNS.addService("http", "tcp", 80);
      s_mdnsOn = true;
      log_i("mDNS up: http://%s.local", NFC_MDNS_HOST);
    }
  }

  refreshOnline();
  checkDailyReset();
  collectBytes();
  accumulateUsage();  // may push a device over its quota...
  nfcSyncFilter();    // ...which this turns into a block on the same tick
  nfcStoreSaveUsage(false);
  checkHealth();
}

// ---------------------------------------------------------------- main -----

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n[net-flow-ctrl] booting");
  captureBootWhy();
  // A software restart keeps the system clock, so the rules can apply from the
  // first tick instead of failing open until NTP lands.
  Serial.printf("[net-flow-ctrl] reset: %s, clock %s\n", s_bootWhy, timeLooksValid() ? "kept" : "unset");

  nfcStoreBegin();
  nfcStoreLoadCfg();
  nfcStoreLoadDevices();
  nfcFilterSetDefaultAllow(g_cfg.defaultAllow);
  nfcFilterSetBlockEncDns(g_cfg.blockEncDns);
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
  startLoopWatchdog();
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
