"""程序入口：python main.py。"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from core import actions  # noqa: E402

actions.set_dpi_aware()

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from config import APP_NAME, LOG_DIR, asset_path  # noqa: E402
from ui.theme import apply_theme  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with (LOG_DIR / "crash.log").open("a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass
    try:
        QMessageBox.critical(None, "程序出错", text[-1500:])
    except Exception:
        pass


def _selftest() -> int:
    """--selftest：不弹窗，直接验证截屏 / OCR / 找图是否可用（打包后也适用）。"""
    import json
    import traceback

    from config import DATA_DIR

    from core.capture import GRABBER
    from core.ocr_engine import OCR
    from core.vision import find_template

    out: dict = {}
    try:
        img, origin = GRABBER.grab_full()
        out["screen"] = {"h": int(img.shape[0]), "w": int(img.shape[1]), "origin": list(origin)}

        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 30)
        canvas = Image.new("RGB", (900, 320), "#f5f5f5")
        d = ImageDraw.Draw(canvas)
        d.text((50, 40), "开始游戏    设置    确定", font=font, fill="#111111")
        d.text((50, 120), "Hello Screen 2026", font=font, fill="#333333")
        import cv2
        import numpy as np

        bgr = cv2.cvtColor(np.array(canvas), cv2.COLOR_RGB2BGR)
        items = OCR.recognize(bgr, min_score=0.5)
        out["ocr"] = [t.text for t in items]
        out["ocr_ok"] = all(k in " ".join(out["ocr"]) for k in ("开始游戏", "设置", "确定"))

        tpl = bgr[20:90, 300:520].copy()
        hits = find_template(bgr, tpl, None, threshold=0.8, multi_scale=True)
        out["match_count"] = len(hits)
        out["match_ok"] = len(hits) >= 1
        out["ok"] = bool(out.get("ocr_ok") and out.get("match_ok"))
    except Exception:
        out["error"] = traceback.format_exc()
        out["ok"] = False

    (DATA_DIR / "selftest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if out.get("ok") else 1


def _selftest_esc() -> int:
    """--selftest-esc：验证取点遮罩"按 Esc 一定能退出"（打包后也适用，会闪一下全屏遮罩）。"""
    import json
    import threading
    import time

    from config import DATA_DIR

    from PySide6.QtCore import QTimer

    from ui.point_picker import PointPicker

    app = QApplication(sys.argv)
    out = {"ok": False}
    dlg = PointPicker()
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

    def press_esc() -> None:
        import ctypes

        u = ctypes.WinDLL("user32", use_last_error=True)
        u.keybd_event(0x1B, 0, 0, 0)
        time.sleep(0.08)
        u.keybd_event(0x1B, 0, 2, 0)

    QTimer.singleShot(500, lambda: threading.Thread(target=press_esc, daemon=True).start())
    t0 = time.time()
    dlg.exec()
    out["canceled"] = getattr(dlg, "point_phys", None) is None
    out["seconds"] = round(time.time() - t0, 2)
    out["ok"] = bool(out["canceled"] and out["seconds"] < 3.0)
    (DATA_DIR / "esc_selftest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if out["ok"] else 1


def main() -> int:
    sys.excepthook = _excepthook
    if "--selftest" in sys.argv:
        return _selftest()
    if "--selftest-esc" in sys.argv:
        return _selftest_esc()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    icon_path = asset_path("icon.png")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_theme(app)

    win = MainWindow()
    if "--offscreen" in sys.argv:      # 仅用于自动化测试，不弹到用户屏幕上
        win.move(-3600, -3600)
    win.show()

    def warmup() -> None:
        try:
            from core.ocr_engine import OCR

            OCR.warmup()
        except Exception as exc:  # noqa: BLE001
            win._log("warn", f"文字识别模型加载失败：{exc}")

    QTimer.singleShot(900, warmup)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
