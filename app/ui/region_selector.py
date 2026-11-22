"""全屏冻结截图 + 拖动框选（框选识别区域、截取图案模板共用）。"""
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


class ScreenSelector(QDialog):
    """覆盖整个虚拟桌面的框选器。返回 rect_phys（物理像素，屏幕绝对坐标）。"""

    def __init__(self, hint: str = "按住鼠标左键拖动框选，Esc / 右键取消", parent=None):
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

        # 物理尺寸 / 逻辑尺寸 → 缩放系数（应对系统缩放不是 100% 的情况）
        self._sx = self._bgr.shape[1] / max(self.v_rect.width(), 1)
        self._sy = self._bgr.shape[0] / max(self.v_rect.height(), 1)

        self.setGeometry(self.v_rect)
        self._start: Optional[QPoint] = None
        self._cur: Optional[QPoint] = None
        self.rect_phys: Optional[Tuple[int, int, int, int]] = None
        self.hint = hint
        self._done = False
        # 不依赖键盘焦点：系统级轮询 Esc，遮罩没被激活时也能退出
        self._esc = EscWatch(self, self._cancel)

    # ------------------------------------------------------------------
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
        self.rect_phys = None
        self.reject()

    def _finish(self, rect: Tuple[int, int, int, int]) -> None:
        if self._done:
            return
        self._done = True
        self.rect_phys = rect
        # 先把图裁好存进 Python 属性：对话框关闭后 C++ 对象会被销毁，
        # 之后再调用 selected_image() 就不安全了。
        try:
            x, y, w, h = rect
            ix, iy = x - self._origin[0], y - self._origin[1]
            self.result_image = self._bgr[iy:iy + h, ix:ix + w].copy()
        except Exception:  # noqa: BLE001
            self.result_image = None
        self.accept()

    # ------------------------------------------------------------------
    def paintEvent(self, _ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        target = self.rect()
        p.drawPixmap(target, QPixmap.fromImage(self._image))

        sel = self._sel_logical()
        mask = QColor(0, 0, 0, 110)
        for r in [
            QRect(0, 0, self.width(), sel.top()),
            QRect(0, sel.bottom() + 1, self.width(), self.height() - sel.bottom() - 1),
            QRect(0, sel.top(), sel.left(), sel.height()),
            QRect(sel.right() + 1, sel.top(), self.width() - sel.right() - 1, sel.height()),
        ]:
            if r.width() > 0 and r.height() > 0:
                p.fillRect(r, mask)

        p.setPen(QPen(QColor("#2dd4bf"), 2))
        p.drawRect(sel)

        if sel.width() > 0 and sel.height() > 0:
            w_phys = int(round(sel.width() * self._sx))
            h_phys = int(round(sel.height() * self._sy))
            label = f"{w_phys} × {h_phys}"
            p.setPen(QColor("#e7ebf2"))
            f = p.font()
            f.setPointSize(10)
            p.setFont(f)
            pos = QPoint(sel.left() + 8, max(sel.top() - 10, 16))
            p.fillRect(QRect(pos.x() - 6, pos.y() - 14, 110, 20), QColor(20, 22, 26, 220))
            p.drawText(pos, label)

        # 提示条
        p.setPen(QColor("#e7ebf2"))
        f = p.font()
        f.setPointSize(11)
        p.setFont(f)
        tip_rect = QRect(self.width() // 2 - 240, 24, 480, 34)
        p.fillRect(tip_rect, QColor(20, 22, 26, 210))
        p.drawText(tip_rect, Qt.AlignmentFlag.AlignCenter, self.hint)

    def _sel_logical(self) -> QRect:
        if self._start is None or self._cur is None:
            return QRect(self._start or QPoint(0, 0), self._start or QPoint(0, 0))
        return QRect(self._start, self._cur).normalized()

    # ------------------------------------------------------------------
    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self._start = ev.pos()
            self._cur = ev.pos()
            self.update()
        elif ev.button() == Qt.MouseButton.RightButton:
            self._cancel()

    def mouseMoveEvent(self, ev) -> None:
        if self._start is not None:
            self._cur = ev.pos()
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() != Qt.MouseButton.LeftButton or self._start is None:
            return
        self._cur = ev.pos()
        sel = self._sel_logical()
        if sel.width() < 4 or sel.height() < 4:
            self._cancel()
            return
        ox, oy = self._origin
        gx, gy = self.geometry().left(), self.geometry().top()
        rect = (
            int(round(ox + (gx + sel.left()) * self._sx)),
            int(round(oy + (gy + sel.top()) * self._sy)),
            int(round(sel.width() * self._sx)),
            int(round(sel.height() * self._sy)),
        )
        self._finish(rect)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(ev)

    # ------------------------------------------------------------------
    def selected_image(self) -> Optional[np.ndarray]:
        image = getattr(self, "result_image", None)
        if image is not None:
            return image
        if not self.rect_phys:
            return None
        x, y, w, h = self.rect_phys
        ix, iy = x - self._origin[0], y - self._origin[1]
        return self._bgr[iy:iy + h, ix:ix + w].copy()


def select_region(parent=None, hint: str = "按住鼠标左键拖动框选，Esc / 右键取消") -> Optional[Tuple[int, int, int, int]]:
    dlg = ScreenSelector(hint, parent)
    dlg.exec()
    return getattr(dlg, "rect_phys", None)


def capture_template(parent=None) -> Optional[np.ndarray]:
    """框选一小块屏幕作为图案模板。"""
    dlg = ScreenSelector("拖动框选要识别的图标 / 按钮 / 图案，Esc / 右键取消", parent)
    dlg.exec()
    return getattr(dlg, "result_image", None)
