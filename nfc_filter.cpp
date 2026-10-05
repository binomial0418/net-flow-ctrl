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
//
// YouTube is recognised by name, not by address: its front-ends share IPs with
// the rest of Google. Both cutting and timing it depend on seeing DNS, so every
// device is refused DNS-over-TLS (853) and DNS-over-HTTPS to the well-known
// public resolvers, and falls back to plain DNS. Refused, not dropped: a silent
// drop leaves the client waiting out a timeout on every lookup, which stalls
// video streaming as it hops between CDN hosts.
// (Switchable from the portal, g_cfg.blockEncDns.) (Google TV, for one, otherwise sends
// its googlevideo.com lookups to 8.8.8.8 over TLS and is never seen.) A device
// under a YouTube block then gets an NXDOMAIN answered in place of its resolver
// for any YouTube domain. And because a video already playing keeps its
// connection to the video CDN, the googlevideo.com addresses seen in DNS answers
// are remembered: traffic to them is timed, and dropped under a block.
#include "nfc_config.h"

#include <WiFi.h>
#include "esp_netif.h"
#include "esp_netif_net_stack.h"
#include "lwip/netif.h"
#include "lwip/pbuf.h"
#include "lwip/tcpip.h"

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
static volatile uint32_t s_ytUp[NFC_MAX_DEVICES];  // to/from learned video IPs, wraps
static volatile uint32_t s_ytDown[NFC_MAX_DEVICES];
static volatile uint32_t s_ytBlock[NFC_MAX_DEVICES];
static volatile uint32_t s_defaultAllow = 1;
static volatile uint32_t s_blockEncDns = 1;

// googlevideo.com addresses learned from DNS answers, used both to cut and to
// time YouTube viewing. Written only by the tcpip thread (linkoutput), read by
// the RX task; each slot is one aligned word.
#define NFC_YT_IPS 64
static volatile uint32_t s_ytIp[NFC_YT_IPS];
static uint32_t s_ytIpNext = 0;


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

// ------------------------------------------------------------ YouTube ------

// A name matches when it is one of these or a subdomain of one. Kept tight:
// broader Google domains (googleapis.com, ggpht.com) carry unrelated services.
static const char *const kYtDomains[] = {
  "youtube.com",      "youtu.be",       "yt.be",          "googlevideo.com",        "ytimg.com",
  "youtubekids.com",  "youtube-nocookie.com", "youtubei.googleapis.com", "youtube.googleapis.com",
  "yt3.ggpht.com",    "yt4.ggpht.com",
};

// Public resolvers that answer DNS-over-HTTPS on their own IP; their 443 is
// cut for every device so the client falls back to plain DNS.
static const uint8_t kDohIps[][4] = {
  {8, 8, 8, 8},     {8, 8, 4, 4},   {1, 1, 1, 1},   {1, 0, 0, 1},   {9, 9, 9, 9},     {149, 112, 112, 112},
  {208, 67, 222, 222}, {208, 67, 220, 220}, {94, 140, 14, 14}, {94, 140, 15, 15}, {185, 228, 168, 9},
  {104, 16, 248, 249}, {104, 16, 249, 249},  // chrome.cloudflare-dns.com
  {162, 159, 61, 4},   {172, 64, 41, 4},     // mozilla.cloudflare-dns.com
};

static bool nameUnder(const char *name, size_t len, const char *dom) {
  size_t dl = strlen(dom);
  if (len < dl || memcmp(name + len - dl, dom, dl) != 0) {
    return false;
  }
  return len == dl || name[len - dl - 1] == '.';
}

static bool isYoutubeName(const char *name) {
  size_t len = strlen(name);
  for (const char *dom : kYtDomains) {
    if (nameUnder(name, len, dom)) {
      return true;
    }
  }
  return false;
}

static bool isDohResolver(const uint8_t *ip) {
  for (const auto &r : kDohIps) {
    if (memcmp(ip, r, 4) == 0) {
      return true;
    }
  }
  return false;
}

