"""Win32 窗口枚举与操作：把识别/点击限定到某个窗口，并在执行前把它切到前台。

所有坐标都是物理像素（进程已声明 Per-Monitor V2 DPI 感知）。
"""
from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes as wt
from typing import Optional

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# --- 原型声明（64 位下必须写 argtypes，否则句柄会被截断） ---
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ClientToScreen.restype = wt.BOOL
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
user32.GetAncestor.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

kernel32.GetCurrentThreadId.restype = wt.DWORD
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL

SW_RESTORE = 9
GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 这些系统外壳窗口不该出现在"目标窗口"列表里
_SKIP_CLASSES = {
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
    "MenuClass", "tooltips_class32", "SysShadow", "ForegroundStarter",
    "Windows.UI.Core.CoreWindow", "ApplicationFrameWindow_NotUse",
}


def _text(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value.strip()


def _class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _exe_name(hwnd) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def _is_cloaked(hwnd) -> bool:
    val = wt.DWORD(0)
    try:
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        return False
    return bool(val.value)


def _frame_rect(hwnd) -> Optional[tuple[int, int, int, int]]:
    """窗口的可见边框（DWM 值，物理像素）；失败时退回 GetWindowRect。"""
    r = wt.RECT()
    ok = False
    try:
        if dwmapi.DwmGetWindowAttribute(
            hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)
        ) == 0:
            ok = True
    except Exception:
        ok = False
    if not ok:
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
    w, h = r.right - r.left, r.bottom - r.top
    if w < 40 or h < 30:
        return None
    return int(r.left), int(r.top), int(w), int(h)


def list_windows(exclude_pid: Optional[int] = None, min_size: int = 60) -> list[dict]:
    """列出当前可见的顶层窗口（按 Z 序，最上面的在前）。

    exclude_pid=None 时排除本进程；传 0 表示不排除任何进程（规则引擎找目标窗口时用）。
    """
    own_pid = 0 if exclude_pid == 0 else (exclude_pid if exclude_pid is not None else os.getpid())
    result: list[dict] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if _class_name(hwnd) in _SKIP_CLASSES:
            return True
        if _is_cloaked(hwnd):
            return True
        title = _text(hwnd)
        if not title:
            return True
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if own_pid and pid.value == own_pid:
            return True
        rect = _frame_rect(hwnd)
        if rect is None or rect[2] < min_size or rect[3] < min_size:
            return True
        result.append({
            "hwnd": int(hwnd) if hwnd else 0,
            "title": title,
            "class": _class_name(hwnd),
            "exe": _exe_name(hwnd),
            "pid": int(pid.value),
            "rect": rect,
            "minimized": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return result


def find_by_title(keyword: str) -> int:
    """按窗口标题关键字（不区分大小写）找窗口，返回 hwnd；找不到返回 0。"""
    kw = (keyword or "").strip().casefold()
    if not kw:
        return 0
    wins = list_windows(exclude_pid=0)
    exact = [w for w in wins if w["title"].casefold() == kw]
    if exact:
        return exact[0]["hwnd"]
    for w in wins:
        if kw in w["title"].casefold():
            return w["hwnd"]
    return 0


def is_alive(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsWindow(hwnd))


def window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    return _frame_rect(hwnd) if hwnd else None


def client_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """窗口客户区（去掉标题栏和边框）在屏幕上的物理像素矩形。"""
    if not hwnd:
        return None
    r = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(r)):
        return window_rect(hwnd)
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 20 or h <= 20:
        return window_rect(hwnd)
    pt = wt.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
        return window_rect(hwnd)
    return int(pt.x), int(pt.y), int(w), int(h)


def rect_for_title(keyword: str, client: bool = True) -> Optional[tuple[int, int, int, int]]:
    hwnd = find_by_title(keyword)
    if not hwnd:
        return None
    return client_rect(hwnd) if client else window_rect(hwnd)


def intersect(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> Optional[tuple[int, int, int, int]]:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[0] + a[2], b[0] + b[2])
    y2 = min(a[1] + a[3], b[1] + b[3])
    if x2 - x1 <= 20 or y2 - y1 <= 20:
        return None
    return x1, y1, x2 - x1, y2 - y1


def activate(hwnd: int, wait: float = 0.35) -> bool:
    """把窗口切到前台并等它真的成为前台窗口。"""
    if not hwnd or not is_alive(hwnd):
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.15)
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    ok = bool(user32.SetForegroundWindow(hwnd))
    if not ok:
        # 前台锁：临时把输入队列挂到当前前台线程上，再抢前台
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        attached = False
        if fg_tid and fg_tid != cur_tid:
            attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.03)
    return user32.GetForegroundWindow() == hwnd


def foreground_is(hwnd: int) -> bool:
    return bool(hwnd) and user32.GetForegroundWindow() == hwnd


# ------------------------- 屏幕某点下面是哪个窗口 -------------------------
def window_at(x: int, y: int) -> int:
    """返回屏幕物理坐标 (x, y) 处最上层的顶层窗口句柄（没有则 0）。"""
    try:
        pt = wt.POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return 0
        root = user32.GetAncestor(hwnd, 2)      # GA_ROOT = 2
        return int(root or hwnd)
    except Exception:  # noqa: BLE001
        return 0


