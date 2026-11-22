"""鼠标 / 键盘模拟：直接调用 Win32 SendInput，兼容高 DPI 与全屏应用。"""
from __future__ import annotations

import ctypes
import random
import threading
import time
from ctypes import wintypes as wt
from typing import Tuple

# v1.2：录制回放（宏）和自动点击规则会同时用鼠标。给"一次完整点击"上锁，
# 保证光标移动 + 按下 + 抬起不会被另一条规则插进来（否则会出现零散的移动/拖拽，点击数对不上）。
MOUSE_LOCK = threading.RLock()

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wt.LONG),
        ("dy", wt.LONG),
        ("mouseData", wt.DWORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wt.WORD),
        ("wScan", wt.WORD),
        ("dwFlags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]


def _send(*inputs: INPUT) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    if not user32.SendInput(n, arr, ctypes.sizeof(INPUT)):
        raise ctypes.WinError(ctypes.get_last_error())


# ----------------------------- 屏幕信息 -----------------------------
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def virtual_screen() -> Tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


def _abs_coord(x: int, y: int) -> Tuple[int, int]:
    vx, vy, vw, vh = virtual_screen()
    dx = int(round((x - vx) * 65535 / max(vw - 1, 1)))
    dy = int(round((y - vy) * 65535 / max(vh - 1, 1)))
    return max(0, min(65535, dx)), max(0, min(65535, dy))


def cursor_pos() -> Tuple[int, int]:
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


# ----------------------------- 鼠标动作 -----------------------------
def move_to(x: int, y: int, steps: int = 1) -> None:
    """平滑移动（steps > 1 时用插值，更像真人，也能触发 hover 效果）。"""
    x, y = int(x), int(y)
    if steps <= 1:
        dx, dy = _abs_coord(x, y)
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
        _send(inp)
        return
    cx, cy = cursor_pos()
    for i in range(1, steps + 1):
        t = i / steps
        nx = int(round(cx + (x - cx) * t))
        ny = int(round(cy + (y - cy) * t))
        dx, dy = _abs_coord(nx, ny)
        inp = INPUT(type=INPUT_MOUSE)
        inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
        _send(inp)
        time.sleep(random.uniform(0.002, 0.006))


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    count: int = 1,
    interval: float = 0.12,
    delay_before: float = 0.0,
    humanize: bool = False,
) -> None:
    """在屏幕物理坐标点击；坐标为空则原地点击。"""
    if delay_before > 0:
        time.sleep(delay_before)
    with MOUSE_LOCK:
        if x is not None and y is not None:
            if humanize:
                move_to(int(x + random.randint(-2, 2)), int(y + random.randint(-2, 2)), steps=random.randint(2, 5))
                time.sleep(random.uniform(0.02, 0.07))
            else:
                move_to(x, y)
                time.sleep(0.02)
        down_f, up_f = _BUTTONS.get(button, _BUTTONS["left"])
        for i in range(max(1, count)):
            d = INPUT(type=INPUT_MOUSE)
            d.mi = MOUSEINPUT(0, 0, 0, down_f, 0, 0)
            u = INPUT(type=INPUT_MOUSE)
            u.mi = MOUSEINPUT(0, 0, 0, up_f, 0, 0)
            _send(d, u)
            if i < count - 1:
                time.sleep(interval)


def double_click(x=None, y=None, button="left", interval=0.14, humanize=False) -> None:
    click(x, y, button, count=2, interval=interval, humanize=humanize)


def drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.4, button: str = "left") -> None:
    with MOUSE_LOCK:
        move_to(x1, y1)
        time.sleep(0.05)
        down_f, up_f = _BUTTONS.get(button, _BUTTONS["left"])
        d = INPUT(type=INPUT_MOUSE)
        d.mi = MOUSEINPUT(0, 0, 0, down_f, 0, 0)
        _send(d)
        steps = max(6, int(duration / 0.01))
        for i in range(1, steps + 1):
            t = i / steps
            nx = int(x1 + (x2 - x1) * t)
            ny = int(y1 + (y2 - y1) * t)
            dx, dy = _abs_coord(nx, ny)
            mv = INPUT(type=INPUT_MOUSE)
            mv.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, 0, 0)
            _send(mv)
            time.sleep(0.008)
        u = INPUT(type=INPUT_MOUSE)
        u.mi = MOUSEINPUT(0, 0, 0, up_f, 0, 0)
        _send(u)


def scroll(amount: int, horizontal: bool = False) -> None:
    """amount 正数向上/向右滚动。"""
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(0, 0, ctypes.c_ulong(amount * 120 & 0xFFFFFFFF).value, flag, 0, 0)
    _send(inp)


def mouse_button(button: str, down: bool) -> None:
    """单独按下 / 抬起某个鼠标键（录制回放用：不移动光标）。"""
    down_f, up_f = _BUTTONS.get(button, _BUTTONS["left"])
    inp = INPUT(type=INPUT_MOUSE)
    inp.mi = MOUSEINPUT(0, 0, 0, down_f if down else up_f, 0, 0)
    _send(inp)


def key_vk(vk: int, up: bool = False) -> None:
    """直接按 VK 码发一次键事件（录制回放用）。"""
    if not vk:
        return
    _send(_key_event(int(vk), up))


# ----------------------------- 键盘动作 -----------------------------
VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D, "shift": 0x10,
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12, "pause": 0x13,
    "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20, " ": 0x20,
    "pgup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C,
    "numlock": 0x90, "scrolllock": 0x91, "prtsc": 0xA0,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE,
    "semicolon": 0xBA, "slash": 0xBF, "backslash": 0xDC,
}
for _c in "abcdefghijklmnopqrstuvwxyz0123456789":
    VK[_c] = ord(_c.upper())


def _vk_of(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        return VK.get(key, ord(key.upper()))
    raise ValueError(f"未知按键: {name}")


def _key_event(vk: int, up: bool = False) -> INPUT:
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if up else 0, 0, 0)
    return inp


def press_key(key: str, hold: float = 0.03) -> None:
    vk = _vk_of(key)
    _send(_key_event(vk), )
    time.sleep(hold)
    _send(_key_event(vk, True))


def hotkey(combo: str, hold: float = 0.02) -> None:
    """组合键，如 "ctrl+shift+s" / "alt+f4"。"""
    parts = [p for p in combo.replace(" ", "").split("+") if p]
    vks = [_vk_of(p) for p in parts]
    for vk in vks:
        _send(_key_event(vk))
        time.sleep(hold)
    for vk in reversed(vks):
        _send(_key_event(vk, True))
        time.sleep(hold)


def type_text(text: str, interval: float = 0.01) -> None:
    """Unicode 方式输入，支持中文与任意字符。"""
    for ch in text:
        units = ch.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i:i + 2], "little")
            down = INPUT(type=INPUT_KEYBOARD)
            down.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0)
            up = INPUT(type=INPUT_KEYBOARD)
            up.ki = KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0)
            _send(down, up)
            if interval > 0:
                time.sleep(interval)


def set_dpi_aware() -> None:
    """尽量声明 DPI 感知，保证屏幕坐标与物理像素一致。"""
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
