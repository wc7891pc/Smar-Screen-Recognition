"""屏幕抓取：基于 mss，返回 BGR numpy 图像（物理像素坐标）。"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

import mss
import numpy as np


class Grabber:
    """mss 实例不能跨线程共享，这里按线程缓存。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def _sct(self):
        sct = getattr(self._local, "sct", None)
        if sct is None:
            sct = mss.mss()
            self._local.sct = sct
        return sct

    # ---------------- 显示器信息 ----------------
    def monitors(self) -> list[dict]:
        return self._sct().monitors

    def virtual_rect(self) -> Tuple[int, int, int, int]:
        """整个虚拟桌面（多屏拼接）的 (left, top, width, height)，物理像素。"""
        m = self._sct().monitors[0]
        return int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])

    def primary_rect(self) -> Tuple[int, int, int, int]:
        mons = self._sct().monitors
        m = mons[1] if len(mons) > 1 else mons[0]
        return int(m["left"]), int(m["top"]), int(m["width"]), int(m["height"])

    # ---------------- 抓图 ----------------
    def grab(self, region: Optional[Tuple[int, int, int, int]] = None) -> Tuple[np.ndarray, Tuple[int, int]]:
        """抓取区域，返回 (BGR 图像, 区域左上角屏幕坐标)。region 为空则抓整个虚拟桌面。"""
        vx, vy, vw, vh = self.virtual_rect()
        if region is None:
            x, y, w, h = vx, vy, vw, vh
        else:
            x, y, w, h = (int(v) for v in region)
            # 窗口可能有一部分在屏幕外，裁到虚拟桌面内，否则 mss 会报错
            x2, y2 = min(x + w, vx + vw), min(y + h, vy + vh)
            x, y = max(x, vx), max(y, vy)
            w, h = x2 - x, y2 - y
        if w <= 0 or h <= 0:
            raise ValueError("抓取区域无效")
        shot = self._sct().grab({"left": x, "top": y, "width": w, "height": h})
        img = np.asarray(shot)  # BGRA
        bgr = np.ascontiguousarray(img[:, :, :3])
        return bgr, (x, y)

    def grab_full(self) -> Tuple[np.ndarray, Tuple[int, int]]:
        return self.grab(None)


GRABBER = Grabber()
