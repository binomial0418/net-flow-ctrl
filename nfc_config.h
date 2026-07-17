// net-flow-ctrl — shared types and global state declarations.
#pragma once

#include <Arduino.h>

#define NFC_MAX_DEVICES 16
#define NFC_NAME_LEN    24

// Portal login. Hardcoded for now. Digest auth is used rather than basic: the
// portal also answers on the STA side, and basic would put the password (only
// base64'd) on the wire of the home LAN with every poll.
#define NFC_ADMIN_USER  "admin"
#define NFC_ADMIN_PASS  "700418"
#define NFC_AUTH_REALM  "net-flow-ctrl"

// A device only burns quota while it is actually pushing traffic. This is how
// long it may stay silent before we stop counting it as "online".
#define NFC_IDLE_GRACE_SEC 60

// Persisted per-device rule. usedSec/usedBytes ride along so a reboot does not
// hand back a fresh quota.
struct DeviceRule {
  uint8_t  mac[6];
  char     name[NFC_NAME_LEN];
  bool     used;          // slot occupied
  bool     approved;      // may reach the uplink at all; seeded from defaultAllow
  bool     winEnabled;    // limit 1: allowed time window
  uint16_t winStart;      // minutes from midnight
  uint16_t winEnd;
  bool     quotaEnabled;  // limit 2: daily cumulative minutes
  uint16_t quotaMin;
  bool     manualBlock;   // admin override, survives reset
  uint32_t usedSec;       // consumed today
  uint32_t upBytes;       // today, informational
  uint32_t downBytes;
};

struct GlobalCfg {
  char     staSsid[33];
  char     staPass[64];
  char     apSsid[33];
  char     apPass[64];
  uint16_t resetMin;      // daily reset, minutes from midnight (default 05:00)
  char     tz[40];        // POSIX TZ, e.g. "CST-8"
  char     ntp[64];
  bool     defaultAllow;  // policy for devices not yet in the table
};

// Reason a device is currently cut off from the uplink.
// Kept in sync with the reason switch in nfc_page.h.
enum BlockReason : uint8_t {
  NFC_ALLOWED = 0,
  NFC_BLOCK_MANUAL,
  NFC_BLOCK_WINDOW,
  NFC_BLOCK_QUOTA,
  NFC_BLOCK_NO_UPLINK,
  NFC_BLOCK_UNAPPROVED,
};

struct DeviceRt {
  uint32_t    ip;          // lwIP byte order, 0 = unknown
  bool        online;
  uint32_t    upSnapshot;  // last value read out of the filter counters
  uint32_t    downSnapshot;
  BlockReason reason;
};

extern GlobalCfg  g_cfg;
extern DeviceRule g_dev[NFC_MAX_DEVICES];
extern DeviceRt   g_rt[NFC_MAX_DEVICES];
extern uint32_t   g_dayKey;      // logical day the counters belong to
extern bool       g_uplinkUp;    // STA has an IP and NAPT is live
extern bool       g_timeValid;   // NTP has landed at least once

// --- state helpers (nfc_state.cpp) -----------------------------------------
int  nfcFindByMac(const uint8_t *mac);
int  nfcRegister(const uint8_t *mac);           // -1 when the table is full
void nfcMacToStr(const uint8_t *mac, char *out);  // out >= 18 bytes
bool nfcStrToMac(const char *s, uint8_t *mac);
bool nfcInWindow(uint16_t nowMin, uint16_t start, uint16_t end);
BlockReason nfcEvaluate(int idx, uint16_t nowMin);

// --- persistence (nfc_store.cpp) -------------------------------------------
void nfcStoreBegin();
void nfcStoreLoadCfg();
void nfcStoreSaveCfg();
void nfcStoreLoadDevices();
void nfcStoreSaveDevices();
void nfcStoreSaveUsage(bool force);  // rate-limited; force on important edges

// --- packet filter / NAPT (nfc_filter.cpp) ---------------------------------
void nfcFilterInstall();  // wraps the AP netif hooks
void nfcFilterSetIdentity(int idx, const uint8_t *mac);  // publish slot to the packet path
void nfcFilterRemove(int idx);
void nfcFilterSetBlocked(int idx, bool blocked);
void nfcFilterSetDefaultAllow(bool allow);
uint32_t nfcFilterIp(int idx);  // last source IP seen from this MAC, 0 = unknown
uint32_t nfcFilterUpBytes(int idx);
uint32_t nfcFilterDownBytes(int idx);
uint32_t nfcFilterLastActiveMs(int idx);
void nfcNaptEnable();
void nfcApplyUpstreamDns();

// --- web portal (nfc_portal.cpp) -------------------------------------------
void nfcPortalBegin();
void nfcPortalLoop();

// --- actions the portal asks of the main sketch (net-flow-ctrl.ino) --------
void nfcStaConnect();     // (re)connect the uplink using g_cfg
void nfcApplyTimeCfg();   // re-arm NTP/TZ after a config change
void nfcResetUsage();     // zero today's counters for every device
void nfcSyncFilter();     // re-evaluate every rule and push it to the filter
