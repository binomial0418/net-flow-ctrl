"""Preview the portal locally with sample devices: the real page and the real
API logic (Controller), with nftables and the network faked out. Edits made
on the page work, but live only in a temp file.

    python3 tools/preview.py [port]     then open http://localhost:8099
"""
import json
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from netflow.app import ApiError, Conf, Controller  # noqa: E402
from netflow.clients import Client  # noqa: E402
from netflow.history import day_minus  # noqa: E402

PAGE = (Path(__file__).resolve().parent.parent / "netflow" / "page.html").read_bytes()
MB = 1024 * 1024


class FakeNft:
    def __init__(self):
        self.bytes = {"acct_up": {}, "acct_down": {}, "acct_yt_up": {}, "acct_yt_down": {}}

    def counters(self):
        return {s: dict(v) for s, v in self.bytes.items()}

    def apply_policy(self, p):
        pass


CLIENTS = {
    "1c:53:f9:16:66:68": Client("1c:53:f9:16:66:68", "192.168.50.101", "lv-chrome", True),
    "c8:1f:e8:59:40:eb": Client("c8:1f:e8:59:40:eb", "192.168.50.102", "Google-TV-Box", True),
    "26:85:f8:1c:8b:5d": Client("26:85:f8:1c:8b:5d", "192.168.50.103", "Pixel-7", True),
    "76:fa:4e:ca:48:87": Client("76:fa:4e:ca:48:87", "", "", False),
}


def build() -> Controller:
    tmp = Path(tempfile.mkdtemp())
    nft = FakeNft()
    ctl = Controller(
        Conf(state_path=str(tmp / "state.json")), nft, snapshot=lambda: CLIENTS, uplink=lambda: True
    )
    ctl.tick()
    d = ctl.st.devices
    tv, box, phone, old = (d[m] for m in CLIENTS)
    tv.name, tv.quota_enabled, tv.quota_min, tv.used_sec, tv.yt_used_sec = "客廳電視", True, 180, 2 * 3600 + 35 * 60, 5400
    tv.win_enabled, tv.win_start, tv.win_end = True, 6 * 60, 21 * 60
    tv.up_bytes, tv.down_bytes = 85 * MB, 6200 * MB
    box.name, box.quota_enabled, box.quota_min, box.used_sec, box.yt_used_sec = "房間電視盒", True, 120, 7200, 3900
    box.yt_only_limit, box.up_bytes, box.down_bytes = True, 40 * MB, 3100 * MB
    phone.name, phone.block_youtube, phone.used_sec = "小明的手機", True, 1500
    phone.up_bytes, phone.down_bytes = 12 * MB, 230 * MB
    old.name, old.approved = "未知裝置", False
    now = datetime.now()
    tv.extend_min, tv.extend_day = min(1439, now.hour * 60 + now.minute + 40), ctl.st.day_key
    ctl.rt["1c:53:f9:16:66:68"].yt_warn = False
    ctl.rt["c8:1f:e8:59:40:eb"].yt_warn = True  # show the recognition warning
    ctl.sync()
    # Sample viewing history (today and earlier in the week).
    day, yt, h = ctl.st.day_key, "com.google.android.youtube.tv", ctl.history
    tv_mac, box_mac = "1c:53:f9:16:66:68", "c8:1f:e8:59:40:eb"
    for d, mac, pkg, ch, title, secs in [
        (day, tv_mac, yt, "台南Josh", "季後挑戰賽預測！中信有1勝優勢就穩了嗎？", 1820),
        (day, tv_mac, yt, "台南Josh", "統一雙王牌能不能扳回劣勢？", 950),
        (day, tv_mac, yt, "蔡阿嘎", "開箱最新遊戲機", 1300),
        (day, tv_mac, "com.spotify.tv.android", "老師不正經", "《EP198｜老師，悅讀越奇怪！》霸王壞壞鵝", 2400),
        (day, box_mac, "com.google.android.youtube.tvkids", "寶寶巴士", "交通工具兒歌", 3100),
        (day_minus(day, 3), tv_mac, yt, "這群人", "上週的影片", 2200),
    ]:
        h.add(d, mac, pkg, ch, title, secs, time.time())
    ctl.rt[tv_mac].now_playing = [{"package": yt, "state": "playing", "title": "季後挑戰賽預測！", "artist": "台南Josh"}]
    ctl.rt[tv_mac].now_playing_at = time.time() + 10**6  # keep it shown in the preview
    return ctl


ctl = build()
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        with lock:
            if self.path.startswith("/api/history"):
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.path).query)
                return self._json(200, ctl.watch_history(int(q.get("days", ["1"])[0]), q.get("mac", [""])[0]))
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(PAGE)
            elif self.path == "/api/status":
                self._json(200, ctl.status())
            elif self.path == "/api/devices":
                self._json(200, ctl.devices())
            else:
                self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        routes = {"/api/device": ctl.update_device, "/api/global": ctl.update_global, "/api/extend": ctl.extend}
        with lock:
            try:
                if self.path == "/api/reset-usage":
                    ctl.reset_usage()
                elif self.path in routes:
                    routes[self.path](body)
                else:
                    return self.send_error(404)
            except ApiError as e:
                self.send_response(e.status)
                self.end_headers()
                self.wfile.write(e.text.encode())
                return
        self._json(200, {"ok": True})

    def log_message(self, *a):
        pass


def ticker():
    while True:
        time.sleep(1)
        with lock:
            ctl.tick()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    threading.Thread(target=ticker, daemon=True).start()
    print(f"preview on http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
