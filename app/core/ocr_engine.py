"""文字识别（完全本地）：基于 RapidOCR（PP-OCR onnx 模型，随包离线运行）。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Sequence

import numpy as np


@dataclass
class TextItem:
    text: str
    score: float
    box: List[List[float]]   # 4 个角点
    rect: tuple              # (x, y, w, h) 物理像素

    @property
    def center(self) -> tuple:
        x, y, w, h = self.rect
        return (int(x + w / 2), int(y + h / 2))

    def to_dict(self) -> dict:
        return {"text": self.text, "score": self.score, "rect": list(self.rect)}


def _box_to_rect(box) -> tuple:
    pts = np.asarray(box, dtype=float).reshape(-1, 2)
    x1, y1 = pts[:, 0].min(), pts[:, 1].min()
    x2, y2 = pts[:, 0].max(), pts[:, 1].max()
    return (int(round(x1)), int(round(y1)), int(round(x2 - x1)), int(round(y2 - y1)))


def _sort_reading_order(items: List[TextItem]) -> List[TextItem]:
    """按行分组，行内从左到右，保证结果符合人眼阅读顺序。"""
    if not items:
        return []
    items = sorted(items, key=lambda t: t.rect[1] + t.rect[3] / 2)
    rows: List[List[TextItem]] = []
    for it in items:
        cy = it.rect[1] + it.rect[3] / 2
        h = max(it.rect[3], 8)
        if rows:
            last = rows[-1]
            last_cy = sum(r.rect[1] + r.rect[3] / 2 for r in last) / len(last)
            if abs(cy - last_cy) <= h * 0.6:
                last.append(it)
                continue
        rows.append([it])
    out: List[TextItem] = []
    for row in rows:
        out.extend(sorted(row, key=lambda t: t.rect[0]))
    return out


class OcrEngine:
    """懒加载单例包装，避免启动时等待模型初始化。"""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    def _get(self):
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    from rapidocr_onnxruntime import RapidOCR

                    self._engine = RapidOCR()
                    self._ready = True
        return self._engine

    def warmup(self) -> None:
        self._get()

    def recognize(
        self,
        img_bgr: np.ndarray,
        min_score: float = 0.6,
        origin: tuple = (0, 0),
        scale: float | None = None,
    ) -> List[TextItem]:
        """识别一张 BGR 图像，origin 为该图左上角在屏幕上的物理坐标（用于换算全局坐标）。

        scale > 1 时先把小图放大再识别（秒数、小字号按钮的识别率会明显提升），
        坐标会自动换算回原尺寸，调用方不需要关心。
        """
        engine = self._get()
        eff = 1.0
        if scale is None:
            try:
                from config import SETTINGS

                scale = float(getattr(SETTINGS, "ocr_upscale", 1.0) or 1.0)
            except Exception:  # noqa: BLE001
                scale = 1.0
        src = img_bgr
        if scale and scale > 1.01 and img_bgr.size:
            h, w = img_bgr.shape[:2]
            if min(h, w) < 320 or h < 56:      # 只放大小块区域，整屏放大反而更慢
                import cv2

                src = cv2.resize(img_bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
                eff = scale
        # v1.2：识别前给四周补一圈边（文字贴着图边 / 单独一行放在框里时，检测模型容易漏）
        pad = 24
        if src.size:
            import cv2

            src = cv2.copyMakeBorder(src, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
        result, _elapse = engine(src)
        items: List[TextItem] = []
        if not result:
            return items
        ox, oy = origin
        ox -= int(round(pad / eff))
        oy -= int(round(pad / eff))
        for row in result:
            try:
                box, text, score = row[0], str(row[1]), float(row[2])
            except Exception:
                continue
            if not text.strip() or score < min_score:
                continue
            rect = _box_to_rect(box)
            rect = (
                int(round(rect[0] / eff)) + ox,
                int(round(rect[1] / eff)) + oy,
                max(1, int(round(rect[2] / eff))),
                max(1, int(round(rect[3] / eff))),
            )
            pts = (np.asarray(box, dtype=float).reshape(-1, 2) / eff) + np.array([ox, oy])
            items.append(TextItem(text.strip(), score, pts.tolist(), rect))
        return _sort_reading_order(items)


OCR = OcrEngine()


def normalize(text: str) -> str:
    """匹配用归一化：去空白、全角转半角、忽略大小写。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            ch = chr(code - 0xFEE0)
        if ch in " \t\r\n\u3000":
            continue
        out.append(ch)
    return "".join(out).lower()


def find_text(
    items: Sequence[TextItem],
    keyword: str,
    mode: str = "contains",
    case_sensitive: bool = False,
) -> List[TextItem]:
    kw = keyword if case_sensitive else normalize(keyword)
    hits = []
    for it in items:
        hay = it.text if case_sensitive else normalize(it.text)
        if mode == "equals":
            ok = hay == kw
        elif mode == "regex":
            import re

            try:
                ok = re.search(keyword, it.text, 0 if case_sensitive else re.IGNORECASE) is not None
            except re.error:
                ok = False
        else:
            ok = kw in hay
        if ok:
            hits.append(it)
    return hits
