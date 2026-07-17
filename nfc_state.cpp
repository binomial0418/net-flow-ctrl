// net-flow-ctrl — global state and rule evaluation.
#include "nfc_config.h"

GlobalCfg  g_cfg;
DeviceRule g_dev[NFC_MAX_DEVICES];
DeviceRt   g_rt[NFC_MAX_DEVICES];
uint32_t   g_dayKey = 0;
bool       g_uplinkUp = false;
bool       g_timeValid = false;

int nfcFindByMac(const uint8_t *mac) {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (g_dev[i].used && memcmp(g_dev[i].mac, mac, 6) == 0) {
      return i;
    }
  }
  return -1;
}

int nfcRegister(const uint8_t *mac) {
  int idx = nfcFindByMac(mac);
  if (idx >= 0) {
    return idx;
  }
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (!g_dev[i].used) {
      memset(&g_dev[i], 0, sizeof(DeviceRule));
      memset(&g_rt[i], 0, sizeof(DeviceRt));
      memcpy(g_dev[i].mac, mac, 6);
      g_dev[i].used = true;
      // A newcomer inherits the global policy: in allowlist mode it lands here
      // blocked and waits for the admin, otherwise it is online right away.
      g_dev[i].approved = g_cfg.defaultAllow;
      // Sensible starting point: unrestricted, admin tightens it in the UI.
      g_dev[i].winStart = 6 * 60;
      g_dev[i].winEnd = 21 * 60;
      g_dev[i].quotaMin = 480;
      snprintf(g_dev[i].name, NFC_NAME_LEN, "%02X%02X%02X", mac[3], mac[4], mac[5]);
      return i;
    }
  }
  return -1;
}

void nfcMacToStr(const uint8_t *mac, char *out) {
  snprintf(out, 18, "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

bool nfcStrToMac(const char *s, uint8_t *mac) {
  unsigned v[6];
  if (sscanf(s, "%2x:%2x:%2x:%2x:%2x:%2x", &v[0], &v[1], &v[2], &v[3], &v[4], &v[5]) != 6) {
    return false;
  }
  for (int i = 0; i < 6; i++) {
    mac[i] = (uint8_t)v[i];
  }
  return true;
}

// start == end means the window covers the whole day; start > end wraps midnight.
bool nfcInWindow(uint16_t nowMin, uint16_t start, uint16_t end) {
  if (start == end) {
    return true;
  }
  if (start < end) {
    return nowMin >= start && nowMin < end;
  }
  return nowMin >= start || nowMin < end;
}

BlockReason nfcEvaluate(int idx, uint16_t nowMin) {
  const DeviceRule &d = g_dev[idx];
  if (d.manualBlock) {
    return NFC_BLOCK_MANUAL;
  }
  if (!d.approved) {
    return NFC_BLOCK_UNAPPROVED;
  }
  if (!g_uplinkUp) {
    return NFC_BLOCK_NO_UPLINK;
  }
  // The quota is a plain elapsed-seconds counter, so it is enforced even with
  // no clock: it survives a reboot via NVS, and letting it lapse would mean a
  // power cycle hands out a fresh allowance until NTP lands.
  if (d.quotaEnabled && d.usedSec >= (uint32_t)d.quotaMin * 60UL) {
    return NFC_BLOCK_QUOTA;
  }
  // A time window genuinely cannot be judged without knowing the time. Fail
  // open here rather than cutting the house off on a bad NTP day -- the portal
  // flags the clock as unset, and the quota above still holds the line.
  if (g_timeValid && d.winEnabled && !nfcInWindow(nowMin, d.winStart, d.winEnd)) {
    return NFC_BLOCK_WINDOW;
  }
  return NFC_ALLOWED;
}
