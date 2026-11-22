"""屏幕预览 + 识别结果框叠加显示，支持点击回传到真实屏幕坐标。"""
from __future__ import annotations

from typing import List, Optional, Tuple

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

BOX_COLORS = {
    "text": QColor("#60a5fa"),
    "image": QColor("#2dd4bf"),
    "match": QColor("#fbbf24"),
}


def bgr_to_qimage(bgr: np.ndarray) -> QImage:
    h, w = bgr.shape[:2]
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class PreviewWidget(QWidget):
    clicked_phys = Signal(int, int)     # 单击：真实屏幕坐标
    box_activated = Signal(int)         # 双击某个框

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 280)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image: Optional[QImage] = None
        self._origin = (0, 0)
        self._boxes: List[dict] = []
        self._selected = -1
        self._disp = QRect()
        self._scale = 1.0
        self._placeholder = "点击「开始识别」或「框选区域」查看屏幕内容"

    # -------------------------- 数据 --------------------------
    def set_image(self, bgr: np.ndarray, origin: Tuple[int, int]) -> None:
        self._image = bgr_to_qimage(bgr)
        self._origin = (int(origin[0]), int(origin[1]))
        self._build_disp()
        self.update()

    def set_boxes(self, boxes: List[dict]) -> None:
        """boxes: {'rect': (x,y,w,h), 'label': str, 'kind': 'text'|'image'|'match'}"""
        self._boxes = boxes or []
        self._selected = -1
        self.update()

    def select_box(self, index: int) -> None:
        self._selected = index if 0 <= index < len(self._boxes) else -1
        self.update()

    def clear(self) -> None:
        self._image = None
        self._boxes = []
        self.update()

    # -------------------------- 绘制 --------------------------
    def resizeEvent(self, ev) -> None:
        self._build_disp()
        super().resizeEvent(ev)

    def _build_disp(self) -> None:
        if self._image is None:
            self._disp = QRect()
            return
        iw, ih = self._image.width(), self._image.height()
        w, h = self.width(), self.height()
        scale = min(w / iw, h / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        self._disp = QRect((w - dw) // 2, (h - dh) // 2, dw, dh)
        self._scale = scale

    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#0f1115"))
        if self._image is None:
            p.setPen(QColor("#6b7688"))
            f = QFont()
            f.setPointSize(10)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._placeholder)
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawImage(self._disp, self._image)

        f = p.font()
        f.setPointSize(9)
        p.setFont(f)
        for i, box in enumerate(self._boxes):
            r = self._to_widget(box["rect"])
            color = BOX_COLORS.get(box.get("kind", "text"), BOX_COLORS["text"])
            selected = i == self._selected
            p.setPen(QPen(QColor("#fbbf24") if selected else color, 2 if selected else 1.4))
            p.drawRect(r)
            label = str(box.get("label", ""))
            fm = p.fontMetrics()
            if selected and label:
                tw = min(fm.horizontalAdvance(label) + 10, max(r.width(), 90))
                tr = QRect(r.left(), max(r.top() - 18, 0), tw, 17)
                p.fillRect(tr, QColor(0, 0, 0, 210))
                p.setPen(QColor("#fbbf24"))
                p.drawText(tr.adjusted(4, 0, 0, 0),
                           Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                           fm.elidedText(label, Qt.TextElideMode.ElideRight, tw - 9))
            elif not selected:
                # 未选中时只画一个小序号，避免文字标签糊成一片
                num = str(i + 1)
                tw = fm.horizontalAdvance(num) + 8
                tr = QRect(r.left(), r.top(), min(tw, r.width()), 15)
                p.fillRect(tr, QColor(0, 0, 0, 170))
                p.setPen(color)
                p.drawText(tr, Qt.AlignmentFlag.AlignCenter, num)

    def _to_widget(self, rect) -> QRect:
        x, y, w, h = rect
        ox, oy = self._origin
        return QRect(
            int(self._disp.left() + (x - ox) * self._scale),
            int(self._disp.top() + (y - oy) * self._scale),
            max(1, int(w * self._scale)),
            max(1, int(h * self._scale)),
        )

    def _to_phys(self, pos) -> Tuple[int, int]:
        if self._scale <= 0 or self._image is None:
            return (0, 0)
        px = int((pos.x() - self._disp.left()) / self._scale) + self._origin[0]
        py = int((pos.y() - self._disp.top()) / self._scale) + self._origin[1]
        return (px, py)

    # -------------------------- 交互 --------------------------
    def mousePressEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or self._image is None:
            return
        for i, box in enumerate(self._boxes):
            if self._to_widget(box["rect"]).contains(ev.pos()):
                self._selected = i
                self.update()
                break

    def mouseDoubleClickEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or self._image is None:
            return
        for i, box in enumerate(self._boxes):
            if self._to_widget(box["rect"]).contains(ev.pos()):
                self.box_activated.emit(i)
                return
        x, y = self._to_phys(ev.pos())
        self.clicked_phys.emit(x, y)
