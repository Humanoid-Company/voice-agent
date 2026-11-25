#!/usr/bin/env python3
"""Hand control page for the R1 EDU (Dex hands) — open it from a phone.

    sudo python3 hand_web.py            # then http://<jetson-ip>:8090

Needs brainco_hand_server running (sudo ./brainco_hand_server -n eth10).
Only the Python standard library is used.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
DEFAULT_STREAM = os.path.expanduser("~unitree/brainco_hand_service/bin/hand_stream")

GESTURES = {
    "open": [0, 0, 0, 0, 0, 0],
    "fist": [1, 1, 1, 1, 1, 1],
    "grip": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
    "point": [1, 1, 0, 1, 1, 1],
    "peace": [1, 1, 0, 0, 1, 1],
    "thumbs_up": [0, 0, 1, 1, 1, 1],
    "rock": [1, 1, 0, 1, 1, 0],
    "three": [1, 1, 0, 0, 0, 1],
    "four": [1, 1, 0, 0, 0, 0],
    "pinch": [0.6, 0.6, 0.6, 0, 0, 0],
}


def clamp01(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    if v != v:  # NaN
        return 0.0
    return min(1.0, max(0.0, v))


class HandStream:
    """Runs hand_stream and feeds it poses; restarts it if it dies."""

    def __init__(self, cmd):
        self.cmd = cmd
        self.proc = None
        self.lock = threading.Lock()
        self.pose = {"left": [0.0] * 6, "right": [0.0] * 6}
        self.speed = 0.6
        self.last_error = ""

    def _ensure(self):
        if self.proc and self.proc.poll() is None:
            return True
        try:
            cwd = os.path.dirname(self.cmd[0]) or None
            self.proc = subprocess.Popen(
                self.cmd, stdin=subprocess.PIPE, cwd=cwd,
                universal_newlines=True, bufsize=1)
            self.last_error = ""
            return True
        except OSError as e:
            self.last_error = f"cannot start {self.cmd[0]}: {e}"
            self.proc = None
            return False

    def send(self, side, q, speed):
        q = [round(clamp01(v), 3) for v in q[:6]]
        if len(q) != 6:
            raise ValueError("need 6 values")
        speed = max(0.05, clamp01(speed))
        sides = ["left", "right"] if side == "both" else [side]
        with self.lock:
            if not self._ensure():
                return False
            try:
                for s in sides:
                    nums = " ".join(f"{v:.3f}" for v in q)
                    self.proc.stdin.write(f"{s} {nums} {speed:.3f}\n")
                    self.pose[s] = q
                self.proc.stdin.flush()
                self.speed = speed
                return True
            except (BrokenPipeError, OSError) as e:
                self.last_error = f"hand_stream stopped: {e}"
                self.proc = None
                return False

    def state(self):
        with self.lock:
            alive = bool(self.proc and self.proc.poll() is None)
            return {"stream": alive, "error": self.last_error,
                    "pose": self.pose, "speed": self.speed,
                    "gestures": sorted(GESTURES)}

    def stop(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.proc.stdin.write("quit\n")
                    self.proc.stdin.flush()
                except OSError:
                    pass
                time.sleep(0.2)
                self.proc.terminate()


STREAM = None


class Handler(BaseHTTPRequestHandler):
    server_version = "R1Hands/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(PAGE, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(200, STREAM.state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/api/hand":
            return self._json(404, {"error": "not found"})
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 4096)
            data = json.loads(self.rfile.read(n) or b"{}")
            side = data.get("side", "both")
            if side not in ("left", "right", "both"):
                raise ValueError("side must be left, right or both")
            if "gesture" in data:
                q = GESTURES.get(data["gesture"])
                if q is None:
                    raise ValueError("unknown gesture")
            else:
                q = data.get("q")
                if not isinstance(q, list) or len(q) != 6:
                    raise ValueError("q must be 6 numbers 0..1")
            ok = STREAM.send(side, q, data.get("speed", 0.6))
        except (ValueError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        self._json(200 if ok else 503, STREAM.state())


def local_ips():
    ips = set()
    try:
        out = subprocess.check_output(["hostname", "-I"], universal_newlines=True)
        ips.update(out.split())
    except (OSError, subprocess.CalledProcessError):
        pass
    if not ips:
        ips.add(socket.gethostbyname(socket.gethostname()))
    return sorted(ip for ip in ips if ":" not in ip)


def main():
    global STREAM
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--stream", default=os.environ.get("HAND_STREAM", DEFAULT_STREAM),
                    help="path to the hand_stream program")
    args = ap.parse_args()

    STREAM = HandStream([args.stream])
    STREAM._ensure()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("Hand control page:")
    for ip in local_ips():
        print(f"   http://{ip}:{args.port}")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        STREAM.stop()


if __name__ == "__main__":
    main()