static bool isVideoIp(uint32_t ip) {
  for (int i = 0; i < NFC_YT_IPS; i++) {
    if (s_ytIp[i] == ip) {
      return true;
    }
  }
  return false;
}

// Decode an uncompressed question name into lowercase dotted text. Returns the
// byte after the name, or nullptr when it is malformed or does not fit.
static const uint8_t *readQname(const uint8_t *q, const uint8_t *end, char *out, size_t cap) {
  size_t n = 0;
  while (q < end) {
    uint8_t len = *q++;
    if (len == 0) {
      out[n] = 0;
      return q;
    }
    if (len > 63 || q + len > end || n + len + 2 > cap) {
      return nullptr;
    }
    if (n) {
      out[n++] = '.';
    }
    for (uint8_t i = 0; i < len; i++) {
      char c = (char)q[i];
      out[n++] = (c >= 'A' && c <= 'Z') ? (char)(c + 32) : c;
    }
    q += len;
  }
  return nullptr;
}

// Skip a resource-record name, which may end in a compression pointer.
static const uint8_t *skipName(const uint8_t *p, const uint8_t *end) {
  while (p < end) {
    uint8_t len = *p;
    if (len == 0) {
      return p + 1;
    }
    if ((len & 0xC0) == 0xC0) {
      return (p + 2 <= end) ? p + 2 : nullptr;
    }
    if (len > 63) {
      return nullptr;
    }
    p += 1 + len;
  }
  return nullptr;
}

// Locate the transport header of an unfragmented IPv4 packet. `end` is clipped
// to the IP total length so link padding is never parsed as payload.
static const uint8_t *l4Header(const uint8_t *iph, const uint8_t *&end) {
  if ((iph[0] >> 4) != 4) {
    return nullptr;
  }
  uint8_t ihl = (iph[0] & 0x0F) * 4;
  uint16_t total = ((uint16_t)iph[2] << 8) | iph[3];
  if (ihl < 20 || total < ihl) {
    return nullptr;
  }
  if (iph + total < end) {
    end = iph + total;
  }
  if ((iph[6] & 0x1F) != 0 || iph[7] != 0) {
    return nullptr;  // a later fragment: no transport header in it
  }
  const uint8_t *l4 = iph + ihl;
  return (l4 + 8 <= end) ? l4 : nullptr;
}

static void sendReplyCb(void *ctx) {
  struct pbuf *r = (struct pbuf *)ctx;
  s_origLinkoutput(s_apNetif, r);
  pbuf_free(r);
}

// One's-complement sum of big-endian 16-bit words, odd tail padded with zero.
static uint32_t sum16(const uint8_t *p, int len, uint32_t sum) {
  for (int i = 0; i + 1 < len; i += 2) {
    sum += ((uint32_t)p[i] << 8) | p[i + 1];
  }
  if (len & 1) {
    sum += (uint32_t)p[len - 1] << 8;
  }
  return sum;
}

static uint16_t foldSum(uint32_t sum) {
  while (sum >> 16) {
    sum = (sum & 0xFFFF) + (sum >> 16);
  }
  return (uint16_t)~sum;
}

static inline void wr16(uint8_t *p, uint16_t v) {
  p[0] = v >> 8;
  p[1] = v & 0xFF;
}

