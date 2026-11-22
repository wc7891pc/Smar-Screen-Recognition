"""图案识别（完全本地）：OpenCV 模板匹配，支持多尺度与 Alpha 蒙版。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


@dataclass
class Match:
    x: int
    y: int
    w: int
    h: int
    score: float

    @property
    def center(self) -> Tuple[int, int]:
        return (int(self.x + self.w / 2), int(self.y + self.h / 2))

    @property
    def rect(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h, "score": round(self.score, 4)}


def load_template(path) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """读取模板，返回 BGR 图与可选蒙版（来自 PNG alpha 通道）。"""
    data = np.fromfile(str(path), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError(f"无法读取模板图片: {path}")
    mask = None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3]
        mask = (alpha > 16).astype(np.uint8) * 255
        img = img[:, :, :3]
    elif img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return np.ascontiguousarray(img), mask


def _nms(matches: List[Match], overlap: float = 0.55) -> List[Match]:
    """按分数贪心抑制，避免同一目标被重复标注。"""
    if not matches:
        return []
    kept: List[Match] = []
    for m in sorted(matches, key=lambda x: -x.score):
        c = m.center
        if all(abs(c[0] - k.center[0]) > max(min(m.w, k.w) * overlap, 6) or
               abs(c[1] - k.center[1]) > max(min(m.h, k.h) * overlap, 6) for k in kept):
            kept.append(m)
    return kept


def find_template(
    screen_bgr: np.ndarray,
    template_bgr: np.ndarray,
    mask: Optional[np.ndarray] = None,
    threshold: float = 0.85,
    multi_scale: bool = True,
    max_results: int = 20,
    origin: Tuple[int, int] = (0, 0),
    scales: Optional[Sequence[float]] = None,
) -> List[Match]:
    """在屏幕图中查找模板的所有出现位置。origin 为屏幕图左上角对应的屏幕坐标。"""
    if template_bgr is None or template_bgr.size == 0:
        return []
    th, tw = template_bgr.shape[:2]
    sh, sw = screen_bgr.shape[:2]
    if th > sh or tw > sw:
        return []

    if scales is None:
        scales = np.linspace(0.7, 1.35, 14) if multi_scale else [1.0]

    candidates: List[Match] = []
    ox, oy = origin
    for s in scales:
        w, h = int(round(tw * float(s))), int(round(th * float(s)))
        if w < 8 or h < 8 or w > sw or h > sh:
            continue
        interp = cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR
        tpl_s = cv2.resize(template_bgr, (w, h), interpolation=interp)
        m_s = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST) if mask is not None else None
        try:
            if m_s is not None and m_s.sum() > 0:
                res = cv2.matchTemplate(screen_bgr, tpl_s, cv2.TM_CCORR_NORMED, mask=m_s)
                res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
            else:
                res = cv2.matchTemplate(screen_bgr, tpl_s, cv2.TM_CCOEFF_NORMED)
                res = np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)
        except cv2.error:
            continue

        loc = np.where(res >= threshold)
        if len(loc[0]) == 0:
            continue
        scores = res[loc]
        # 只取分数最高的一批，控制计算量
        order = np.argsort(-scores)[: max(200, max_results * 8)]
        for idx in order:
            y, x = int(loc[0][idx]), int(loc[1][idx])
            candidates.append(
                Match(x + ox, y + oy, int(w), int(h), float(scores[idx]))
            )

    return _nms(candidates)[:max_results]


def crop(img: np.ndarray, rect: Tuple[int, int, int, int]) -> np.ndarray:
    x, y, w, h = rect
    x = max(0, min(x, img.shape[1] - 1))
    y = max(0, min(y, img.shape[0] - 1))
    w = max(1, min(w, img.shape[1] - x))
    h = max(1, min(h, img.shape[0] - y))
    return img[y:y + h, x:x + w].copy()


def save_image(path, img_bgr: np.ndarray) -> None:
    import os

    ext = os.path.splitext(str(path))[1].lower() or ".png"
    ok, buf = cv2.imencode(ext, img_bgr)
    if not ok:
        raise ValueError("图片编码失败")
    with open(str(path), "wb") as f:
        f.write(buf.tobytes())
