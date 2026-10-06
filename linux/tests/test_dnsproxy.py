import asyncio
import unittest

from netflow.dnsproxy import DnsProxy
from test_dnsmsg import answer, cname_answer, query


class Proxy(unittest.TestCase):
    def run_answer(self, proxy, q, ip="192.168.50.101"):
        return asyncio.run(proxy.answer(q, ip))

    def make(self, blocked, upstream_reply=None, **kw):
        seen, videos = [], []
        self.forwarded = []

        async def on_video(ip, v):
            videos.extend((ip, *x) for x in v)

        p = DnsProxy(["192.0.2.1"], lambda ip: blocked, on_video, on_query=lambda ip, n: seen.append((ip, n)), **kw)

        async def fake_forward(q):
            self.forwarded.append(q)
            return upstream_reply

        p._forward_udp = fake_forward
        return p, seen, videos

    def test_blocked_gets_nxdomain_without_forwarding(self):
        p, seen, _ = self.make(True, upstream_reply=b"should not be used")
        r = self.run_answer(p, query("m.youtube.com"))
        self.assertEqual(r[3] & 0x0F, 3)
        self.assertEqual(seen, [("192.168.50.101", "m.youtube.com")])

    def test_unblocked_forwards_and_learns_video(self):
        reply = answer("rr1.googlevideo.com", [(1, bytes([1, 2, 3, 4]))])
        p, _, videos = self.make(False, upstream_reply=reply)
        self.assertEqual(self.run_answer(p, query("rr1.googlevideo.com")), reply)
        self.assertEqual(videos, [("192.168.50.101", "1.2.3.4", 300)])

    def test_video_learned_for_the_asking_client(self):
        reply = answer("rr1.googlevideo.com", [(1, bytes([1, 2, 3, 4]))])
        p, _, videos = self.make(False, upstream_reply=reply)
        self.run_answer(p, query("rr1.googlevideo.com"), ip="192.168.50.150")
        self.assertEqual(videos, [("192.168.50.150", "1.2.3.4", 300)])

    def test_cname_into_youtube_is_blocked(self):
        reply = cname_answer("cdn-alias.example.net", "rr5.googlevideo.com", bytes([1, 2, 3, 4]))
        p, _, videos = self.make(True, upstream_reply=reply)
        self.assertEqual(self.run_answer(p, query("cdn-alias.example.net"))[3] & 0x0F, 3)
        self.assertEqual(videos, [])  # nothing handed to a device it is hidden from
        p, _, videos = self.make(False, upstream_reply=reply)
        self.assertEqual(self.run_answer(p, query("cdn-alias.example.net")), reply)
        self.assertEqual(videos, [("192.168.50.101", "1.2.3.4", 60)])

    def test_unreadable_query_fails_closed_under_a_block(self):
        q = bytearray(query("www.youtube.com"))
        q[5] = 2  # two questions: only the first would be checked
        p, _, _ = self.make(True, upstream_reply=b"should not be used")
        self.assertEqual(self.run_answer(p, bytes(q))[3] & 0x0F, 1)  # FORMERR
        self.assertEqual(self.forwarded, [])
        p, _, _ = self.make(False, upstream_reply=b"upstream")
        self.assertEqual(self.run_answer(p, bytes(q)), b"upstream")

    def test_responses_are_not_forwarded(self):
        p, _, _ = self.make(False, upstream_reply=b"upstream")
        self.assertIsNone(self.run_answer(p, answer("www.example.com", [])))
        self.assertEqual(self.forwarded, [])

    def test_enc_dns_names_refused_while_blocking_enc_dns(self):
        on = {"v": True}
        p, _, _ = self.make(False, upstream_reply=b"upstream", enc_dns_blocked=lambda: on["v"])
        self.assertEqual(self.run_answer(p, query("dns.google"))[3] & 0x0F, 3)
        self.assertEqual(self.run_answer(p, query("use-application-dns.net"))[3] & 0x0F, 3)
        self.assertEqual(self.run_answer(p, query("www.google.com")), b"upstream")
        on["v"] = False
        self.assertEqual(self.run_answer(p, query("dns.google")), b"upstream")

    def test_configured_domains(self):
        # YouTube moved its video to a new CDN: listed in netflow.json, it is learned and cut.
        reply = answer("edge7.newytcdn.net", [(1, bytes([5, 6, 7, 8]))])
        p, _, videos = self.make(False, upstream_reply=reply, video_domains=["newytcdn.net"])
        self.run_answer(p, query("edge7.newytcdn.net"))
        self.assertEqual(videos, [("192.168.50.101", "5.6.7.8", 300)])
        p, _, _ = self.make(True, upstream_reply=reply, youtube_domains=["newytcdn.net"])
        self.assertEqual(self.run_answer(p, query("edge7.newytcdn.net"))[3] & 0x0F, 3)


if __name__ == "__main__":
    unittest.main()