static inline uint32_t rd32be(const uint8_t *p) {
  return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

static inline void wr32be(uint8_t *p, uint32_t v) {
  wr16(p, v >> 16);
  wr16(p + 2, v & 0xFFFF);
}

// Start a reply to the client that sent `eth`, posing as the host it addressed:
// ethernet and IPv4 headers filled in, `l4Len` bytes of payload left to write.
static struct pbuf *newReply(const uint8_t *eth, uint8_t proto, uint16_t l4Len) {
  struct pbuf *r = pbuf_alloc(PBUF_RAW, 14 + 20 + l4Len, PBUF_RAM);
  if (r == nullptr) {
    return nullptr;
  }
  const uint8_t *iph = eth + 14;
  uint8_t *o = (uint8_t *)r->payload;
  memcpy(o, eth + 6, 6);  // back to the client...
  memcpy(o + 6, eth, 6);  // ...from the AP's own MAC it addressed
  o[12] = 0x08;
  o[13] = 0x00;
  uint8_t *ip = o + 14;
  memset(ip, 0, 20);
  ip[0] = 0x45;
  wr16(ip + 2, 20 + l4Len);
  ip[6] = 0x40;  // DF
  ip[8] = 64;
  ip[9] = proto;
  memcpy(ip + 12, iph + 16, 4);  // from the host it addressed
  memcpy(ip + 16, iph + 12, 4);
  wr16(ip + 10, foldSum(sum16(ip, 20, 0)));
  return r;
}

static void queueReply(struct pbuf *r) {
  if (tcpip_try_callback(sendReplyCb, r) != ERR_OK) {
    pbuf_free(r);
  }
}

// Answer a DNS query with NXDOMAIN on the resolver's behalf: the app fails at
// once instead of retrying into a timeout. The reply is handed to the tcpip
// thread rather than transmitted from the RX task.
static void sendNxdomain(const uint8_t *eth, const uint8_t *udp, const uint8_t *dns, const uint8_t *qend) {
  uint16_t dnsLen = (uint16_t)(qend - dns);  // header + first question
  struct pbuf *r = newReply(eth, 17, 8 + dnsLen);
  if (r == nullptr) {
    return;
  }
  uint8_t *u = (uint8_t *)r->payload + 14 + 20;
  u[0] = udp[2];
  u[1] = udp[3];
  u[2] = udp[0];
  u[3] = udp[1];
  wr16(u + 4, 8 + dnsLen);
  u[6] = u[7] = 0;  // no UDP checksum, legal over IPv4

  uint8_t *dn = u + 8;
  memcpy(dn, dns, dnsLen);
  dn[2] = 0x80 | (dns[2] & 0x79);  // QR, keep opcode and RD
  dn[3] = 0x80 | 3;                // RA, NXDOMAIN
  dn[4] = 0;
  dn[5] = 1;  // the one question echoed back
  memset(dn + 6, 0, 6);
  queueReply(r);
}

// Refuse a TCP segment the way a closed port would (RFC 793 reset rules), so
// the client gives up at once instead of retransmitting its SYN.
static void sendTcpReset(const uint8_t *eth, const uint8_t *tcp, const uint8_t *end) {
  const uint8_t *iph = eth + 14;
  if (tcp + 20 > end) {
    return;
  }
  uint8_t flags = tcp[13];
  if (flags & 0x04) {
    return;  // never answer a reset
  }
  uint8_t ihl = (iph[0] & 0x0F) * 4;
  uint8_t doff = (tcp[12] >> 4) * 4;
  uint16_t total = ((uint16_t)iph[2] << 8) | iph[3];
  if (doff < 20 || ihl + doff > total) {
    return;
  }
  struct pbuf *r = newReply(eth, 6, 20);
  if (r == nullptr) {
    return;
  }
  uint8_t *ip = (uint8_t *)r->payload + 14;
  uint8_t *t = ip + 20;
  memset(t, 0, 20);
  t[0] = tcp[2];
  t[1] = tcp[3];
  t[2] = tcp[0];
  t[3] = tcp[1];
  if (flags & 0x10) {  // ACK set: the reset takes its sequence from that ACK
    wr32be(t + 4, rd32be(tcp + 8));
    t[13] = 0x04;  // RST
  } else {             // otherwise acknowledge the whole segment
    uint32_t segLen = (uint32_t)(total - ihl - doff) + ((flags & 0x02) ? 1 : 0) + ((flags & 0x01) ? 1 : 0);
    wr32be(t + 8, rd32be(tcp + 4) + segLen);
    t[13] = 0x14;  // RST|ACK
  }
  t[12] = 0x50;  // 20-byte header
  uint32_t sum = sum16(ip + 12, 8, 0) + 6 + 20;  // pseudo-header
  wr16(t + 16, foldSum(sum16(t, 20, sum)));
  queueReply(r);
}

// Refuse a UDP datagram (QUIC) with ICMP port unreachable, quoting the
// offending IP header and first 8 payload bytes as RFC 792 requires.
static void sendPortUnreachable(const uint8_t *eth) {
  const uint8_t *iph = eth + 14;
  uint16_t quote = (iph[0] & 0x0F) * 4 + 8;
  struct pbuf *r = newReply(eth, 1, 8 + quote);
  if (r == nullptr) {
    return;
  }
  uint8_t *ic = (uint8_t *)r->payload + 14 + 20;
  memset(ic, 0, 8);
  ic[0] = 3;  // destination unreachable
  ic[1] = 3;  // port unreachable
  memcpy(ic + 8, iph, quote);
  wr16(ic + 2, foldSum(sum16(ic, 8 + quote, 0)));
  queueReply(r);
}

// Client -> uplink packet: true for encrypted DNS, which is refused rather than
// forwarded so that every lookup stays visible.
static bool rejectEncryptedDns(const uint8_t *eth, const uint8_t *end) {
  const uint8_t *iph = eth + 14;
  uint8_t proto = iph[9];
  if (proto != 6 && proto != 17) {
    return false;
  }
  const uint8_t *l4 = l4Header(iph, end);
  if (l4 == nullptr) {
    return false;
  }
  uint16_t dport = ((uint16_t)l4[2] << 8) | l4[3];
  // DNS-over-TLS (Android Private DNS), or DNS-over-HTTPS / QUIC
  if (dport != 853 && !(dport == 443 && isDohResolver(iph + 16))) {
    return false;
  }
  if (proto == 6) {
    sendTcpReset(eth, l4, end);
  } else {
    sendPortUnreachable(eth);
  }
  return true;
}

// Client -> uplink packet from a device under a YouTube block. True when it
// must not be forwarded (dropped, or answered here in place of the resolver).
static bool ytIntercept(const uint8_t *eth, const uint8_t *end) {
  const uint8_t *iph = eth + 14;
  if (isVideoIp(rdIp(iph + 16))) {
    return true;  // a stream that started before the block
  }
  if (iph[9] != 17) {
    return false;
  }
  const uint8_t *l4 = l4Header(iph, end);
  if (l4 == nullptr) {
    return false;
  }
  uint16_t dport = ((uint16_t)l4[2] << 8) | l4[3];
  if (dport != 53) {
    return false;
  }
  const uint8_t *dns = l4 + 8;
  if (dns + 12 > end || (dns[2] & 0x80) || (dns[4] == 0 && dns[5] == 0)) {
    return false;  // not a query
  }
  char name[128];
  const uint8_t *q = readQname(dns + 12, end, name, sizeof(name));
  if (q == nullptr || q + 4 > end || !isYoutubeName(name)) {
    return false;
  }
  sendNxdomain(eth, l4, dns, q + 4);
  return true;
}

// Uplink -> client DNS answer: remember the addresses behind googlevideo.com
// names so a block can also cut a video that is already streaming.
static void learnVideoIps(const uint8_t *eth, const uint8_t *end) {
  if (eth + 34 > end || eth[12] != 0x08 || eth[13] != 0x00) {
    return;
  }
  const uint8_t *iph = eth + 14;
  if (iph[9] != 17) {
    return;
  }
  const uint8_t *l4 = l4Header(iph, end);
  if (l4 == nullptr || l4[0] != 0 || l4[1] != 53) {
    return;
  }
  const uint8_t *dns = l4 + 8;
  if (dns + 12 > end || !(dns[2] & 0x80) || (dns[3] & 0x0F) != 0 || dns[4] != 0 || dns[5] != 1) {
    return;  // only successful single-question answers
  }
  char name[128];
  const uint8_t *p = readQname(dns + 12, end, name, sizeof(name));
  if (p == nullptr || !nameUnder(name, strlen(name), "googlevideo.com")) {
    return;
  }
  p += 4;
  uint16_t an = ((uint16_t)dns[6] << 8) | dns[7];
  for (uint16_t i = 0; i < an && p != nullptr; i++) {
    p = skipName(p, end);
    if (p == nullptr || p + 10 > end) {
      return;
    }
    uint16_t type = ((uint16_t)p[0] << 8) | p[1];
    uint16_t rdLen = ((uint16_t)p[8] << 8) | p[9];
    p += 10;
    if (p + rdLen > end) {
      return;
    }
    if (type == 1 && rdLen == 4) {
      uint32_t ip = rdIp(p);
      if (!isVideoIp(ip)) {
        s_ytIp[s_ytIpNext] = ip;
        s_ytIpNext = (s_ytIpNext + 1) % NFC_YT_IPS;
      }
    }
    p += rdLen;
  }
}

// ------------------------------------------------------------- hooks -------

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
        } else if (s_blocked[idx] || (s_blockEncDns && rejectEncryptedDns(d, d + p->len)) || (s_ytBlock[idx] && ytIntercept(d, d + p->len))) {
          pbuf_free(p);
          return ERR_OK;
        } else {
          s_up[idx] += p->tot_len;
          if (isVideoIp(rdIp(iph + 16))) {
            s_ytUp[idx] += p->tot_len;
          }
        }
      }
    }
  }
  return s_origInput(p, inp);
}

