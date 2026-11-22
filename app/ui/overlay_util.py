"""全屏遮罩（取点 / 框选）共用的"Esc 一定能退出"支持。

为什么需要：全屏遮罩是无边框 + 置顶 + Tool 窗口，弹出时不一定拿得到键盘焦点
（Windows 的前台锁、目标程序抢先、或用户还没点过这块遮罩），于是 keyPressEvent
收不到 Esc，界面就像"卡住了"。这里不依赖焦点，直接定时查询系统级按键状态
（GetAsyncKeyState），无论焦点在谁身上按 Esc 都能关掉遮罩。
"""
from __future__ import annotations

import ctypes

from PySide6.QtCore import Qt, QTimer

VK_ESCAPE = 0x1B

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
except Exception:  # noqa: BLE001 - 非 Windows 环境（如离屏测试）时静默降级
    _user32 = None


def esc_down() -> bool:
    """系统范围内 Esc 是否正被按住（与焦点无关）。"""
    if _user32 is None:
        return False
    try:
        return bool(_user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def prepare_overlay(widget) -> None:
    """让遮罩有机会拿到键盘（有焦点时 keyPressEvent 也能正常工作）。"""
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    widget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)


def activate_overlay(widget) -> None:
    """遮罩显示后主动抢一次焦点（抢不到也没关系，EscWatch 兜底）。"""
    try:
        widget.raise_()
        widget.activateWindow()
        widget.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        widget.grabKeyboard()
    except Exception:  # noqa: BLE001
        pass


def release_overlay(widget) -> None:
    try:
        widget.releaseKeyboard()
    except Exception:  # noqa: BLE001
        pass


class EscWatch:
    """轮询系统 Esc 键，按下一次就回调 on_esc（30ms 一次，几乎无感）。"""

    def __init__(self, widget, on_esc, interval_ms: int = 30):
        self._on_esc = on_esc
        self._was_down = False
        self._timer = QTimer(widget)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._was_down = esc_down()      # 打开瞬间已经按着 Esc，先不当作一次"按下"
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        down = esc_down()
        if down and not self._was_down:
            self._was_down = True
            self._on_esc()
        elif not down:
            self._was_down = False
