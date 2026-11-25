"""
Bridge: робот <-> бекенд Єви («Роботи», server/robots.py).

  python bridge.py --robot go2 --iface eth0 --id go2-01 --name "Go2"

R1 EDU, лише кисті (на Jetson):  python3 bridge.py --robot r1-hands --id r1-edu --name "R1 EDU"
Для перевірки без робота:  --robot mock-dog, mock-humanoid або mock-hands
Локальний бекенд:          --relay ws://localhost:8000/ws/robot
"""
import argparse
import asyncio
import json
import os
import sys
import time

import websockets
from adapters import make_adapter

DEADMAN = 0.35        # с без команд руху -> стоп
HEARTBEAT = 1.0       # с між heartbeat


def clamp(v, lim):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(-lim, min(lim, v))


class Bridge:
    def __init__(self, args):
        self.args = args
        self.robot = make_adapter(args.robot, args.iface)
        self.last_move = 0.0
        self.moving = False

    async def run(self):
        self.robot.connect()
        # Пароль потрібен, лише якщо на Render задано ROBOT_TOKEN.
        url = f"{self.args.relay}?token={self.args.token}" if self.args.token else self.args.relay
        while True:
            try:
                async with websockets.connect(url, ping_interval=10) as ws:
                    print(f"[bridge] connected to relay as {self.args.id}")
                    await ws.send(json.dumps({
                        "type": "register",
                        "robot_id": self.args.id,
                        "name": self.args.name or self.args.id,
                        **self.robot.capabilities(),
                    }))
                    await asyncio.gather(self.recv(ws), self.heartbeat(ws), self.watchdog())
            except Exception as e:
                self.safe_stop()
                print(f"[bridge] relay lost ({e}), retry in 2s")
                await asyncio.sleep(2)

    async def recv(self, ws):
        lim = self.robot.capabilities()["max_speed"] or {"vx": 0, "vy": 0, "yaw": 0}
        async for raw in ws:
            msg = json.loads(raw)
            t = msg.get("type")
            if t == "hand" and self.robot.hands:
                self.on_hand(msg)
            elif t == "move" and self.robot.can_move:
                vx = clamp(msg.get("vx"), lim["vx"])
                vy = clamp(msg.get("vy"), lim["vy"])
                yaw = clamp(msg.get("yaw"), lim["yaw"])
                self.last_move = time.monotonic()
                if vx == vy == yaw == 0:
                    self.safe_stop()
                else:
                    self.robot.move(vx, vy, yaw)
                    self.moving = True
            elif t == "stop":
                self.safe_stop()
            elif t == "action":
                name = msg.get("name")
                if name in self.robot.actions:
                    self.safe_stop()
                    self.robot.action(name)
                    await ws.send(json.dumps({"type": "event", "text": f"виконую: {name}"}))

    def on_hand(self, msg):
        side = msg.get("side")
        if side not in ("left", "right", "both"):
            return
        if "gesture" in msg:
            q = self.robot.gestures.get(msg["gesture"])
            if q is None:
                return
        else:
            q = msg.get("q")
            if not isinstance(q, list) or len(q) != 6:
                return
        q = [max(0.0, min(1.0, clamp(v, 1))) for v in q]  # bridge обрізає сам, не покладаючись на сервер
        speed = max(0.05, min(1.0, clamp(msg.get("speed", 0.6), 1)))
        try:
            self.robot.hand(side, q, speed)
        except Exception as e:
            print(f"[bridge] hand failed: {e}")

    async def heartbeat(self, ws):
        while True:
            await ws.send(json.dumps({"type": "heartbeat", "state": self.robot.state()}))
            await asyncio.sleep(HEARTBEAT)

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.05)
            if self.moving and time.monotonic() - self.last_move > DEADMAN:
                print("[bridge] deadman -> stop")
                self.safe_stop()

    def safe_stop(self):
        try:
            self.robot.stop()
        except Exception as e:
            print(f"[bridge] stop failed: {e}")
        self.moving = False


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)  # лог видно одразу, навіть у файлі чи systemd
    p = argparse.ArgumentParser()
    p.add_argument("--robot", required=True, help="go2 | r1 | g1 | r1-hands | mock-dog | mock-humanoid | mock-hands")
    p.add_argument("--iface", default="eth0", help="мережевий інтерфейс до робота")
    p.add_argument("--id", required=True, help="унікальний robot_id, напр. go2-01")
    p.add_argument("--name", default=None, help="назва на сайті")
    p.add_argument("--relay", default=os.getenv("ROBOT_RELAY", "wss://voice-agent-f3m4.onrender.com/ws/robot"))
    p.add_argument("--token", default=os.getenv("ROBOT_TOKEN", ""), help="лише якщо на Render задано ROBOT_TOKEN")
    asyncio.run(Bridge(p.parse_args()).run())
