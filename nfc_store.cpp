// net-flow-ctrl — NVS persistence for the global config and device rules.
#include "nfc_config.h"

#include <Preferences.h>
#include <stddef.h>

// Usage counters are rewritten on a slow cadence: at ~10 min a full year of
// uptime costs ~52k NVS writes, comfortably inside the flash endurance budget.
#define NFC_USAGE_SAVE_MS (10UL * 60UL * 1000UL)

static Preferences s_prefs;
static uint32_t s_lastUsageSave = 0;

void nfcStoreBegin() { s_prefs.begin("netflow", false); }

void nfcStoreLoadCfg() {
  memset(&g_cfg, 0, sizeof(g_cfg));
  // Defaults before any load fills them.
  g_cfg.activeKBmin = NFC_ACTIVE_KBMIN_DEFAULT;
  g_cfg.blockEncDns = true;
  size_t n = s_prefs.getBytesLength("cfg");
  if (n == sizeof(GlobalCfg)) {
    s_prefs.getBytes("cfg", &g_cfg, sizeof(GlobalCfg));
  } else if (n > 0 && n < sizeof(GlobalCfg)) {
    // An older, shorter layout: load exactly what was stored (fields kept their
    // offsets, new ones are appended), leaving the trailing additions at their
    // defaults, then rewrite in the current layout.
    s_prefs.getBytes("cfg", &g_cfg, n);
    nfcStoreSaveCfg();
  } else {
    strlcpy(g_cfg.apSsid, "NetFlowCtrl", sizeof(g_cfg.apSsid));
    strlcpy(g_cfg.apPass, "12345678", sizeof(g_cfg.apPass));
    strlcpy(g_cfg.tz, "CST-8", sizeof(g_cfg.tz));
    strlcpy(g_cfg.ntp, "pool.ntp.org", sizeof(g_cfg.ntp));
    g_cfg.resetMin = 5 * 60; // 05:00
    g_cfg.defaultAllow = true;
    nfcStoreSaveCfg();
  }
  g_dayKey = s_prefs.getULong("daykey", 0);
}

void nfcStoreSaveCfg() { s_prefs.putBytes("cfg", &g_cfg, sizeof(GlobalCfg)); }

void nfcStoreLoadDevices() {
  memset(g_dev, 0, sizeof(g_dev));
  size_t n = s_prefs.getBytesLength("devs");
  size_t stride = n / NFC_MAX_DEVICES;
  if (n == sizeof(g_dev)) {
    s_prefs.getBytes("devs", g_dev, sizeof(g_dev));
  } else if (n % NFC_MAX_DEVICES == 0 && stride >= offsetof(DeviceRule, blockYoutube) && stride < sizeof(DeviceRule)) {
    // An older, shorter record layout: fields kept their offsets and new ones
    // were appended, so copy each record's stored prefix, leave the additions
    // zeroed (= off), then rewrite in the current layout.
    uint8_t *buf = (uint8_t *)malloc(n);
    if (buf != nullptr) {
      s_prefs.getBytes("devs", buf, n);
      for (int i = 0; i < NFC_MAX_DEVICES; i++) {
        memcpy(&g_dev[i], buf + i * stride, stride);
      }
      free(buf);
      nfcStoreSaveDevices();
    }
  }
  memset(g_rt, 0, sizeof(g_rt));
}

void nfcStoreSaveDevices() {
  s_prefs.putBytes("devs", g_dev, sizeof(g_dev));
  s_lastUsageSave = millis();
}

void nfcStoreSaveUsage(bool force) {
  uint32_t now = millis();
  if (!force && (now - s_lastUsageSave) < NFC_USAGE_SAVE_MS) {
    return;
  }
  s_prefs.putBytes("devs", g_dev, sizeof(g_dev));
  s_prefs.putULong("daykey", g_dayKey);
  s_lastUsageSave = now;
}
