// net-flow-ctrl — per-client uplink filtering, byte accounting and NAPT setup.
//
// The ESP32 Arduino core ships lwIP with IP_FORWARD/IPV4_NAPT compiled in, but
// none of the IPv4 filter hooks (CONFIG_LWIP_HOOK_IP4_* is not CUSTOM), so
// there is no supported way to filter forwarded traffic per client. Instead we
// take the AP's lwIP netif and wrap its input/linkoutput function pointers at
// runtime. Everything addressed inside the AP subnet still flows normally --
// that is what keeps the config portal reachable for a blocked device -- while
// packets headed for the uplink are dropped when the client is over its limit.
//
// Clients are keyed by source MAC, straight out of the ethernet header: it
// needs no lease tracking, it is available on the very first packet, and a
// client cannot change it without re-associating. Keying on IP instead would
// leave a gap between association and the DHCP lease being observed, during
// which a blocked device could reconnect and slip traffic through.
#include "nfc_config.h"

#include <WiFi.h>
#include "esp_netif.h"
#include "esp_netif_net_stack.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"

// s_mac is written by the main loop only while s_valid[i] is 0, so the packet
// path never reads a half-written address. Every other field is a naturally
// aligned 32-bit word: single reads and writes are atomic on Xtensa, so the
// packet path needs no lock.
static uint8_t s_mac[NFC_MAX_DEVICES][6];
static volatile uint32_t s_valid[NFC_MAX_DEVICES];
static volatile uint32_t s_blocked[NFC_MAX_DEVICES];
static volatile uint32_t s_ip[NFC_MAX_DEVICES];  // observed, for display only
static volatile uint32_t s_up[NFC_MAX_DEVICES];  // free-running, wraps
static volatile uint32_t s_down[NFC_MAX_DEVICES];
static volatile uint32_t s_lastActive[NFC_MAX_DEVICES];
static volatile uint32_t s_defaultAllow = 1;

static struct netif *s_apNetif = nullptr;
static netif_input_fn s_origInput = nullptr;
static netif_linkoutput_fn s_origLinkoutput = nullptr;
static uint32_t s_apAddr = 0;  // lwIP byte order
static uint32_t s_apMask = 0;

static inline uint32_t rdIp(const uint8_t *b) {
  // Assemble in lwIP's byte order so it can be compared with ip4_addr_t.addr.
  return (uint32_t)b[0] | ((uint32_t)b[1] << 8) | ((uint32_t)b[2] << 16) | ((uint32_t)b[3] << 24);
}

static inline int findByMac(const uint8_t *mac) {
  for (int i = 0; i < NFC_MAX_DEVICES; i++) {
    if (s_valid[i] && memcmp(s_mac[i], mac, 6) == 0) {
      return i;
    }
  }
  return -1;
}

// True when the packet stays on the AP link and must never be filtered:
// same-subnet traffic (the portal itself), broadcast, and multicast.
static inline bool isLocalDst(const uint8_t *dst) {
  if (dst[0] == 0xFF && dst[1] == 0xFF && dst[2] == 0xFF && dst[3] == 0xFF) {
    return true;
  }
  if ((dst[0] & 0xF0) == 0xE0) {
    return true;
  }
  if (s_apMask == 0) {
    return true;  // subnet unknown: fail open rather than firewall the portal
  }
  return (rdIp(dst) & s_apMask) == (s_apAddr & s_apMask);
}

// Client -> AP. Runs on the WiFi RX task.
static err_t apInputHook(struct pbuf *p, struct netif *inp) {
  if (p != nullptr && p->len >= 34) {
    const uint8_t *d = (const uint8_t *)p->payload;
    if (d[12] == 0x08 && d[13] == 0x00) {  // IPv4
      const uint8_t *iph = d + 14;
      int idx = findByMac(d + 6);
      if (idx >= 0) {
        uint32_t src = rdIp(iph + 12);
        if (src != 0) {
          s_ip[idx] = src;  // remember the lease so the UI can show it
        }
      }
      if (!isLocalDst(iph + 16)) {  // headed for the uplink
        if (idx < 0) {
          // Unknown MAC: the main loop registers it within a tick. Until then
          // this is a brand new device, which has no rules yet anyway.
          if (!s_defaultAllow) {
            pbuf_free(p);
            return ERR_OK;
          }
        } else if (s_blocked[idx]) {
          pbuf_free(p);
          return ERR_OK;
        } else {
          s_lastActive[idx] = millis();
          s_up[idx] += p->tot_len;
        }
      }
    }
  }
  return s_origInput(p, inp);
}