def pid_of(hwnd: int) -> int:
    """窗口所属进程 ID。"""
    if not hwnd:
        return 0
    pid = wt.DWORD(0)
    try:
        user32.GetWindowThreadProcessId(int(hwnd), ctypes.byref(pid))
    except Exception:  # noqa: BLE001
        return 0
    return int(pid.value)


def point_belongs_to_me(x: int, y: int) -> bool:
    """屏幕上这个点此时是不是被本程序自己的窗口盖着（盖着的话直接点会点到自己）。"""
    hwnd = window_at(x, y)
    return bool(hwnd) and pid_of(hwnd) == os.getpid()


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def is_desktop_window(hwnd: int) -> bool:
    """桌面 / 任务栏这类窗口：不需要（也不该）为了点击去激活。"""
    return _class_name(hwnd) in _SHELL_CLASSES


# =====================================================================
# v1.6：多窗口 —— 一次找多个同标题窗口 / 取标题 / 读最小化状态 / 后台截图
# =====================================================================
def title_of(hwnd: int) -> str:
    """窗口标题（实时读取）。"""
    if not hwnd or not is_alive(hwnd):
        return ""
    return _text(hwnd)


def is_minimized(hwnd: int) -> bool:
    return bool(hwnd) and bool(user32.IsIconic(hwnd))


def find_all_by_title(keyword: str) -> list[int]:
    """按标题关键字找窗口。

    - 有标题完全相同的窗口：把这些窗口全返回（两个同名游戏窗口能同时跑）；
    - 否则只返回第一个包含该关键字的窗口（避免手输关键字误抓一堆无关窗口）。
    """
    kw = (keyword or "").strip().casefold()
    if not kw:
        return []
    wins = list_windows(exclude_pid=0)
    exact = [w["hwnd"] for w in wins if w["title"].casefold() == kw]
    if exact:
        return exact
    for w in wins:
        if kw in w["title"].casefold():
            return [w["hwnd"]]
    return []


# ---- GDI / PrintWindow：抓窗口自身画面（窗口被挡住、甚至不在最前面也能识别）----
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, ctypes.c_uint]
user32.PrintWindow.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [
    wt.HDC, wt.HBITMAP, ctypes.c_uint, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
]
gdi32.GetDIBits.restype = ctypes.c_int

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD),
        ("biBitCount", wt.WORD),
        ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 1)]


def _print_bitmap(hwnd: int, w: int, h: int, flags: int):
    """把窗口画到内存位图并取回 numpy BGR（失败返回 None）。"""
    import numpy as np

    if w <= 0 or h <= 0:
        return None
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(screen_dc)
    bmp = gdi32.CreateCompatibleBitmap(screen_dc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    try:
        ok = user32.PrintWindow(hwnd, mem_dc, flags)
        if not ok and flags != (PW_CLIENTONLY | PW_RENDERFULLCONTENT):
            ok = user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
        if not ok:
            return None
        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负数 = 从上到下，省一次翻转
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0      # BI_RGB
        buf_len = w * h * 4
        buf = (ctypes.c_ubyte * buf_len)()
        got = gdi32.GetDIBits(mem_dc, bmp, 0, h, ctypes.byref(buf),
                             ctypes.byref(bmi), 0)
        if got == 0:
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
        return np.ascontiguousarray(arr[:, :, :3])
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            gdi32.SelectObject(mem_dc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)
        except Exception:  # noqa: BLE001
            pass


def _looks_blank(img) -> bool:
    if img is None or img.size == 0:
        return True
    try:
        return float(img.max()) <= 6 and float(img.std()) < 2.0
    except Exception:  # noqa: BLE001
        return False


def capture_client(hwnd: int):
    """抓某个窗口的客户区画面，返回 (BGR 图, 客户区左上角屏幕坐标)；失败返回 None。

    这是"窗口后台截图"：不要求窗口在最前面、也不要求完全可见，
    因此多个窗口可以同时被识别（截图互不打扰）。
    """
    if not hwnd or not is_alive(hwnd):
        return None
    rect = client_rect(hwnd)
    if rect is None:
        return None
    cx, cy, cw, ch = rect
    img = _print_bitmap(hwnd, cw, ch, PW_CLIENTONLY | PW_RENDERFULLCONTENT)
    if _looks_blank(img):
        img = _print_bitmap(hwnd, cw, ch, PW_CLIENTONLY)
    if not _looks_blank(img):
        return img, (cx, cy)

    # 有些程序不支持"只画客户区"，退回到整窗截图再裁出客户区
    wr = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(wr)):
        return None
    ww, wh = wr.right - wr.left, wr.bottom - wr.top
    if ww <= 0 or wh <= 0:
        return None
    for flags in (PW_RENDERFULLCONTENT, 0):
        full = _print_bitmap(hwnd, ww, wh, flags)
        if _looks_blank(full):
            continue
        dx, dy = max(0, cx - int(wr.left)), max(0, cy - int(wr.top))
        crop = full[dy:dy + ch, dx:dx + cw]
        if crop.size:
            return crop.copy(), (cx, cy)
    return None
