import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from netflow.notifier import Notice, Notifier, render_png


class FakeTvOverlay:
    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.calls.append((self.path, json.loads(self.rfile.read(n))))
                body = b'{"success":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


class Sending(unittest.TestCase):
    def test_clock_off_once_then_notify(self):
        tv = FakeTvOverlay()
        try:
            n = Notifier(tv.port, 20)

            async def go():
                await n.send(Notice("127.0.0.1", "還剩 10 分鐘", "客廳電視 今天的上網時間"))
                await n.send(Notice("127.0.0.1", "時間到了", "客廳電視 今天的上網時數已用完"))

            asyncio.run(go())
            paths = [p for p, _ in tv.calls]
            self.assertEqual(paths, ["/set/overlay", "/set/notifications", "/notify", "/notify"])
            self.assertEqual(tv.calls[0][1], {"clockOverlayVisibility": 0})
            self.assertEqual(tv.calls[1][1], {"notificationLayoutName": "Default"})
            body = tv.calls[2][1]
            self.assertEqual(body["duration"], 20)
            if render_png("a", "b") is not None:  # Pillow and the font present: the picture alone
                self.assertEqual(set(body), {"image", "duration"})
            else:  # plain text fallback
                self.assertNotIn("image", body)
                self.assertEqual(body["message"], "還剩 10 分鐘：客廳電視 今天的上網時間")
        finally:
            tv.close()

    def test_unreachable_tv_is_not_an_error(self):
        async def go():
            await Notifier(1, 5).send(Notice("127.0.0.1", "x", "y"))  # nothing listens on port 1

        with self.assertLogs("netflow.notifier", "WARNING"):
            asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