// AP -> client. Accounting only: NAPT holds no entry for a blocked client, so
// there is nothing coming back to drop.
static err_t apLinkoutputHook(struct netif *nif, struct pbuf *p) {
  if (p != nullptr && p->len >= 14) {
    const uint8_t *d = (const uint8_t *)p->payload;
    if ((d[0] & 0x01) == 0) {  // unicast only
      int idx = findByMac(d);
      if (idx >= 0) {
        s_down[idx] += p->tot_len;
      }
    }
  }
  return s_origLinkoutput(nif, p);
}

void nfcFilterInstall() {
  if (s_apNetif != nullptr) {
    return;
  }
  esp_netif_t *ap = WiFi.AP.netif();
  if (ap == nullptr) {
    log_e("AP netif not ready, filter not installed");
    return;
  }
  esp_netif_ip_info_t info;
  if (esp_netif_get_ip_info(ap, &info) == ESP_OK) {
    s_apAddr = info.ip.addr;
    s_apMask = info.netmask.addr;
  }
  struct netif *nif = (struct netif *)esp_netif_get_netif_impl(ap);
  if (nif == nullptr) {
    log_e("lwIP netif not available, filter not installed");
    return;
  }
  s_origInput = nif->input;
  s_origLinkoutput = nif->linkoutput;
  nif->input = apInputHook;
  nif->linkoutput = apLinkoutputHook;
  s_apNetif = nif;
  log_i(
    "filter installed on AP netif (%u.%u.%u.%u)", (unsigned)(s_apAddr & 0xFF), (unsigned)((s_apAddr >> 8) & 0xFF), (unsigned)((s_apAddr >> 16) & 0xFF),
    (unsigned)((s_apAddr >> 24) & 0xFF)
  );
}

// Publish a slot to the packet path. It opens closed and starts from zero: the
// caller must run a rule evaluation to open it, so a device restored from NVS
// cannot slip traffic through in the gap, and a reused slot cannot inherit the
// previous owner's byte counts.
void nfcFilterSetIdentity(int idx, const uint8_t *mac) {
  s_valid[idx] = 0;  // unpublish first: the packet path must never see a half-written MAC
  memcpy(s_mac[idx], mac, 6);
  s_blocked[idx] = 1;
  s_ip[idx] = 0;
  s_up[idx] = 0;
  s_down[idx] = 0;
  s_lastActive[idx] = 0;
  s_valid[idx] = 1;
}

void nfcFilterRemove(int idx) {
  s_valid[idx] = 0;
  s_ip[idx] = 0;
  s_blocked[idx] = 0;
  s_up[idx] = 0;
  s_down[idx] = 0;
  s_lastActive[idx] = 0;
}

void nfcFilterSetBlocked(int idx, bool blocked) {
  s_blocked[idx] = blocked ? 1 : 0;
}

void nfcFilterSetDefaultAllow(bool allow) {
  s_defaultAllow = allow ? 1 : 0;
}

uint32_t nfcFilterIp(int idx) {
  return s_ip[idx];
}

uint32_t nfcFilterUpBytes(int idx) {
  return s_up[idx];
}

uint32_t nfcFilterDownBytes(int idx) {
  return s_down[idx];
}

uint32_t nfcFilterLastActiveMs(int idx) {
  return s_lastActive[idx];
}

void nfcNaptEnable() {
  if (!WiFi.AP.enableNAPT(true)) {
    log_e("enableNAPT failed");
  } else {
    log_i("NAPT enabled");
  }
}

// Hand the uplink's DNS server to AP clients. Without this they would try to
// resolve against 192.168.4.1, which runs no resolver, and "no internet" would
// look like a routing bug.
void nfcApplyUpstreamDns() {
  esp_netif_t *ap = WiFi.AP.netif();
  esp_netif_t *sta = WiFi.STA.netif();
  if (ap == nullptr || sta == nullptr) {
    return;
  }
  esp_netif_dns_info_t dns;
  if (esp_netif_get_dns_info(sta, ESP_NETIF_DNS_MAIN, &dns) != ESP_OK) {
    return;
  }
  if (dns.ip.u_addr.ip4.addr == 0) {
    dns.ip.u_addr.ip4.addr = ipaddr_addr("8.8.8.8");
    dns.ip.type = ESP_IPADDR_TYPE_V4;
  }
  uint8_t offer = 2;  // OFFER_DNS
  esp_netif_dhcps_stop(ap);
  esp_netif_set_dns_info(ap, ESP_NETIF_DNS_MAIN, &dns);
  esp_netif_dhcps_option(ap, ESP_NETIF_OP_SET, ESP_NETIF_DOMAIN_NAME_SERVER, &offer, sizeof(offer));
  esp_netif_dhcps_start(ap);
}
