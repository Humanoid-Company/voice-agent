"""
Адаптери роботів. Кожен реалізує однаковий інтерфейс:
  move(vx, vy, yaw)  — швидкості, м/с і рад/с (вже обрізані bridge'ем)
  stop()
  action(name)
  capabilities       — що надіслати релею при реєстрації
  state()            — dict для статусу на сайті
  hand(side, q, speed) — лише якщо hands = True: кисті з пальцями (R1 EDU з Dex)
"""
from __future__ import annotations  # Python 3.8 на Jetson R1

import os
import subprocess
import threading


class RobotAdapter:
    kind = "generic"
    actions: list[str] = []
    max_vx = 0.5
    max_vy = 0.3
    max_yaw = 0.8
    can_move = True
    hands = False
    gestures: dict[str, list[float]] = {}

    def connect(self): ...
    def move(self, vx: float, vy: float, yaw: float): raise NotImplementedError
    def stop(self): raise NotImplementedError
    def action(self, name: str): raise NotImplementedError
    def state(self) -> dict: return {}
    def hand(self, side: str, q: list[float], speed: float): raise NotImplementedError

    def capabilities(self) -> dict:
        return {
            "kind": self.kind,
            "actions": self.actions,
            "max_speed": {"vx": self.max_vx, "vy": self.max_vy, "yaw": self.max_yaw} if self.can_move else None,
            "can_move": self.can_move,
            "hands": self.hands,
            "gestures": list(self.gestures),
        }


