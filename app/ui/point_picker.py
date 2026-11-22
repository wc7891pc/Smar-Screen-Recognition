"""全屏取点器：点一下屏幕，返回该点的物理坐标（用于"点击另一个位置"）。

Esc / 右键 / 左上角提示条点击 都可以取消，见 _cancel()。
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QDialog

from core.capture import GRABBER
from ui.overlay_util import EscWatch, activate_overlay, prepare_overlay, release_overlay


def _to_qimage(bgr: np.ndarray) -> QImage:
    h, w = bgr.shape[:2]
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()


class PointPicker(QDialog):
    """覆盖整个虚拟桌面，单击一次即取该点坐标。"""

    def __init__(self, hint: str = "移动鼠标到目标位置，单击取点；Esc / 右键取消", parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        prepare_overlay(self)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._bgr, self._origin = GRABBER.grab_full()
        self._image = _to_qimage(self._bgr)
        self.v_rect = QGuiApplication.primaryScreen().virtualGeometry()
        self._sx = self._bgr.shape[1] / max(self.v_rect.width(), 1)
        self._sy = self._bgr.shape[0] / max(self.v_rect.height(), 1)
        self.setGeometry(self.v_rect)
        self.hint = hint
        self.point_phys: Optional[Tuple[int, int]] = None
        self._pos = QPoint(self.width() // 2, self.height() // 2)
        self._done = False
        # 不依赖键盘焦点：系统级轮询 Esc，遮罩没被激活时也能退出
        self._esc = EscWatch(self, self._cancel)

    # ---------------------------------------------------------------- 生命周期
    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        activate_overlay(self)
        self._esc.start()

    def closeEvent(self, ev) -> None:
        self._esc.stop()
        release_overlay(self)
        super().closeEvent(ev)

    def _cancel(self) -> None:
        if self._done:
            return
        self._done = True
        self.point_phys = None
        self.reject()

    def _finish(self, pt: Tuple[int, int]) -> None:
        if self._done:
            return
        self._done = True
        self.point_phys = pt
        self.accept()

    # ---------------------------------------------------------------- 绘制
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(self.rect(), QPixmap.fromImage(self._image))

        # 十字准线
        p.setPen(QPen(QColor("#2dd4bf"), 2))
        p.drawLine(0, self._pos.y(), self.width(), self._pos.y())
        p.drawLine(self._pos.x(), 0, self._pos.x(), self.height())
        p.setPen(QPen(QColor("#fbbf24"), 1))
        p.drawEllipse(self._pos, 16, 16)

        phys = self._phys_of(self._pos)
        tip = f"{self.hint}    当前：({phys[0]}, {phys[1]})"
        tip_rect = QRect(self.width() // 2 - 340, 24, 680, 34)
        p.fillRect(tip_rect, QColor(20, 22, 26, 210))
        p.setPen(QColor("#e7ebf2"))
        p.drawText(tip_rect, Qt.AlignmentFlag.AlignCenter, tip)

    def _phys_of(self, pos: QPoint) -> Tuple[int, int]:
        ox, oy = self._origin
        gx, gy = self.geometry().left(), self.geometry().top()
        return (
            int(round(ox + (gx + pos.x()) * self._sx)),
            int(round(oy + (gy + pos.y()) * self._sy)),
        )

    # ---------------------------------------------------------------- 输入
    def mouseMoveEvent(self, ev) -> None:
        self._pos = ev.pos()
        self.update()

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._finish(self._phys_of(ev.pos()))
        elif ev.button() == Qt.MouseButton.RightButton:
            self._cancel()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(ev)


def pick_point(parent=None, hint: str = "移动鼠标到目标位置，单击取点；Esc / 右键取消") -> Optional[Tuple[int, int]]:
    dlg = PointPicker(hint, parent)
    dlg.exec()
    return getattr(dlg, "point_phys", None)
