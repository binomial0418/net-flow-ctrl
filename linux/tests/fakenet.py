"""Helpers for integration_netns.sh: a fake internet (DNS + HTTP + a DoT port)
and the client-side probes run from the fake TV."""
import json
import socket
import struct
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VIDEO_IP = "173.194.9.9"
SITE_IP = "93.184.0.10"
YT_FRONT_IP = "93.184.0.20"


def qname(name):
    return b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"


def parse_qname(msg, off=12):
    labels = []
    while msg[off]:
        n = msg[off]
        labels.append(msg[off + 1 : off + 1 + n].decode())
        off += 1 + n
    return ".".join(labels).lower(), off + 1


def serve():
    def answer_for(name):
        if name.endswith("googlevideo.com"):
            return VIDEO_IP
        if name.endswith("youtube.com"):
            return YT_FRONT_IP
        return SITE_IP

    def dns():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bound to the resolver address itself, so replies come from it, as
        # a real 8.8.8.8 does (an unbound socket would answer from 10.99.0.1).
        s.bind(("8.8.8.8", 53))
        while True:
            q, addr = s.recvfrom(1500)
            name, off = parse_qname(q)
            ip = answer_for(name)
            hdr = q[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, 1, 0, 0)
            rr = b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 300, 4) + socket.inet_aton(ip)
            s.sendto(hdr + q[12 : off + 4] + rr, addr)

    class Http(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"x" * (2 * 1024 * 1024)
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    def dot():
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 853))
        s.listen()
        while True:
            c, _ = s.accept()
            c.close()

    threading.Thread(target=dns, daemon=True).start()
    threading.Thread(target=dot, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 80), Http).serve_forever()


def resolve(name, server="8.8.8.8"):
    q = struct.pack("!HBBHHHH", 0x4242, 1, 0, 1, 0, 0, 0) + qname(name) + struct.pack("!HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    s.sendto(q, (server, 53))
    r, _ = s.recvfrom(1500)
    if r[3] & 0x0F == 3:
        return "NXDOMAIN"
    return socket.inet_ntoa(r[-4:]) if struct.unpack_from("!H", r, 6)[0] else "NOANSWER"


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as r:
            return f"OK {len(r.read())}"
    except Exception as e:
        return f"FAIL {type(e).__name__}"


def connect(host, port):
    s = socket.socket()
    s.settimeout(3)
    try:
        s.connect((host, int(port)))
        return "OPEN"
    except ConnectionRefusedError:
        return "REFUSED"
    except socket.timeout:
        return "TIMEOUT"
    except OSError as e:
        return f"ERR {e.errno}"


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://192.168.50.1" + path, data=data, method=method)
    with urllib.request.urlopen(req, timeout=4) as r:
        return r.read().decode()


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "serve":
        serve()
    elif cmd == "resolve":
        print(resolve(*args))
    elif cmd == "get":
        print(get(*args))
    elif cmd == "connect":
        print(connect(*args))
    elif cmd == "api":
        print(api(args[0], args[1], json.loads(args[2]) if len(args) > 2 else None))
