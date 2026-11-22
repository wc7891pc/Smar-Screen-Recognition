"""鼠标 / 键盘动作录制与回放：全局低层钩子（WH_MOUSE_LL + WH_KEYBOARD_LL）。

录制结果是一个 JSON 事件列表（相对时间 + 坐标 + 按键），
回放时按原时间轴（可用倍速压缩）把动作通过 SendInput 重新发出去。
"""
from __future__ import annotations

import ctypes
import json
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from config import MACRO_DIR
from core import actions

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

ULONG_PTR = actions.ULONG_PTR
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# x64 下句柄是 64 位，必须声明 argtypes/restype，否则钩子会装不上
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.UnhookWindowsHookEx.restype = wt.BOOL
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wt.POINT),
        ("mouseData", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wt.DWORD),
        ("scanCode", wt.DWORD),
        ("flags", wt.DWORD),
        ("time", wt.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


_BUTTON_BY_MSG = {
    WM_LBUTTONDOWN: ("left", "down"),
    WM_LBUTTONUP: ("left", "up"),
    WM_RBUTTONDOWN: ("right", "down"),
    WM_RBUTTONUP: ("right", "up"),
    WM_MBUTTONDOWN: ("middle", "down"),
    WM_MBUTTONUP: ("middle", "up"),
}


def _now() -> float:
    return time.perf_counter()


class MacroRecorder:
    """全局鼠标 / 键盘录制器。录制时不要同时运行回放。"""

    def __init__(self) -> None:
        self._events: List[dict] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._tid: int = 0
        self._recording = False
        self._t0 = 0.0
        self._last_move: tuple[int, int, float] = (0, 0, 0.0)
        self._playing = False
        self._play_lock = threading.Lock()
        self.skip_injected = False      # True = 忽略程序注入的输入（规则边跑边录时用）
        self._hooks: list = []          # (hook handle, callback ref)
        self._errors: list[str] = []

    # ----------------------------- 录制 -----------------------------
    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        if self._recording:
            return True
        with self._lock:
            self._events = []
        self._errors = []
        self._t0 = _now()
        self._last_move = (0, 0, 0.0)
        self._recording = True
        self._thread = threading.Thread(target=self._hook_loop, name="MacroRecorder", daemon=True)
        self._thread.start()
        for _ in range(20):             # 等钩子装好
            if self._tid:
                break
            time.sleep(0.02)
        return self._recording

    def stop(self) -> List[dict]:
        if not self._recording:
            with self._lock:
                return list(self._events)
        self._recording = False
        tid = self._tid
        if tid:
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(1.0)
        self._thread = None
        self._tid = 0
        with self._lock:
            return list(self._events)

    def _hook_loop(self) -> None:
        self._tid = kernel32.GetCurrentThreadId()
        mouse_cb = HOOKPROC(self._on_mouse)
        kbd_cb = HOOKPROC(self._on_key)
        hmod = kernel32.GetModuleHandleW(None)
        h_mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_cb, hmod, 0)
        h_kbd = user32.SetWindowsHookExW(WH_KEYBOARD_LL, kbd_cb, hmod, 0)
        err = ctypes.get_last_error()
        if not h_mouse:
            self._errors.append(f"鼠标钩子安装失败（错误码 {err}），可能被安全软件拦截")
        if not h_kbd:
            self._errors.append(f"键盘钩子安装失败（错误码 {ctypes.get_last_error()}）")
        self._hooks = [(h_mouse, mouse_cb), (h_kbd, kbd_cb)]
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        for h, _cb in self._hooks:
            if h:
                user32.UnhookWindowsHookEx(h)
        self._hooks = []

    # 钩子回调：只记录，绝不阻塞
    def _on_mouse(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code >= 0 and self._recording and not self._playing:
            try:
                data = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if self.skip_injected and (int(data.flags) & 0x01):   # LLMHF_INJECTED：本程序/其他工具注入的，不录
                    return user32.CallNextHookEx(0, n_code, w_param, l_param)
                x, y = int(data.pt.x), int(data.pt.y)
                dt = _now() - self._t0
                if w_param == WM_MOUSEMOVE:
                    lx, ly, lt = self._last_move
                    if dt - lt > 0.03 or abs(x - lx) + abs(y - ly) > 6:
                        self._last_move = (x, y, dt)
                        self._push({"t": dt, "type": "move", "x": x, "y": y})
                elif w_param in _BUTTON_BY_MSG:
                    button, phase = _BUTTON_BY_MSG[w_param]
                    self._push({"t": dt, "type": "down" if phase == "down" else "up",
                                "button": button, "x": x, "y": y})
                elif w_param in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                    delta = ctypes.c_short((data.mouseData >> 16) & 0xFFFF).value
                    self._push({"t": dt, "type": "wheel", "delta": int(delta),
                                "horizontal": w_param == WM_MOUSEHWHEEL, "x": x, "y": y})
            except Exception:  # noqa: BLE001
                pass
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    def _on_key(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code >= 0 and self._recording and not self._playing:
            try:
                data = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if self.skip_injected and (int(data.flags) & 0x10):   # LLKHF_INJECTED
                    return user32.CallNextHookEx(0, n_code, w_param, l_param)
                vk = int(data.vkCode)
                dt = _now() - self._t0
                if w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    self._push({"t": dt, "type": "key", "vk": vk, "down": True})
                elif w_param in (WM_KEYUP, WM_SYSKEYUP):
                    self._push({"t": dt, "type": "key", "vk": vk, "down": False})
            except Exception:  # noqa: BLE001
                pass
        return user32.CallNextHookEx(0, n_code, w_param, l_param)

    def _push(self, ev: dict) -> None:
        with self._lock:
            self._events.append(ev)

    # ----------------------------- 回放 -----------------------------
    def play(
        self,
        events: Sequence[dict],
        speed: float = 1.0,
        stop_flag: Optional[threading.Event] = None,
    ) -> int:
        """回放事件列表，返回实际执行的事件数。speed 越大越快。"""
        if not events:
            return 0
        speed = max(0.2, min(10.0, float(speed or 1.0)))
        with self._play_lock:            # 同一时刻只允许一套动作在放，避免互相抢鼠标
            self._playing = True
            done = 0
            t0 = _now()
            evs = list(events)
            i = 0
            try:
                while i < len(evs):
                    if stop_flag is not None and stop_flag.is_set():
                        break
                    ev = evs[i]
                    target = t0 + float(ev.get("t", 0.0)) / speed
                    while True:
                        left = target - _now()
                        if left <= 0:
                            break
                        time.sleep(min(left, 0.01))
                    # 把"移过去 + 按下 + 抬起"合成一次原子点击：这样同时跑的自动点击规则
                    # 不会插到中间，把这次点击甩到别的坐标上（表现为点击数少了）。
                    nxt = evs[i + 1] if i + 1 < len(evs) else None
                    pair_up = evs[i + 2] if i + 2 < len(evs) else None
                    is_move = ev.get("type") == "move"
                    if (
                        nxt is not None
                        and nxt.get("type") == "down"
                        and pair_up is not None
                        and pair_up.get("type") == "up"
                        and str(pair_up.get("button", "left")) == str(nxt.get("button", "left"))
                        and abs(float(pair_up.get("t", 0.0)) - float(nxt.get("t", 0.0))) <= 0.6
                    ):
                        with actions.MOUSE_LOCK:
                            if is_move:
                                self._dispatch(ev)
                            self._dispatch(nxt)
                            self._dispatch(pair_up)
                        done += 3 if is_move else 2
                        i += 3 if is_move else 2
                        continue
                    self._dispatch(ev)
                    done += 1
                    i += 1
            finally:
                self._playing = False
            return done

    def _dispatch(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "move":
            actions.move_to(int(ev["x"]), int(ev["y"]))
        elif kind == "down":
            actions.mouse_button(str(ev.get("button", "left")), True)
        elif kind == "up":
            actions.mouse_button(str(ev.get("button", "left")), False)
        elif kind == "wheel":
            actions.scroll(int(ev.get("delta", 0)), horizontal=bool(ev.get("horizontal")))
        elif kind == "key":
            actions.key_vk(int(ev.get("vk", 0)), up=not bool(ev.get("down", True)))

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def errors(self) -> List[str]:
        return list(self._errors)


RECORDER = MacroRecorder()


# ----------------------------- 宏文件管理 -----------------------------
def _safe_name(name: str) -> str:
    keep = [c for c in name.strip() if c not in '\\/:*?"<>|']
    return "".join(keep)[:40] or time.strftime("动作_%Y%m%d_%H%M%S")


def macro_files() -> List[Path]:
    return sorted(MACRO_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)


def macro_path(name: str) -> Path:
    p = Path(name)
    if p.is_absolute() and p.suffix.lower() == ".json":
        return p
    name = name if name.endswith(".json") else f"{name}.json"
    return MACRO_DIR / name


def save_macro(name: str, events: Sequence[dict]) -> Path:
    path = macro_path(_safe_name(name))
    payload = {"version": 2, "name": path.stem, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(events), "events": list(events)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_macro(name: str) -> List[dict]:
    if not name:
        return []
    path = macro_path(name)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if isinstance(data, list):
        return data
    return list(data.get("events", []))


def delete_macro(name: str) -> bool:
    path = macro_path(name)
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def describe(events: Sequence[dict]) -> str:
    """给用户看的摘要：几步、多长、几次点击、几个按键。"""
    if not events:
        return "空动作"
    clicks = sum(1 for e in events if e.get("type") == "down" and e.get("button") == "left")
    keys = sum(1 for e in events if e.get("type") == "key" and e.get("down"))
    dur = max(float(e.get("t", 0.0)) for e in events)
    parts = [f"{len(events)} 个事件", f"{dur:.1f} 秒"]
    if clicks:
        parts.append(f"{clicks} 次点击")
    if keys:
        parts.append(f"{keys} 个按键")
    return " · ".join(parts)


def play_macro_file(
    name: str,
    speed: float = 1.0,
    stop_flag: Optional[threading.Event] = None,
    on_log: Optional[Callable[[str], None]] = None,
) -> int:
    events = load_macro(name)
    if not events:
        if on_log:
            on_log(f"动作文件为空或不存在：{name}")
        return 0
    if on_log:
        on_log(f"开始播放动作「{Path(name).stem}」（{describe(events)}，{speed:.1f}×）")
    return RECORDER.play(events, speed=speed, stop_flag=stop_flag)