# Пози кистей: великий, поворот великого, вказівний, середній, безіменний, мізинець; 0 — розкрито, 1 — зігнуто.
HAND_GESTURES = {
    "open": [0, 0, 0, 0, 0, 0],
    "fist": [1, 1, 1, 1, 1, 1],
    "grip": [0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
    "point": [1, 1, 0, 1, 1, 1],
    "peace": [1, 1, 0, 0, 1, 1],
    "thumbs_up": [0, 0, 1, 1, 1, 1],
    "three": [1, 1, 0, 0, 0, 1],
    "four": [1, 1, 0, 0, 0, 0],
    "rock": [1, 1, 0, 1, 1, 0],
    "pinch": [0.6, 0.6, 0.6, 0, 0, 0],
}


# ------------------------------------------------------------------
class MockAdapter(RobotAdapter):
    """Емулятор — для перевірки без робота. Друкує команди в консоль."""
    kind = "dog"
    actions = ["stand", "sit", "hello"]

    def __init__(self, kind="dog"):
        self.kind = kind
        if kind == "humanoid":
            self.actions = ["stand", "sit", "wave", "shake_hand"]
            self.max_vx, self.max_vy, self.max_yaw = 0.4, 0.2, 0.6
        self.v = (0.0, 0.0, 0.0)
        self.last_action = None

    def connect(self):
        print(f"[mock:{self.kind}] connected")

    def move(self, vx, vy, yaw):
        self.v = (vx, vy, yaw)
        print(f"[mock] move vx={vx:+.2f} vy={vy:+.2f} yaw={yaw:+.2f}")

    def stop(self):
        if self.v != (0.0, 0.0, 0.0):
            print("[mock] STOP")
        self.v = (0.0, 0.0, 0.0)

    def action(self, name):
        self.last_action = name
        print(f"[mock] action {name}")

    def state(self):
        return {"moving": self.v != (0.0, 0.0, 0.0), "last_action": self.last_action, "battery": 87}


# ------------------------------------------------------------------
class Go2Adapter(RobotAdapter):
    """Unitree Go2 (потрібна EDU або доступ до SDK). pip install unitree_sdk2py"""
    kind = "dog"
    actions = ["stand", "sit", "lie_down", "recovery", "hello", "stretch", "balance"]
    max_vx, max_vy, max_yaw = 0.8, 0.4, 1.0

    def __init__(self, iface: str):
        self.iface = iface
        self.client = None

    def connect(self):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.sport.sport_client import SportClient

        ChannelFactoryInitialize(0, self.iface)
        self.client = SportClient()
        self.client.SetTimeout(5.0)
        self.client.Init()

    def move(self, vx, vy, yaw):
        self.client.Move(vx, vy, yaw)

    def stop(self):
        self.client.StopMove()

    def action(self, name):
        c = self.client
        table = {
            "stand": c.StandUp,
            "sit": c.Sit,
            "lie_down": c.StandDown,
            "recovery": c.RecoveryStand,
            "hello": c.Hello,
            "stretch": c.Stretch,
            "balance": c.BalanceStand,
        }
        if name in table:
            table[name]()


# ------------------------------------------------------------------
class HumanoidAdapter(RobotAdapter):
    """
    Гуманоїди Unitree (G1 / R1). Використовує LocoClient з unitree_sdk2py.
    Для R1 назви методів ще не перевірені — якщо щось падає, дивіться
    приклади з SDK для вашої моделі і правте таблицю в action().
    """
    kind = "humanoid"
    actions = ["stand", "sit", "wave", "shake_hand", "damp"]
    max_vx, max_vy, max_yaw = 0.4, 0.2, 0.6  # гуманоїд — обережніше

    def __init__(self, iface: str):
        self.iface = iface
        self.client = None

    def connect(self):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient

        ChannelFactoryInitialize(0, self.iface)
        self.client = LocoClient()
        self.client.SetTimeout(5.0)
        self.client.Init()

    def move(self, vx, vy, yaw):
        self.client.Move(vx, vy, yaw)

    def stop(self):
        self.client.StopMove()

    def action(self, name):
        c = self.client
        table = {
            "stand": getattr(c, "StandUp", None),
            "sit": getattr(c, "Sit", None),
            "wave": getattr(c, "WaveHand", None),
            "shake_hand": getattr(c, "ShakeHand", None),
            "damp": getattr(c, "Damp", None),
        }
        fn = table.get(name)
        if fn:
            fn()


# ------------------------------------------------------------------
class R1HandsAdapter(RobotAdapter):
    """
    R1 EDU з кистями Dex: керування пальцями через brainco_hand_server на Jetson.
    Тримає запущеною програму hand_stream (robot_bridge/r1/hand_stream.cpp), яка
    один раз підключається до DDS і шле пози в rt/brainco/<left|right>/cmd.
    Ходьби тут немає: джойстик на сайті для цього робота схований.
    """
    kind = "humanoid"
    actions: list[str] = []
    can_move = False
    hands = True
    gestures = HAND_GESTURES

    def __init__(self, stream_path: str | None = None):
        self.path = stream_path or os.environ.get(
            "HAND_STREAM", os.path.expanduser("~/brainco_hand_service/bin/hand_stream"))
        self.proc = None
        self.lock = threading.Lock()
        self.pose = {"left": [0.0] * 6, "right": [0.0] * 6}

    def _ensure(self):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen([self.path], stdin=subprocess.PIPE, cwd=os.path.dirname(self.path) or None,
                                         universal_newlines=True, bufsize=1)

    def connect(self):
        self._ensure()
        print(f"[r1-hands] hand_stream started: {self.path}")

    def move(self, vx, vy, yaw):
        pass

    def stop(self):
        pass  # пальці тримають позу; СТОП стосується ходьби

    def hand(self, side, q, speed):
        sides = ["left", "right"] if side == "both" else [side]
        with self.lock:
            for _attempt in range(2):
                try:
                    self._ensure()
                    for s_ in sides:
                        nums = " ".join(f"{v:.3f}" for v in q)
                        self.proc.stdin.write(f"{s_} {nums} {speed:.3f}\n")
                    self.proc.stdin.flush()
                    break
                except (BrokenPipeError, OSError):
                    self.proc = None  # програма впала — перезапуск і ще одна спроба
            for s_ in sides:
                self.pose[s_] = list(q)

    def state(self):
        alive = self.proc is not None and self.proc.poll() is None
        return {"hands_ok": alive, "pose": self.pose}


# ------------------------------------------------------------------
class MockHandsAdapter(R1HandsAdapter):
    """Емулятор R1 з руками: друкує пози в консоль, без робота."""

    def connect(self):
        print("[mock:r1-hands] connected")

    def hand(self, side, q, speed):
        for s_ in (["left", "right"] if side == "both" else [side]):
            self.pose[s_] = list(q)
        print(f"[mock] hand {side} {q} speed={speed}")

    def state(self):
        return {"hands_ok": True, "pose": self.pose}


def make_adapter(kind: str, iface: str) -> RobotAdapter:
    if kind == "go2":
        return Go2Adapter(iface)
    if kind in ("g1", "r1", "humanoid"):
        return HumanoidAdapter(iface)
    if kind == "mock-dog":
        return MockAdapter("dog")
    if kind == "r1-hands":
        return R1HandsAdapter()
    if kind == "mock-hands":
        return MockHandsAdapter()
    if kind == "mock-humanoid":
        return MockAdapter("humanoid")
    raise ValueError(f"unknown robot kind: {kind}")
