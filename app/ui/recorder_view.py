"""录制动作页面：录制鼠标 / 键盘操作，存成动作文件，供规则"看到就播放"。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core import recorder
from config import SETTINGS


class PlayThread(QThread):
    done = Signal(int, str)

    def __init__(self, name: str, speed: float, parent=None):
        super().__init__(parent)
        self.name = name
        self.speed = speed
        self._stop = threading.Event()

    def run(self) -> None:
        try:
            events = recorder.load_macro(self.name)
            n = recorder.RECORDER.play(events, speed=self.speed, stop_flag=self._stop)
            self.done.emit(n, recorder.describe(events))
        except Exception as exc:  # noqa: BLE001
            self.done.emit(0, f"播放失败：{exc}")

    def stop(self) -> None:
        self._stop.set()


class RecorderView(QWidget):
    def __init__(self, log, parent=None):
        super().__init__(parent)
        self._log = log            # callable(level, text)
        self._play: PlayThread | None = None
        self._tick = QTimer(self)
        self._tick.setInterval(250)
        self._tick.timeout.connect(self._update_clock)
        self._t0 = 0.0
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)

        card = QFrame()
        card.setObjectName("Card")
        c = QVBoxLayout(card)
        c.setContentsMargins(16, 14, 16, 14)
        c.setSpacing(8)

        head = QHBoxLayout()
        t = QLabel("录制鼠标动作")
        t.setStyleSheet("font-weight:700;font-size:15px;")
        self.lbl_state = QLabel("空闲")
        self.lbl_state.setObjectName("Pill")
        head.addWidget(t)
        head.addSpacing(10)
        head.addWidget(self.lbl_state)
        head.addStretch(1)
        c.addLayout(head)

        tip = QLabel(
            "① 点「开始录制」（或按 F8）→ 本软件会自动最小化\n"
            "② 到目标窗口上照你想让它做的动作点一遍（点击、滚动、快捷键都行）\n"
            "③ 再按 F8（或点「停止并保存」）→ 自动存成动作文件\n"
            "之后在规则里把动作选成「播放录制动作」，就能做到「看到某个条件就自动跑这套动作」。"
        )
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        c.addWidget(tip)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_rec = QPushButton("开始录制  F8")
        self.btn_rec.setObjectName("Primary")
        self.btn_rec.clicked.connect(self.toggle_record)
        self.btn_stop = QPushButton("停止并保存")
        self.btn_stop.clicked.connect(self._finish_record)
        self.btn_stop.setEnabled(False)
        self.btn_play = QPushButton("播放选中动作")
        self.btn_play.clicked.connect(self.play_selected)
        self.btn_play_stop = QPushButton("停止播放")
        self.btn_play_stop.clicked.connect(self._stop_play)
        self.btn_play_stop.setEnabled(False)
        self.btn_del = QPushButton("删除")
        self.btn_del.setObjectName("Ghost")
        self.btn_del.clicked.connect(self._delete)
        row.addWidget(self.btn_rec)
        row.addWidget(self.btn_stop)
        row.addSpacing(14)
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_play_stop)
        row.addStretch(1)
        row.addWidget(self.btn_del)
        c.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("动作名称"))
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("例如 领取奖励 / 一键登录")
        self.ed_name.setMinimumWidth(220)
        row2.addWidget(self.ed_name, 2)
        row2.addWidget(QLabel("已保存的动作"))
        self.cb_macro = QComboBox()
        self.cb_macro.setMinimumWidth(220)
        self.cb_macro.currentIndexChanged.connect(self._show_current)
        row2.addWidget(self.cb_macro, 2)
        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh)
        row2.addWidget(btn_refresh)
        row2.addWidget(QLabel("倍速"))
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.2, 10.0)
        self.sp_speed.setSingleStep(0.5)
        self.sp_speed.setDecimals(1)
        self.sp_speed.setValue(1.0)
        self.sp_speed.setSuffix("×")
        row2.addWidget(self.sp_speed)
        c.addLayout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        self.chk_skip_inj = QCheckBox("录制时忽略程序自己发出的点击/按键（规则边跑也能干净地录）")
        self.chk_skip_inj.setChecked(SETTINGS.record_skip_injected)
        self.chk_skip_inj.setToolTip(
            "勾上后，只记录你本人鼠标键盘的动作，规则自动点击不会被录进去；"
            "可以一边跑规则一边录新动作。"
        )
        self.chk_skip_inj.stateChanged.connect(self._on_skip_inj)
        row3.addWidget(self.chk_skip_inj)
        row3.addStretch(1)
        c.addLayout(row3)

        self.lbl_info = QLabel("还没有录制动作")
        self.lbl_info.setObjectName("Muted")
        self.lbl_info.setWordWrap(True)
        c.addWidget(self.lbl_info)

        lay.addWidget(card)

        warn = QLabel("提示：规则跑着的时候也能录 —— 勾上「忽略程序自己发出的点击」，"
                      "规则照常自动点击，你只管在目标窗口上做自己的操作；"
                      "播放动作时其他规则也不会被卡住，两件事同时进行。")
        warn.setObjectName("Muted")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        # ---- 已保存动作列表：一眼看清有哪些、多久、几次点击 ----
        list_card = QFrame()
        list_card.setObjectName("Card")
        lc = QVBoxLayout(list_card)
        lc.setContentsMargins(16, 12, 16, 12)
        lc.setSpacing(8)
        lh = QHBoxLayout()
        lt = QLabel("我的动作")
        lt.setStyleSheet("font-weight:700;")
        lh.addWidget(lt)
        lh.addSpacing(8)
        lhint = QLabel("双击一行 = 选中并播放；在「自动点击」页把动作选「播放录制动作」就能自动跑")
        lhint.setObjectName("Muted")
        lh.addWidget(lhint)
        lh.addStretch(1)
        lc.addLayout(lh)

        from PySide6.QtWidgets import (
            QAbstractItemView,
            QHeaderView,
            QTableWidget,
            QTableWidgetItem,
        )

        self.tb = QTableWidget(0, 3)
        self.tb.setHorizontalHeaderLabels(["动作名称", "内容", "创建时间"])
        self.tb.setAlternatingRowColors(True)
        self.tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tb.verticalHeader().setVisible(False)
        self.tb.verticalHeader().setDefaultSectionSize(32)
        hh = self.tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tb.cellDoubleClicked.connect(lambda r, _c: self._play_row(r))
        self.tb.itemSelectionChanged.connect(self._on_row_selected)
        lc.addWidget(self.tb, 1)
        lay.addWidget(list_card)
        self.refresh()

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        keep = self.cb_macro.currentData()
        self.cb_macro.blockSignals(True)
        self.cb_macro.clear()
        files = recorder.macro_files()
        for p in files:
            events = recorder.load_macro(p.name)
            self.cb_macro.addItem(f"{p.stem}　（{recorder.describe(events)}）", p.name)
        self.cb_macro.blockSignals(False)
        self._fill_table(files)
        if files:
            idx = self.cb_macro.findData(keep) if keep else 0
            self.cb_macro.setCurrentIndex(max(0, idx))
            self._show_current()
        else:
            self.lbl_info.setText("还没有录制动作：点上面的「开始录制」，照着操作一遍即可")

    def _fill_table(self, files) -> None:
        from PySide6.QtWidgets import QTableWidgetItem

        self.tb.blockSignals(True)
        self.tb.setRowCount(len(files))
        for r, p in enumerate(files):
            try:
                desc = recorder.describe(recorder.load_macro(p.name))
            except Exception:  # noqa: BLE001
                desc = ""
            try:
                mt = time.strftime("%m-%d %H:%M", time.localtime(p.stat().st_mtime))
            except Exception:  # noqa: BLE001
                mt = ""
            for c, v in enumerate((p.stem, desc, mt)):
                it = QTableWidgetItem(v)
                it.setData(Qt.ItemDataRole.UserRole, p.name)
                self.tb.setItem(r, c, it)
        self.tb.blockSignals(False)
        if files:
            self.tb.selectRow(0)

    def _on_row_selected(self) -> None:
        rows = self.tb.selectionModel().selectedRows() if self.tb.selectionModel() else []
        if not rows:
            return
        it = self.tb.item(rows[0].row(), 0)
        name = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not name:
            return
        i = self.cb_macro.findData(name)
        if i >= 0 and i != self.cb_macro.currentIndex():
            self.cb_macro.blockSignals(True)
            self.cb_macro.setCurrentIndex(i)
            self.cb_macro.blockSignals(False)
            self._show_current()

    def _play_row(self, row: int) -> None:
        it = self.tb.item(row, 0)
        name = it.data(Qt.ItemDataRole.UserRole) if it else None
        if not name:
            return
        i = self.cb_macro.findData(name)
        if i >= 0:
            self.cb_macro.setCurrentIndex(i)
        self.play_selected()

    def _show_current(self) -> None:
        name = self.cb_macro.currentData()
        if not name:
            return
        events = recorder.load_macro(name)
        self.lbl_info.setText(f"「{Path(name).stem}」：{recorder.describe(events)}")
        for r in range(self.tb.rowCount()):
            it = self.tb.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) == name:
                self.tb.blockSignals(True)
                self.tb.selectRow(r)
                self.tb.blockSignals(False)
                break

    def _set_state(self, text: str, recording: bool = False) -> None:
        self.lbl_state.setText(text)
        if recording:
            self.lbl_state.setStyleSheet(
                "background:#4a1d1d;border:1px solid #7f2d2d;border-radius:11px;"
                "color:#fca5a5;padding:3px 10px;font-size:12px;"
            )
        else:
            self.lbl_state.setStyleSheet("")

    def _update_clock(self) -> None:
        if recorder.RECORDER.is_recording:
            self._set_state(f"录制中 · {time.monotonic() - self._t0:.1f}s", recording=True)

    # ------------------------------------------------------------------
    def toggle_record(self) -> None:
        if recorder.RECORDER.is_playing:
            self._log("warn", "动作正在播放，先等它播完")
            return
        if recorder.RECORDER.is_recording:
            self._finish_record()
        else:
            self._start_record()

    def _on_skip_inj(self, *_args) -> None:
        SETTINGS.record_skip_injected = self.chk_skip_inj.isChecked()
        recorder.RECORDER.skip_injected = SETTINGS.record_skip_injected
        SETTINGS.save()

    def _start_record(self) -> None:
        recorder.RECORDER.skip_injected = self.chk_skip_inj.isChecked()
        SETTINGS.record_skip_injected = self.chk_skip_inj.isChecked()
        if not recorder.RECORDER.start():
            self._log("error", "录制启动失败")
            return
        for msg in recorder.RECORDER.errors:
            self._log("warn", msg)
        self._t0 = time.monotonic()
        self._tick.start()
        self._set_state("录制中 · 0.0s", recording=True)
        self.btn_rec.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_play.setEnabled(False)
        self._log("ok", "开始录制动作：现在去目标窗口操作，完成后按 F8 停止")
        win = self.window()
        if win is not None:
            win.showMinimized()

    def _finish_record(self) -> None:
        if not recorder.RECORDER.is_recording:
            return
        events = recorder.RECORDER.stop()
        self._tick.stop()
        self._set_state("空闲")
        self.btn_rec.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_play.setEnabled(True)
        win = self.window()
        if win is not None:
            win.showNormal()
            win.raise_()
            win.activateWindow()
        if not events:
            self._log("warn", "这次没有录到任何动作（是不是没点到目标窗口？）")
            QMessageBox.information(self, "没有录到动作", "没有捕获到鼠标动作。\n请确认你在目标窗口上真的点了几下，再按 F8 停止。")
            return
        name = self.ed_name.text().strip() or time.strftime("动作_%m%d_%H%M%S")
        path = recorder.save_macro(name, events)
        self._log("ok", f"动作已保存：{path.name}（{recorder.describe(events)}）")
        self.refresh()
        idx = self.cb_macro.findData(path.name)
        if idx >= 0:
            self.cb_macro.setCurrentIndex(idx)
        self._show_current()
        self.ed_name.clear()

    # ------------------------------------------------------------------
    def play_selected(self) -> None:
        name = self.cb_macro.currentData()
        if not name:
            QMessageBox.information(self, "提示", "还没有可选的动作文件，请先录制一个")
            return
        if self._play is not None and self._play.isRunning():
            self._log("warn", "正在播放中")
            return
        events = recorder.load_macro(name)
        if not events:
            QMessageBox.warning(self, "动作为空", f"「{Path(name).stem}」里没有事件")
            return
        self._play = PlayThread(name, float(self.sp_speed.value()), self)
        self._play.done.connect(self._on_play_done)
        self.btn_play_stop.setEnabled(True)
        self.btn_play.setEnabled(False)
        self.btn_rec.setEnabled(False)
        self._set_state("播放中…", recording=True)
        self._log("info", f"开始播放动作「{Path(name).stem}」（{recorder.describe(events)}，{self.sp_speed.value():.1f}×）")
        self._play.start()

    def _on_play_done(self, n: int, summary: str) -> None:
        self._set_state("空闲")
        self.btn_play_stop.setEnabled(False)
        self.btn_play.setEnabled(True)
        self.btn_rec.setEnabled(True)
        self._log("ok", f"动作播放结束：{summary}")

    def _stop_play(self) -> None:
        if self._play is not None and self._play.isRunning():
            self._play.stop()
            self._log("info", "已请求停止播放")

    def _delete(self) -> None:
        name = self.cb_macro.currentData()
        if not name:
            return
        if QMessageBox.question(self, "删除动作", f"确定删除「{Path(name).stem}」？") != QMessageBox.StandardButton.Yes:
            return
        recorder.delete_macro(name)
        self._log("ok", f"已删除动作：{Path(name).stem}")
        self.refresh()

    def shutdown(self) -> None:
        self._stop_play()
        if recorder.RECORDER.is_recording:
            recorder.RECORDER.stop()
