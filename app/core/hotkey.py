"""全局热键：RegisterHotKey + 独立消息循环线程（不依赖键盘钩子）。"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008

VK = {
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "ESC": 0x1B, "HOME": 0x24, "END": 0x23,
}


class HotkeyManager(threading.Thread):
    """register: {热键名: (修饰键组合, 回调)}，例如 {'F9': (0, on_start)}"""

    def __init__(self, bindings: dict[str, tuple[int, callable]]):
        super().__init__(daemon=True)
        self.bindings = bindings
        self._stop_event = threading.Event()
        self._ids: dict[int, callable] = {}

    def run(self) -> None:
        for idx, (name, (mods, cb)) in enumerate(self.bindings.items(), start=1):
            vk = VK.get(name.upper())
            if vk is None:
                continue
            if user32.RegisterHotKey(None, idx, mods, vk):
                self._ids[idx] = cb
        msg = wt.MSG()
        while not self._stop_event.is_set():
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0 or ret == -1:
                break
            if msg.message == WM_HOTKEY:
                cb = self._ids.get(int(msg.wParam))
                if cb:
                    threading.Thread(target=cb, daemon=True).start()
        for idx in list(self._ids):
            user32.UnregisterHotKey(None, idx)
        self._ids.clear()

    def stop(self) -> None:
        self._stop_event.set()
        tid = self.native_id
        if tid:
            user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT
