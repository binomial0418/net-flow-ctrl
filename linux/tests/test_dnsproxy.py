import asyncio
import unittest

from netflow.dnsproxy import DnsProxy
from test_dnsmsg import answer, query


class Proxy(unittest.TestCase):
    def run_answer(self, proxy, q, ip="192.168.50.101"):
        return asyncio.run(proxy.answer(q, ip))

    def make(self, blocked, upstream_reply=None, **kw):
        seen, videos = [], []

        async def on_video(v):
            videos.extend(v)

        p = DnsProxy(["192.0.2.1"], lambda ip: blocked, on_video, on_query=lambda ip, n: seen.append((ip, n)), **kw)

        async def fake_forward(q):
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
        self.assertEqual(videos, [("1.2.3.4", 300)])

    def test_configured_domains(self):
        # YouTube moved its video to a new CDN: listed in netflow.json, it is learned and cut.
        reply = answer("edge7.newytcdn.net", [(1, bytes([5, 6, 7, 8]))])
        p, _, videos = self.make(False, upstream_reply=reply, video_domains=["newytcdn.net"])
        self.run_answer(p, query("edge7.newytcdn.net"))
        self.assertEqual(videos, [("5.6.7.8", 300)])
        p, _, _ = self.make(True, upstream_reply=reply, youtube_domains=["newytcdn.net"])
        self.assertEqual(self.run_answer(p, query("edge7.newytcdn.net"))[3] & 0x0F, 3)


if __name__ == "__main__":
    unittest.main()