// AP -> client. Accounting and DNS snooping only: NAPT holds no entry for a
// blocked client, so there is nothing coming back to drop. Runs on the tcpip
// thread, the sole writer of s_ytIp.
static err_t apLinkoutputHook(struct netif *nif, struct pbuf *p) {
  if (p != nullptr && p->len >= 14) {
    const uint8_t *d = (const uint8_t *)p->payload;
    if ((d[0] & 0x01) == 0) {  // unicast only
      int idx = findByMac(d);
      if (idx >= 0) {
        s_down[idx] += p->tot_len;
        if (p->len >= 34 && d[12] == 0x08 && d[13] == 0x00 && isVideoIp(rdIp(d + 14 + 12))) {
          s_ytDown[idx] += p->tot_len;
        }
      }
      learnVideoIps(d, d + p->len);
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
  s_ytBlock[idx] = 0;
  s_ip[idx] = 0;
  s_up[idx] = 0;
  s_down[idx] = 0;
  s_ytUp[idx] = 0;
  s_ytDown[idx] = 0;
  s_valid[idx] = 1;
}

void nfcFilterRemove(int idx) {
  s_valid[idx] = 0;
  s_ip[idx] = 0;
  s_blocked[idx] = 0;
  s_ytBlock[idx] = 0;
  s_up[idx] = 0;
  s_down[idx] = 0;
  s_ytUp[idx] = 0;
  s_ytDown[idx] = 0;
}

void nfcFilterSetBlocked(int idx, bool blocked) {
  s_blocked[idx] = blocked ? 1 : 0;
}

void nfcFilterSetYoutubeBlocked(int idx, bool blocked) {
  s_ytBlock[idx] = blocked ? 1 : 0;
}

void nfcFilterSetBlockEncDns(bool block) {
  s_blockEncDns = block ? 1 : 0;
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

uint32_t nfcFilterYtUpBytes(int idx) {
  return s_ytUp[idx];
}

uint32_t nfcFilterYtDownBytes(int idx) {
  return s_ytDown[idx];
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
