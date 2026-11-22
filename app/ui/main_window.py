"""主窗口：屏幕识别 / 自动化规则 / 设置。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config import APP_NAME, APP_TITLE, DATA_DIR, LOG_DIR, SETTINGS, TEMPLATE_DIR, asset_path
from core import actions, recorder, vision
from core import windows as winapi
from core.capture import GRABBER
from core.hotkey import MOD_ALT, HotkeyManager
from core.ocr_engine import OCR, TextItem, find_text, normalize
from core.rules import ACTION_LABELS, Rule, RuleEngine
from ui.point_picker import pick_point
from ui.preview import PreviewWidget
from ui.recorder_view import RecorderView
from ui.region_selector import capture_template, select_region
from ui.rule_dialog import RuleDialog, WindowPickDialog

LEVEL_COLORS = {
    "info": "#9aa4b2",
    "ok": "#34d399",
    "warn": "#fbbf24",
    "error": "#f87171",
    "dry": "#a78bfa",
}


class Job(QThread):
    """在后台线程跑一个耗时函数，避免界面卡住。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class Bridge(QObject):
    log = Signal(str, str)
    state = Signal(bool, bool)
    rule_item = Signal(int, int, bool)
    hot_start = Signal()
    hot_stop = Signal()
    hot_record = Signal()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1320, 900)
        self.setMinimumSize(1100, 720)

        self.region = None            # None = 全屏；否则 (x,y,w,h) 物理像素
        self._bgr = None
        self._origin = (0, 0)
        self._items: list[TextItem] = []
        self._matches: list = []
        self._box_targets: list = []      # 预览框 → (真实坐标, 名称)，双击预览框用
        self._job: Job | None = None
        self._template_path: str = ""
        self._region_label_extra = ""
        self._running = False
        self._user_stopped = False
        self._quitting = False
        self.target_window: str = ""   # "" = 整个桌面；否则按窗口标题关键字限定
        self._qwin_titles: list[str] = []   # v1.6：快捷任务要盯的窗口（可多选）

        self.bridge = Bridge()
        self.engine = RuleEngine(emit=self._emit_from_engine)
        self._connect_bridge()

        self._build_ui()
        self._start_hotkeys()
        self._start_preview_timer()
        self._refresh_rules()
        self._refresh_windows()
        for tb in (self.tb_text, self.tb_match):
            tb.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tb_text.customContextMenuRequested.connect(self._text_menu)
        self.tb_match.customContextMenuRequested.connect(self._match_menu)
        self._build_tray()
        self._start_watchdog()
        self._log("info", f"{APP_NAME} 已启动，F9 开始 / F10 停止 / F8 录制动作。")
        if SETTINGS.auto_start:
            QTimer.singleShot(900, self._auto_start_engine)

    # ==================================================================
    # 基础设施
    # ==================================================================
    def _emit_from_engine(self, kind: str, payload: dict) -> None:
        if kind == "log":
            self.bridge.log.emit(payload["level"], f"[{payload.get('time', '')}] {payload['text']}")
        elif kind == "state":
            self.bridge.state.emit(bool(payload.get("running")), bool(payload.get("dry_run", False)))
        elif kind == "rule_item":
            self.bridge.rule_item.emit(int(payload["index"]), int(payload["times"]), bool(payload["disabled"]))

    def _connect_bridge(self) -> None:
        self.bridge.log.connect(self._log)
        self.bridge.state.connect(self._set_running)
        self.bridge.rule_item.connect(self._on_rule_item)
        self.bridge.hot_start.connect(self._start_engine)
        self.bridge.hot_stop.connect(self.engine_stop)
        self.bridge.hot_record.connect(self._toggle_record)

    def _toggle_record(self) -> None:
        self.tabs.setCurrentWidget(self.recorder_view)
        self.recorder_view.toggle_record()

    def _start_hotkeys(self) -> None:
        self._hotkeys = HotkeyManager({
            "F9": (0, lambda: self.bridge.hot_start.emit()),
            "F10": (0, lambda: self.bridge.hot_stop.emit()),
            "F8": (0, lambda: self.bridge.hot_record.emit()),
        })
        self._hotkeys.start()

    def _start_preview_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(SETTINGS.preview_interval_ms)
        self._timer.timeout.connect(self._tick_preview)
        if SETTINGS.live_preview:
            self._timer.start()

    # ==================================================================
    # 界面搭建
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 12, 16, 8)
        body_lay.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tab_ocr(), "看屏幕")
        self.tabs.addTab(self._build_tab_rules(), "自动点击")
        self.recorder_view = RecorderView(self._log)
        self.tabs.addTab(self.recorder_view, "录制动作")
        self.tabs.addTab(self._build_tab_settings(), "设置")
        body_lay.addWidget(self.tabs, 1)
        body_lay.addWidget(self._build_log_panel())
        root.addWidget(body, 1)

        self.setCentralWidget(central)
        self._build_statusbar()

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("Header")
        header.setFixedHeight(72)
        lay = QHBoxLayout(header)
        lay.setContentsMargins(18, 10, 18, 10)
        lay.setSpacing(12)

        logo = QLabel("慧")
        logo.setFixedSize(40, 40)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "background:#14b8a6;color:#06231f;border-radius:10px;"
            "font-size:20px;font-weight:700;"
        )
        lay.addWidget(logo)

        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        t = QLabel(APP_NAME)
        t.setObjectName("HeaderTitle")
        s = QLabel("认文字 / 找图案 / 盯秒数 → 自动点击或播动作（纯本地离线）")
        s.setObjectName("HeaderSub")
        title_box.addWidget(t)
        title_box.addWidget(s)
        lay.addLayout(title_box)
        lay.addStretch(1)

        self.lbl_state = QLabel("未运行")
        self.lbl_state.setObjectName("Pill")
        lay.addWidget(self.lbl_state)

        self.btn_start_top = QPushButton("开始 F9")
        self.btn_start_top.setObjectName("Primary")
        self.btn_start_top.clicked.connect(self._start_engine)
        self.btn_stop_top = QPushButton("停止 F10")
        self.btn_stop_top.setObjectName("Danger")
        self.btn_stop_top.clicked.connect(self.engine_stop)
        self.btn_stop_top.setEnabled(False)
        lay.addWidget(self.btn_start_top)
        lay.addWidget(self.btn_stop_top)
        return header

    def _build_statusbar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(True)
        self.setStatusBar(bar)
        self._region_status = QLabel("识别区域：整个屏幕")
        self._region_status.setObjectName("Muted")
        bar.addPermanentWidget(self._region_status)
        bar.showMessage("就绪")

    # ---------------------------- 识别页 ----------------------------
    def _build_tab_ocr(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)

        win_row = QHBoxLayout()
        cap_w = QLabel("目标窗口")
        cap_w.setStyleSheet("font-weight:600;")
        self.cb_window = QComboBox()
        self.cb_window.setMinimumWidth(280)
        self.cb_window.currentIndexChanged.connect(self._on_window_changed)
        btn_refresh_win = QPushButton("刷新窗口")
        btn_refresh_win.clicked.connect(self._refresh_windows)
        hint_win = QLabel("选定窗口后只识别这个窗口；规则页可给每条规则单独指定窗口")
        hint_win.setObjectName("Muted")
        win_row.addWidget(cap_w)
        win_row.addWidget(self.cb_window)
        win_row.addWidget(btn_refresh_win)
        win_row.addStretch(1)
        win_row.addWidget(hint_win)
        lay.addLayout(win_row)

        toolbar = QHBoxLayout()
        btn_region = QPushButton("框选识别区域")
        btn_region.clicked.connect(self._pick_region)
        btn_full = QPushButton("重置全屏")
        btn_full.clicked.connect(self._reset_region)
        self.btn_ocr = QPushButton("立即识别")
        self.btn_ocr.setObjectName("Primary")
        self.btn_ocr.clicked.connect(self.run_ocr)
        self.chk_live = QCheckBox("实时预览")
        self.chk_live.setChecked(SETTINGS.live_preview)
        self.chk_live.toggled.connect(self._toggle_live)
        self.lbl_ocr_region = QLabel("识别区域：整个屏幕")
        self.lbl_ocr_region.setObjectName("Muted")
        toolbar.addWidget(btn_region)
        toolbar.addWidget(btn_full)
        toolbar.addWidget(self.btn_ocr)
        toolbar.addWidget(self.chk_live)
        toolbar.addStretch(1)
        toolbar.addWidget(self.lbl_ocr_region)
        lay.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # 左：预览
        left = QFrame()
        left.setObjectName("Card")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(10, 10, 10, 10)
        head = QHBoxLayout()
        cap = QLabel("屏幕预览")
        cap.setStyleSheet("font-weight:600;")
        hint = QLabel("双击框内任意位置可直接点击真实屏幕")
        hint.setObjectName("Muted")
        head.addWidget(cap)
        head.addStretch(1)
        head.addWidget(hint)
        left_lay.addLayout(head)
        self.preview = PreviewWidget()
        self.preview.box_activated.connect(self._click_box)
        self.preview.clicked_phys.connect(self._click_point)
        left_lay.addWidget(self.preview, 1)
        splitter.addWidget(left)

        # 右：结果面板
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(10)

        gb_text = QGroupBox("识别到的文字")
        gb_text.setMinimumHeight(258)
        g_text = QVBoxLayout(gb_text)
        self.ed_filter = QLineEdit()
        self.ed_filter.setPlaceholderText("过滤文字…（双击某一行 = 点击屏幕上对应位置）")
        self.ed_filter.textChanged.connect(self._fill_text_table)
        self.tb_text = self._make_table(["#", "文字内容", "置信度"])
        self.tb_text.setMinimumHeight(124)
        self.tb_text.itemDoubleClicked.connect(self._on_text_row_dblclick)
        g_text.addWidget(self.ed_filter)
        g_text.addWidget(self.tb_text, 1)
        row = QHBoxLayout()
        btn_copy = QPushButton("复制全部文字")
        btn_copy.clicked.connect(self._copy_all_text)
        btn_save = QPushButton("导出结果")
        btn_save.clicked.connect(self._export_text)
        self.lbl_ocr_stat = QLabel("尚未识别")
        self.lbl_ocr_stat.setObjectName("Muted")
        row.addWidget(btn_copy)
        row.addWidget(btn_save)
        row.addStretch(1)
        row.addWidget(self.lbl_ocr_stat)
        g_text.addLayout(row)
        right_lay.addWidget(gb_text, 3)

        gb_img = QGroupBox("图案定位（找图）")
        gb_img.setMinimumHeight(206)
        g_img = QVBoxLayout(gb_img)
        row1 = QHBoxLayout()
        self.lbl_tpl = QLabel("未选择模板")
        self.lbl_tpl.setFixedSize(88, 56)
        self.lbl_tpl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_tpl.setStyleSheet("border:1px solid #2f3540;border-radius:6px;background:#14161a;color:#6b7688;")
        btn_tpl = QPushButton("截取图案")
        btn_tpl.clicked.connect(self._capture_template)
        btn_find = QPushButton("立即查找")
        btn_find.setObjectName("Primary")
        btn_find.clicked.connect(self.run_match)
        row1.addWidget(self.lbl_tpl)
        row1.addWidget(btn_tpl)
        row1.addWidget(btn_find)
        self.sp_tpl_thr = QDoubleSpinBox()
        self.sp_tpl_thr.setRange(0.4, 0.99)
        self.sp_tpl_thr.setSingleStep(0.01)
        self.sp_tpl_thr.setDecimals(2)
        self.sp_tpl_thr.setValue(SETTINGS.template_threshold)
        self.sp_tpl_thr.setPrefix("相似度 ")
        row1.addWidget(self.sp_tpl_thr)
        row1.addStretch(1)
        g_img.addLayout(row1)
        self.tb_match = self._make_table(["#", "屏幕位置", "相似度"])
        self.tb_match.setMinimumHeight(94)
        self.tb_match.itemDoubleClicked.connect(self._on_match_row_dblclick)
        self.lbl_match_stat = QLabel("先用「截取图案」框选一个图标 / 按钮")
        self.lbl_match_stat.setObjectName("Muted")
        g_img.addWidget(self.lbl_match_stat)
        g_img.addWidget(self.tb_match, 1)
        right_lay.addWidget(gb_img, 2)

        right.setMinimumWidth(430)
        right_lay.setSizeConstraint(QVBoxLayout.SizeConstraint.SetMinimumSize)
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        splitter.setSizes([780, 460])
        lay.addWidget(splitter, 1)
        return page

    def _make_table(self, headers: list[str]) -> QTableWidget:
        tb = QTableWidget(0, len(headers))
        tb.setHorizontalHeaderLabels(headers)
        tb.setAlternatingRowColors(True)
        tb.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        tb.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        tb.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        tb.verticalHeader().setVisible(False)
        tb.verticalHeader().setDefaultSectionSize(30)
        hh = tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        tb.setColumnWidth(0, 38)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        return tb

    # ---------------------------- 规则页 ----------------------------
    def _build_tab_rules(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(10)

        # ---- 快捷任务：选窗口 + 填文字 + 选动作 → 一键生成规则 ----
        quick = QFrame()
        quick.setObjectName("Card")
        qv = QVBoxLayout(quick)
        qv.setContentsMargins(12, 9, 12, 9)
        qv.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        cap = QLabel("看到文字就动手")
        cap.setStyleSheet("font-weight:700;color:#2dd4bf;")
        top.addWidget(cap)
        top.addWidget(QLabel("窗口"))
        self.btn_qwin = QPushButton("全屏（整个桌面）")
        self.btn_qwin.setMinimumWidth(200)
        self.btn_qwin.setToolTip("可以一次勾多个窗口：同一套规则在每个窗口里各跑一遍")
        self.btn_qwin.clicked.connect(self._pick_quick_windows)
        top.addWidget(self.btn_qwin, 1)
        top.addWidget(QLabel("文字"))
        self.ed_qtext = QLineEdit()
        self.ed_qtext.setPlaceholderText("例如 确定 / 开始游戏 / 领取奖励")
        self.ed_qtext.returnPressed.connect(self._add_quick_rule)
        top.addWidget(self.ed_qtext, 2)
        top.addWidget(QLabel("动作"))
        self.cb_qact = QComboBox()
        self.cb_qact.addItem("点击它", "click")
        self.cb_qact.addItem("点击别的位置", "click_at")
        self.cb_qact.addItem("播放录制动作", "macro")
        self.cb_qact.currentIndexChanged.connect(self._sync_quick_mode)
        top.addWidget(self.cb_qact)
        qv.addLayout(top)

        # -- 点击"别的位置"时显示坐标 --
        self.w_qat = QWidget()
        qat = QHBoxLayout(self.w_qat)
        qat.setContentsMargins(0, 0, 0, 0)
        qat.setSpacing(8)
        self.sp_qx = QSpinBox()
        self.sp_qy = QSpinBox()
        for sp in (self.sp_qx, self.sp_qy):
            sp.setRange(-8000, 8000)
            sp.setSuffix(" px")
        btn_qpick = QPushButton("在屏幕上点一下取坐标")
        btn_qpick.clicked.connect(lambda: self._pick_coords(self.sp_qx, self.sp_qy, self.chk_qrel))
        self.chk_qrel = QCheckBox("相对窗口")
        self.chk_qrel.setChecked(True)
        qat.addWidget(QLabel("点这里 →  X"))
        qat.addWidget(self.sp_qx)
        qat.addWidget(QLabel("Y"))
        qat.addWidget(self.sp_qy)
        qat.addWidget(btn_qpick)
        qat.addWidget(self.chk_qrel)
        qat.addStretch(1)
        qv.addWidget(self.w_qat)

        # -- 播放录制动作时显示宏选择 --
        self.w_qmacro = QWidget()
        qmr = QHBoxLayout(self.w_qmacro)
        qmr.setContentsMargins(0, 0, 0, 0)
        qmr.setSpacing(8)
        self.cb_qmacro = QComboBox()
        self.cb_qmacro.setMinimumWidth(280)
        btn_qmrefresh = QPushButton("刷新")
        btn_qmrefresh.clicked.connect(self._fill_qmacro)
        qmr.addWidget(QLabel("动作"))
        qmr.addWidget(self.cb_qmacro, 1)
        qmr.addWidget(btn_qmrefresh)
        qmr.addStretch(1)
        qv.addWidget(self.w_qmacro)
        self._fill_qmacro()

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.lbl_qhint = QLabel()
        self.lbl_qhint.setObjectName("Muted")
        bottom.addWidget(self.lbl_qhint, 1)
        self.chk_qstart = QCheckBox("加入后立即开始")
        self.chk_qstart.setChecked(True)
        btn_qadd = QPushButton("生成规则")
        btn_qadd.setObjectName("Primary")
        btn_qadd.clicked.connect(self._add_quick_rule)
        bottom.addWidget(self.chk_qstart)
        bottom.addWidget(btn_qadd)
        qv.addLayout(bottom)
        lay.addWidget(quick)

        # ---- 秒数卡：秒数不动 → 点另一个位置 ----
        tcard = QFrame()
        tcard.setObjectName("Card")
        tl = QHBoxLayout(tcard)
        tl.setContentsMargins(12, 9, 12, 9)
        tl.setSpacing(8)
        tcap = QLabel("秒数停住就动手")
        tcap.setStyleSheet("font-weight:700;color:#f59e0b;")
        tl.addWidget(tcap)
        btn_tregion = QPushButton("框选秒数区域")
        btn_tregion.clicked.connect(self._pick_timer_region)
        self.lbl_tregion = QLabel("还没框选")
        self.lbl_tregion.setObjectName("Muted")
        tl.addWidget(btn_tregion)
        tl.addWidget(self.lbl_tregion)
        tl.addSpacing(6)
        tl.addWidget(QLabel("数字不动（须带「秒」）"))
        self.sp_qstill = QDoubleSpinBox()
        self.sp_qstill.setToolTip("只认数字后面带「秒」字的倒计时，比如「剩余 30 秒」；框选区域要把「秒」也框进去")
        self.sp_qstill.setRange(0.5, 120.0)
        self.sp_qstill.setSingleStep(0.5)
        self.sp_qstill.setDecimals(1)
        self.sp_qstill.setValue(3.0)
        self.sp_qstill.setSuffix(" 秒")
        tl.addWidget(self.sp_qstill)
        tl.addSpacing(6)
        tl.addWidget(QLabel("后去点  X"))
        self.sp_tx = QSpinBox()
        self.sp_ty = QSpinBox()
        for sp in (self.sp_tx, self.sp_ty):
            sp.setRange(-8000, 8000)
            sp.setSuffix(" px")
        tl.addWidget(self.sp_tx)
        tl.addWidget(QLabel("Y"))
        tl.addWidget(self.sp_ty)
        btn_tpick = QPushButton("取坐标")
        btn_tpick.clicked.connect(lambda: self._pick_coords(self.sp_tx, self.sp_ty, self.chk_trel))
        tl.addWidget(btn_tpick)
        self.chk_trel = QCheckBox("相对窗口")
        self.chk_trel.setChecked(True)
        tl.addWidget(self.chk_trel)
        btn_tadd = QPushButton("生成规则")
        btn_tadd.setObjectName("Primary")
        btn_tadd.clicked.connect(self._add_timer_rule)
        tl.addWidget(btn_tadd)
        tl.addStretch(1)
        lay.addWidget(tcard)
        self._timer_region = [0, 0, 0, 0]
        self._sync_quick_mode()

        # ---- 多窗口：哪些窗口允许被规则操作（不勾 = 全部允许）----
        wcard = QFrame()
        wcard.setObjectName("Card")
        wl = QHBoxLayout(wcard)
        wl.setContentsMargins(12, 7, 12, 7)
        wl.setSpacing(8)
        wcap = QLabel("多窗口同时跑")
        wcap.setStyleSheet("font-weight:700;color:#38bdf8;")
        wl.addWidget(wcap)
        wl.addWidget(QLabel("参与窗口"))
        self.lbl_white = QLabel("全部窗口")
        self.lbl_white.setObjectName("Muted")
        wl.addWidget(self.lbl_white, 1)
        btn_white = QPushButton("选择参与窗口…")
        btn_white.clicked.connect(self._pick_whitelist)
        btn_white_clear = QPushButton("清除（=全部）")
        btn_white_clear.clicked.connect(self._clear_whitelist)
        wl.addWidget(btn_white)
        wl.addWidget(btn_white_clear)
        lay.addWidget(wcard)
        self._update_whitelist_label()

        bar = QHBoxLayout()
        for text, slot in (
            ("新增规则", self._add_rule),
            ("编辑", self._edit_rule),
            ("删除", self._del_rule),
            ("上移", lambda: self._move_rule(-1)),
            ("下移", lambda: self._move_rule(1)),
            ("导入", self._import_rules),
            ("导出", self._export_rules),
        ):
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            bar.addWidget(btn)
        bar.addStretch(1)
        hint = QLabel("从上到下检查；一条规则可以只盯一个窗口，也可以同时盯多个窗口")
        hint.setObjectName("Muted")
        bar.addWidget(hint)
        lay.addLayout(bar)

        self.tb_rules = QTableWidget(0, 6)
        self.tb_rules.setHorizontalHeaderLabels(["启用", "规则名称", "窗口", "条件 → 动作", "已执行", "状态"])
        self.tb_rules.setAlternatingRowColors(True)
        self.tb_rules.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tb_rules.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tb_rules.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tb_rules.verticalHeader().setVisible(False)
        self.tb_rules.verticalHeader().setDefaultSectionSize(38)
        hh = self.tb_rules.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tb_rules.setColumnWidth(0, 56)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.tb_rules.itemDoubleClicked.connect(lambda _it: self._edit_rule())
        lay.addWidget(self.tb_rules, 1)

        bottom = QHBoxLayout()
        self.chk_loop = QCheckBox("循环执行")
        self.chk_loop.setChecked(True)
        self.chk_dry = QCheckBox("演练模式（只识别、不真的点击）")
        self.btn_start = QPushButton("开始执行  F9")
        self.btn_start.setObjectName("Primary")
        self.btn_start.clicked.connect(self._start_engine)
        self.btn_stop = QPushButton("停止  F10")
        self.btn_stop.setObjectName("Danger")
        self.btn_stop.clicked.connect(self.engine_stop)
        self.btn_stop.setEnabled(False)
        bottom.addWidget(self.chk_loop)
        bottom.addWidget(self.chk_dry)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_start)
        bottom.addWidget(self.btn_stop)
        lay.addLayout(bottom)

        note = QLabel("安全提示：把鼠标快速甩到屏幕左上角即可紧急停止；建议先用「演练模式」确认识别是否准确，再开实战。")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        lay.addWidget(note)
        return page

    # ---------------------------- 设置页 ----------------------------
    def _build_tab_settings(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        scroll_host = QWidget()
        lay = QVBoxLayout(scroll_host)
        lay.setContentsMargins(2, 2, 8, 2)
        lay.setSpacing(12)

        gb1 = QGroupBox("识别与找图")
        f1 = QFormLayout(gb1)
        f1.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.sp_ocr_score = QDoubleSpinBox()
        self.sp_ocr_score.setMinimumWidth(200)
        self.sp_ocr_score.setRange(0.3, 1.0)
        self.sp_ocr_score.setSingleStep(0.01)
        self.sp_ocr_score.setDecimals(2)
        self.sp_ocr_score.setValue(SETTINGS.ocr_min_score)
        self.chk_multi = QCheckBox("多尺度搜索（目标被缩放时也能找到）")
        self.chk_multi.setChecked(SETTINGS.multi_scale)
        self.sp_maxmatch = QSpinBox()
        self.sp_maxmatch.setRange(1, 100)
        self.sp_maxmatch.setMinimumWidth(200)
        self.sp_maxmatch.setValue(SETTINGS.max_match_results)
        f1.addRow("文字置信度下限", self.sp_ocr_score)
        f1.addRow("", self.chk_multi)
        f1.addRow("单次最多匹配数", self.sp_maxmatch)
        # 提速 / 提高识别率
        self.sp_upscale = QDoubleSpinBox()
        self.sp_upscale.setMinimumWidth(200)
        self.sp_upscale.setRange(1.0, 4.0)
        self.sp_upscale.setSingleStep(0.5)
        self.sp_upscale.setDecimals(1)
        self.sp_upscale.setSuffix("×")
        self.sp_upscale.setValue(SETTINGS.ocr_upscale)
        self.chk_frameskip = QCheckBox("画面没变就复用上次结果（明显更快，推荐开）")
        self.chk_frameskip.setChecked(SETTINGS.frame_skip)
        f1.addRow("小字放大倍数", self.sp_upscale)
        f1.addRow("", self.chk_frameskip)

        gb2 = QGroupBox("操作手感")
        f2 = QFormLayout(gb2)
        f2.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.sp_delay = QSpinBox()
        self.sp_delay.setMinimumWidth(200)
        self.sp_delay.setRange(0, 2000)
        self.sp_delay.setSingleStep(20)
        self.sp_delay.setSuffix(" ms")
        self.sp_delay.setValue(SETTINGS.click_delay_ms)
        self.sp_dbl = QSpinBox()
        self.sp_dbl.setMinimumWidth(200)
        self.sp_dbl.setRange(50, 600)
        self.sp_dbl.setSuffix(" ms")
        self.sp_dbl.setValue(SETTINGS.double_click_interval_ms)
        self.chk_human = QCheckBox("拟人化：轻微抖动 + 随机间隔（降低被判定为脚本的概率）")
        self.chk_human.setChecked(SETTINGS.humanize)
        f2.addRow("点击前等待", self.sp_delay)
        f2.addRow("双击间隔", self.sp_dbl)
        f2.addRow("", self.chk_human)

        gb3 = QGroupBox("运行与安全")
        f3 = QFormLayout(gb3)
        f3.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        self.sp_loop = QDoubleSpinBox()
        self.sp_loop.setMinimumWidth(200)
        self.sp_loop.setRange(0.1, 30.0)
        self.sp_loop.setSingleStep(0.1)
        self.sp_loop.setDecimals(1)
        self.sp_loop.setSuffix(" 秒")
        self.sp_loop.setValue(SETTINGS.loop_interval_s)
        self.chk_corner = QCheckBox("鼠标甩到左上角紧急停止")
        self.chk_corner.setChecked(SETTINGS.emergency_corner)
        self.chk_savelog = QCheckBox("把运行日志保存到文件")
        self.chk_savelog.setChecked(SETTINGS.save_log)
        self.chk_minimize = QCheckBox("识别前自动最小化本窗口")
        self.chk_minimize.setChecked(SETTINGS.minimize_before_capture)
        self.sp_preview = QSpinBox()
        self.sp_preview.setMinimumWidth(200)
        self.sp_preview.setRange(200, 5000)
        self.sp_preview.setSingleStep(100)
        self.sp_preview.setSuffix(" ms")
        self.sp_preview.setValue(SETTINGS.preview_interval_ms)
        self.chk_live_def = QCheckBox("启动时开启实时预览")
        self.chk_live_def.setChecked(SETTINGS.live_preview)
        self.chk_autostart = QCheckBox("启动程序后自动开始执行已启用的规则")
        self.chk_autostart.setChecked(SETTINGS.auto_start)
        self.chk_autorecover = QCheckBox("引擎意外停了自动重启（适合长期挂机）")
        self.chk_autorecover.setChecked(SETTINGS.auto_recover)
        self.chk_tray = QCheckBox("关闭窗口时缩到托盘继续运行（不退出）")
        self.chk_tray.setChecked(SETTINGS.close_to_tray)
        self.chk_toast = QCheckBox("命中时弹托盘提示（关掉更安静）")
        self.chk_toast.setChecked(SETTINGS.toast_on_hit)
        f3.addRow("规则扫描间隔", self.sp_loop)
        f3.addRow("", self.chk_autostart)
        f3.addRow("", self.chk_autorecover)
        f3.addRow("", self.chk_tray)
        f3.addRow("", self.chk_toast)
        f3.addRow("", self.chk_corner)
        f3.addRow("", self.chk_savelog)
        f3.addRow("", self.chk_minimize)
        f3.addRow("预览刷新间隔", self.sp_preview)
        f3.addRow("", self.chk_live_def)

        gb4 = QGroupBox("快捷键与存储")
        f4 = QVBoxLayout(gb4)
        for line in (
            "F9 —— 开始执行规则（任何窗口都生效）",
            "F10 —— 立即停止",
            "F8 —— 开始 / 停止录制鼠标动作",
            "鼠标移到屏幕左上角 —— 紧急停止",
            "双击预览中的方框 —— 直接点击屏幕上对应位置",
        ):
            lb = QLabel(line)
            lb.setObjectName("Muted")
            f4.addWidget(lb)
        row_path = QHBoxLayout()
        self.lbl_data_dir = QLabel(str(DATA_DIR))
        self.lbl_data_dir.setObjectName("Muted")
        self.lbl_data_dir.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        btn_open = QPushButton("打开数据目录")
        btn_open.clicked.connect(lambda: os.startfile(str(DATA_DIR)))
        row_path.addWidget(self.lbl_data_dir, 1)
        row_path.addWidget(btn_open)
        f4.addLayout(row_path)

        gb5 = QGroupBox("关于")
        about = QLabel(
            f"{APP_NAME} v1.8 · 纯本地运行，不联网、不上传屏幕内容\n"
            "文字识别：RapidOCR（PP-OCR 轻量模型）　找图：OpenCV 模板匹配\n"
            "操作模拟：Win32 SendInput　录制：低层鼠标钩子　界面：PySide6 (Qt 6)"
        )
        about.setObjectName("Muted")
        about.setWordWrap(True)
        gb5_lay = QVBoxLayout(gb5)
        gb5_lay.addWidget(about)

        lay.addWidget(gb1)
        lay.addWidget(gb2)
        lay.addWidget(gb3)
        lay.addWidget(gb4)
        lay.addWidget(gb5)
        lay.addStretch(1)
        scroll.setWidget(scroll_host)
        outer.addWidget(scroll, 1)

        row = QHBoxLayout()
        btn_save = QPushButton("保存设置")
        btn_save.setObjectName("Primary")
        btn_save.clicked.connect(self._save_settings)
        row.addStretch(1)
        row.addWidget(btn_save)
        outer.addLayout(row)
        return page

    def _build_log_panel(self) -> QWidget:
        card = QFrame()
        card.setObjectName("Card")
        card.setFixedHeight(152)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(6)
        head = QHBoxLayout()
        t = QLabel("运行日志")
        t.setStyleSheet("font-weight:600;")
        head.addWidget(t)
        head.addStretch(1)
        btn_clear = QPushButton("清空")
        btn_clear.setObjectName("Ghost")
        btn_clear.clicked.connect(lambda: self.log_view.clear())
        btn_open = QPushButton("打开日志目录")
        btn_open.setObjectName("Ghost")
        btn_open.clicked.connect(lambda: os.startfile(str(LOG_DIR)))
        head.addWidget(btn_clear)
        head.addWidget(btn_open)
        lay.addLayout(head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1000)
        self.log_view.setStyleSheet("background:#12141a;border:1px solid #2f3540;border-radius:8px;")
        f = QFont("Consolas")
        f.setPointSize(9)
        self.log_view.setFont(f)
        lay.addWidget(self.log_view, 1)
        return card

    # ==================================================================
    # 日志 / 状态
    # ==================================================================
    def _log(self, level: str, text: str) -> None:
        color = LEVEL_COLORS.get(level, "#9aa4b2")
        stamp = "" if text.startswith("[") else f"[{time.strftime('%H:%M:%S')}] "
        self.log_view.appendHtml(
            f'<span style="color:{color};">{stamp}{self._escape(text)}</span>'
        )
        self.log_view.verticalScrollBar().setValue(self.log_view.verticalScrollBar().maximum())

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _set_running(self, running: bool, dry: bool = False) -> None:
        self._running = running
        if running:
            self.lbl_state.setText("运行中 · 演练" if dry else "运行中")
            self.lbl_state.setStyleSheet(
                "background:#3a2f12;border:1px solid #6b5518;border-radius:11px;"
                "color:#fbbf24;padding:3px 10px;font-size:12px;"
                if dry
                else "background:#0f3d34;border:1px solid #145c4d;border-radius:11px;"
                     "color:#34d399;padding:3px 10px;font-size:12px;"
            )
        else:
            self.lbl_state.setText("未运行")
            self.lbl_state.setStyleSheet("")
            self.lbl_state.setObjectName("Pill")
        for btn, on in ((self.btn_start_top, not running), (self.btn_start, not running),
                        (self.btn_stop_top, running), (self.btn_stop, running)):
            btn.setEnabled(on)
        self.statusBar().showMessage("规则运行中…" if running else "就绪")

    # ==================================================================
    # 抓屏辅助
    # ==================================================================
    def _hide_for_capture(self) -> bool:
        if SETTINGS.minimize_before_capture and self.isVisible() and not self.isMinimized():
            self.showMinimized()
            QApplication.processEvents()
            time.sleep(0.35)
            return True
        return False

    def _restore_after_capture(self, hidden: bool) -> None:
        if hidden:
            self.showNormal()
            self.raise_()
            self.activateWindow()

    def _start_job(self, fn, on_done, what: str = "") -> None:
        if self._job is not None and self._job.isRunning():
            self._log("warn", "上一个任务还在执行，请稍候")
            return
        self.btn_ocr.setEnabled(False)
        hidden = self._hide_for_capture()
        self.statusBar().showMessage(what or "处理中…")
        job = Job(fn, self)
        job.done.connect(on_done)
        job.failed.connect(self._on_job_failed)
        job.finished.connect(lambda: self._restore_after_capture(hidden))
        job.finished.connect(lambda: self.btn_ocr.setEnabled(True))
        self._job = job
        job.start()

    def _on_job_failed(self, msg: str) -> None:
        self.btn_ocr.setEnabled(True)
        self.statusBar().showMessage("就绪")
        self._log("error", msg)
        QMessageBox.critical(self, "操作失败", msg)

    # ==================================================================
    # 识别区域
    # ==================================================================
    # ==================================================================
    # 目标窗口
    # ==================================================================
    def _refresh_windows(self) -> None:
        """把当前可见窗口填进下拉框（保留原有选择）。"""
        try:
            wins = winapi.list_windows(exclude_pid=os.getpid())
        except Exception as exc:  # noqa: BLE001
            self._log("warn", f"枚举窗口失败：{exc}")
            return
        keep = self.target_window
        cb = self.cb_window
        cb.blockSignals(True)
        cb.clear()
        cb.addItem("全屏（整个桌面）", "")
        for w in wins:
            title = w["title"]
            short = title if len(title) <= 38 else title[:37] + "…"
            cb.addItem(f"{short}   ·   {w['exe'] or w['class']}", title)
        cb.addItem("（找不到？点「刷新窗口」重新扫）", "__refresh__")
        idx = cb.findData(keep) if keep else 0
        cb.setCurrentIndex(idx if idx >= 0 else 0)
        cb.blockSignals(False)
        self._update_qwin_label()
        self._log("info", f"已扫描到 {len(wins)} 个窗口，可在「目标窗口」里选择")

    def _on_window_changed(self) -> None:
        data = self.cb_window.currentData()
        if data == "__refresh__":
            self._refresh_windows()
            return
        self.target_window = data or ""
        if self.target_window:
            rect = winapi.rect_for_title(self.target_window)
            if rect:
                self._log("ok", f"目标窗口：{self.target_window}（客户区 {rect[2]}×{rect[3]} @ {rect[0]},{rect[1]}）")
            else:
                self._log("warn", f"目标窗口「{self.target_window}」当前不可见，可能已最小化")
        else:
            self._log("info", "目标窗口：整个桌面")
        self._update_region_labels()
        if self.chk_live.isChecked():
            self._tick_preview()

    # ------------------------- v1.6 多窗口选择 -------------------------
    def _qwin_label(self) -> str:
        titles = self._qwin_titles
        if not titles:
            return "全屏（整个桌面）"
        if len(titles) == 1:
            return titles[0]
        joined = "；".join(titles)
        if len(joined) > 28:
            joined = joined[:27] + "…"
        return f"{len(titles)} 个窗口：{joined}"

    def _update_qwin_label(self) -> None:
        if not hasattr(self, "btn_qwin"):
            return
        self.btn_qwin.setText(self._qwin_label())
        self.btn_qwin.setToolTip("；".join(self._qwin_titles) if self._qwin_titles else "整个桌面")

    def _capture_ok_for(self, title: str) -> bool:
        """这个窗口支持"窗口后台截图"吗？（不支持就用屏幕区域）"""
        try:
            hwnd = winapi.find_by_title(title)
            if not hwnd:
                return False
            return winapi.capture_client(hwnd) is not None
        except Exception:  # noqa: BLE001
            return False

    def _pick_quick_windows(self) -> None:
        """快捷任务：一次勾多个窗口，同一条规则在每个窗口各跑一遍。"""
        dlg = WindowPickDialog(self._qwin_titles, self, "快捷任务要盯哪些窗口（可多选）")
        if dlg.exec() != QDialogResult.Accepted:
            return
        self._qwin_titles = dlg.selected_titles()
        self._update_qwin_label()
        self._log("ok", "快捷任务窗口：" + ("；".join(self._qwin_titles) if self._qwin_titles else "整个桌面"))

    def _update_whitelist_label(self) -> None:
        wl = [str(w) for w in (SETTINGS.window_whitelist or []) if str(w).strip()]
        if not hasattr(self, "lbl_white"):
            return
        if not wl:
            self.lbl_white.setText("全部窗口（不限制）")
            self.lbl_white.setToolTip("")
            return
        short = "；".join(wl)
        if len(short) > 60:
            short = short[:59] + "…"
        self.lbl_white.setText(f"已选 {len(wl)} 个：{short}")
        self.lbl_white.setToolTip("；".join(wl))

    def _pick_whitelist(self) -> None:
        """选择哪些窗口允许被自动操作（一个都不勾 = 不限制）。"""
        dlg = WindowPickDialog(SETTINGS.window_whitelist, self, "哪些窗口允许被自动操作（可多选）")
        if dlg.exec() != QDialogResult.Accepted:
            return
        SETTINGS.window_whitelist = dlg.selected_titles()
        SETTINGS.save()
        self._update_whitelist_label()
        n = len(SETTINGS.window_whitelist)
        self._log("ok", "参与窗口已更新：" + ("全部窗口" if not n else f"{n} 个窗口，其他窗口的规则会被跳过"))

    def _clear_whitelist(self) -> None:
        SETTINGS.window_whitelist = []
        SETTINGS.save()
        self._update_whitelist_label()
        self._log("info", "已清除参与窗口限制：所有窗口都可以被规则操作")

    def _current_region(self):
        """识别 / 找图 / 预览实际使用的区域：优先窗口客户区，其次手动框选。"""
        if self.target_window:
            rect = winapi.rect_for_title(self.target_window)
            if rect is None:
                raise RuntimeError(f"窗口「{self.target_window}」不在了（可能已关闭或最小化）")
            return rect
        return self.region

    def _update_region_labels(self) -> None:
        if self.target_window:
            txt = f"识别区域：窗口「{self.target_window[:24]}」"
        elif self.region:
            x, y, w, h = self.region
            txt = f"识别区域：X={x} Y={y}  {w}×{h}"
        else:
            txt = "识别区域：整个屏幕"
        if hasattr(self, "lbl_ocr_region"):
            self.lbl_ocr_region.setText(txt)
        self._region_status.setText(txt)

    # ==================================================================
    # 识别区域
    # ==================================================================
    def _pick_region(self) -> None:
        if self.target_window:
            self.cb_window.setCurrentIndex(0)   # 手动框选时回到整个桌面
        self._timer.stop()
        hidden = self._hide_for_capture()
        rect = select_region(self)
        if hidden:
            self.showNormal()
            self.raise_()
            self.activateWindow()
        QApplication.processEvents()
        time.sleep(0.15)
        self.region = tuple(rect) if rect else None
        self._update_region_labels()
        if self.chk_live.isChecked():
            self._timer.start()
        if rect:
            self._log("info", f"已设定识别区域：{rect}")
            self.run_ocr()

    def _reset_region(self) -> None:
        self.region = None
        self._update_region_labels()
        self._log("info", "识别区域已重置为整个屏幕")

    def _toggle_live(self, on: bool) -> None:
        SETTINGS.live_preview = on
        if on:
            self._timer.setInterval(SETTINGS.preview_interval_ms)
            self._timer.start()
        else:
            self._timer.stop()

    def _tick_preview(self) -> None:
        if self._running or not self.isVisible() or self.isMinimized():
            return
        try:
            img, origin = GRABBER.grab(self._current_region())
        except Exception:
            return
        self._bgr, self._origin = img, origin
        self.preview.set_image(img, origin)

    # ==================================================================
    # 文字识别
    # ==================================================================
    def run_ocr(self) -> None:
        try:
            region = self._current_region()
        except RuntimeError as exc:  # 目标窗口已关闭
            self._log("warn", str(exc))
            self._refresh_windows()
            return

        def work():
            t0 = time.perf_counter()
            img, origin = GRABBER.grab(region)
            items = OCR.recognize(img, min_score=SETTINGS.ocr_min_score, origin=origin)
            return {"img": img, "origin": origin, "items": items, "cost": time.perf_counter() - t0}

        self._start_job(work, self._on_ocr_done, "正在识别屏幕文字…")

    def _on_ocr_done(self, data: dict) -> None:
        self._bgr, self._origin = data["img"], data["origin"]
        self._items = data["items"]
        self.preview.set_image(self._bgr, self._origin)
        boxes = [
            {"rect": it.rect, "label": it.text, "kind": "text"} for it in self._items
        ]
        self.preview.set_boxes(boxes)
        self._box_targets = [(tuple(it.center), f"「{it.text}」") for it in self._items]
        self._fill_text_table()
        self.lbl_ocr_stat.setText(f"{len(self._items)} 段 · {data['cost']:.2f}s")
        self.statusBar().showMessage("就绪")
        self._log("ok", f"识别完成：{len(self._items)} 段文字，用时 {data['cost']:.2f} 秒")

    def _fill_text_table(self) -> None:
        kw = self.ed_filter.text().strip()
        rows = [(i, it) for i, it in enumerate(self._items)
                if not kw or normalize(kw) in normalize(it.text)]
        self.tb_text.setRowCount(len(rows))
        for r, (i, it) in enumerate(rows):
            for c, val in enumerate((str(i + 1), it.text, f"{it.score:.2f}")):
                item = QTableWidgetItem(val)
                if c == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.tb_text.setItem(r, c, item)

    def _on_text_row_dblclick(self, item: QTableWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx >= len(self._items):
            return
        it = self._items[idx]
        cx, cy = it.center
        self._do_click(cx, cy, f"「{it.text}」")

    def _copy_all_text(self) -> None:
        if not self._items:
            self._log("warn", "还没有识别结果")
            return
        text = "\n".join(it.text for it in self._items)
        QApplication.clipboard().setText(text)
        self._log("ok", f"已复制 {len(self._items)} 段文字到剪贴板")

    def _export_text(self) -> None:
        if not self._items:
            QMessageBox.information(self, "提示", "还没有可导出的识别结果")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出识别结果", str(DATA_DIR / f"识别结果_{time.strftime('%Y%m%d_%H%M%S')}.txt"),
            "文本文件 (*.txt);;CSV (*.csv)",
        )
        if not path:
            return
        try:
            if path.lower().endswith(".csv"):
                lines = ["序号,文字,置信度,X,Y,宽,高"]
                for i, it in enumerate(self._items, 1):
                    t = it.text.replace('"', '""')
                    lines.append(f'{i},"{t}",{it.score:.3f},{it.rect[0]},{it.rect[1]},{it.rect[2]},{it.rect[3]}')
                Path(path).write_text("\n".join(lines), encoding="utf-8-sig")
            else:
                lines = [f"{i}. {it.text}  [{it.center[0]},{it.center[1]}]" for i, it in enumerate(self._items, 1)]
                Path(path).write_text("\n".join(lines), encoding="utf-8")
            self._log("ok", f"识别结果已导出：{path}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))

    # ==================================================================
    # 找图
    # ==================================================================
    def _capture_template(self) -> None:
        self._timer.stop()
        hidden = self._hide_for_capture()
        img = capture_template(self)
        if hidden:
            self.showNormal()
            self.raise_()
            self.activateWindow()
        QApplication.processEvents()
        if self.chk_live.isChecked():
            self._timer.start()
        if img is None or img.size == 0:
            return
        name = f"tpl_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = TEMPLATE_DIR / name
        vision.save_image(path, img)
        self._template_path = str(path)
        pix = QPixmap(str(path))
        self.lbl_tpl.setPixmap(pix.scaled(82, 50, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation))
        self.lbl_tpl.setText("")
        self.lbl_match_stat.setText(f"模板：{name}（{img.shape[1]}×{img.shape[0]}）")
        self._log("ok", f"已截取图案模板：{name}")
        self.run_match()

    def run_match(self) -> None:
        if not self._template_path:
            QMessageBox.information(self, "提示", "请先用「截取图案」框选一个图标或按钮")
            return
        path = self._template_path
        try:
            region = self._current_region()
        except RuntimeError as exc:
            self._log("warn", str(exc))
            self._refresh_windows()
            return
        threshold = float(self.sp_tpl_thr.value())

        def work():
            t0 = time.perf_counter()
            img, origin = GRABBER.grab(region)
            tpl, mask = vision.load_template(path)
            hits = vision.find_template(
                img, tpl, mask, threshold=threshold, multi_scale=SETTINGS.multi_scale,
                max_results=SETTINGS.max_match_results, origin=origin,
            )
            return {"img": img, "origin": origin, "hits": hits, "cost": time.perf_counter() - t0}

        self._start_job(work, self._on_match_done, "正在查找图案…")

    def _on_match_done(self, data: dict) -> None:
        self._bgr, self._origin = data["img"], data["origin"]
        self._matches = data["hits"]
        self.preview.set_image(self._bgr, self._origin)
        self.preview.set_boxes([
            {"rect": m.rect, "label": f"#{i + 1} {m.score:.2f}", "kind": "image"}
            for i, m in enumerate(self._matches)
        ])
        self._box_targets = [(tuple(m.center), f"图案 #{i + 1}") for i, m in enumerate(self._matches)]
        self.tb_match.setRowCount(len(self._matches))
        for i, m in enumerate(self._matches):
            vals = (f"#{i + 1}", f"({m.center[0]}, {m.center[1]})", f"{m.score:.2f}")
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c != 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                item.setData(Qt.ItemDataRole.UserRole, i)
                self.tb_match.setItem(i, c, item)
        self.lbl_match_stat.setText(
            f"找到 {len(self._matches)} 处 · {data['cost']:.2f}s（双击结果可点击）"
            if self._matches else "没有找到匹配，试试降低相似度或重新截取模板"
        )
        self.statusBar().showMessage("就绪")
        self._log("ok" if self._matches else "warn",
                  f"找图完成：{len(self._matches)} 处匹配，用时 {data['cost']:.2f} 秒")

    def _on_match_row_dblclick(self, item: QTableWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or idx >= len(self._matches):
            return
        m = self._matches[idx]
        cx, cy = m.center
        self._do_click(cx, cy, f"图案 #{idx + 1}")

    # ==================================================================
    # 预览交互 → 真实点击
    # ==================================================================
    def _click_box(self, index: int) -> None:
        # v1.5：以"画框时记下的目标"为准。以前按并列列表取，找图后会误点到上一次 OCR 的文字位置。
        if 0 <= index < len(self._box_targets):
            pos, label = self._box_targets[index]
            self._do_click(pos[0], pos[1], label)
            return
        if 0 <= index < len(self._items):
            cx, cy = self._items[index].center
            self._do_click(cx, cy, f"「{self._items[index].text}」")
        elif 0 <= index < len(self._matches):
            cx, cy = self._matches[index].center
            self._do_click(cx, cy, f"图案 #{index + 1}")

    def _click_point(self, x: int, y: int) -> None:
        self._do_click(x, y, "指定位置")

    def _do_click(self, x: int, y: int, label: str) -> None:
        hid = False
        try:
            self.lbl_state.setText("点击中…")
            QApplication.processEvents()
            # v1.5 关键修复：预览里的那个位置，此刻很可能正被本程序自己的窗口盖住
            #（识别前自动最小化后截图，窗口又回来了）。直接点只会点到自己的界面，
            # 看起来就是"双击没反应"。先把窗口藏起来，点完再回来。
            if self.isVisible() and winapi.point_belongs_to_me(x, y):
                self.hide()
                QApplication.processEvents()
                time.sleep(0.18)
                hid = True
            # 目标窗口不在前台时，第一下点击经常只用来"激活窗口"、动作被吞掉。
            # 先把它切到前台，再点，双击预览就一定落地（桌面/任务栏不碰）。
            hwnd = winapi.window_at(x, y)
            if hwnd and not winapi.point_belongs_to_me(x, y) \
                    and not winapi.is_desktop_window(hwnd) \
                    and not winapi.foreground_is(hwnd):
                winapi.activate(hwnd, wait=0.12)
            actions.click(int(x), int(y), humanize=SETTINGS.humanize)
            self._log("ok", f"已点击 {label} @ ({x}, {y})")
        except Exception as exc:  # noqa: BLE001
            self._log("error", f"点击失败：{exc}")
        finally:
            if hid:
                self.showNormal()
                self.raise_()       # 不抢焦点：让刚被点的那个程序保持在前台
                QApplication.processEvents()
            self._set_running(self._running)

    # ==================================================================
    # 规则管理
    # ==================================================================
    def _refresh_rules(self) -> None:
        rules = self.engine.snapshot()
        counts = self.engine.counts()
        self.tb_rules.setRowCount(len(rules))
        for r, rule in enumerate(rules):
            chk = QCheckBox()
            chk.setChecked(rule.enabled)
            chk.stateChanged.connect(lambda st, i=r: self._toggle_rule_enabled(i, bool(st)))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(14, 0, 0, 0)
            hl.addWidget(chk)
            hl.addStretch(1)
            self.tb_rules.setCellWidget(r, 0, holder)

            n = counts.get(r, 0)
            done = bool(rule.max_times and n >= rule.max_times)
            win = rule.window_label()
            vals = (rule.name, win, rule.summary(), str(n) if n else "—", "已达上限" if done else "待运行")
            for c, v in enumerate(vals, start=1):
                item = QTableWidgetItem(v)
                if c in (2, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if done:
                    item.setForeground(QColor("#fbbf24"))
                if not rule.enabled:
                    item.setForeground(QColor("#5f6878"))
                self.tb_rules.setItem(r, c, item)

    def _toggle_rule_enabled(self, index: int, enabled: bool) -> None:
        rules = self.engine.snapshot()
        if 0 <= index < len(rules):
            rules[index].enabled = enabled
            self.engine.update(index, rules[index])
            self._refresh_rules()

    def _on_rule_item(self, index: int, times: int, disabled: bool) -> None:
        if index < self.tb_rules.rowCount():
            item = self.tb_rules.item(index, 4)
            if item:
                item.setText(str(times))
                if disabled:
                    item.setForeground(QColor("#fbbf24"))
                    status = self.tb_rules.item(index, 5)
                    if status:
                        status.setText("已达上限")
            if SETTINGS.toast_on_hit and times > 0 and getattr(self, "tray", None):
                nm = self.tb_rules.item(index, 1)
                label = nm.text() if nm else "规则"
                self.tray.showMessage(APP_NAME, f"命中：{label}（第 {times} 次）",
                                      QSystemTrayIcon.MessageIcon.Information, 2500)

    def _sync_quick_mode(self) -> None:
        mode = self.cb_qact.currentData()
        self.w_qat.setVisible(mode == "click_at")
        self.w_qmacro.setVisible(mode == "macro")
        self.lbl_qhint.setText({
            "click": "看到这段文字 → 直接点它",
            "click_at": "看到这段文字 → 去点你指定的那个位置",
            "macro": "看到这段文字 → 播放你录好的一整套动作",
        }.get(mode, ""))

    def _fill_qmacro(self) -> None:
        cur = self.cb_qmacro.currentData()
        self.cb_qmacro.blockSignals(True)
        self.cb_qmacro.clear()
        files = recorder.macro_files()
        if not files:
            self.cb_qmacro.addItem("（还没录制动作，去「录制动作」页录一个）", "")
        for f in files:
            try:
                desc = recorder.describe(recorder.load_macro(f))
            except Exception:  # noqa: BLE001
                desc = ""
            self.cb_qmacro.addItem(f"{f}　（{desc}）" if desc else f, f)
        if cur:
            i = self.cb_qmacro.findData(cur)
            if i >= 0:
                self.cb_qmacro.setCurrentIndex(i)
        self.cb_qmacro.blockSignals(False)

    def _pick_coords(self, spx: QSpinBox, spy: QSpinBox, chk_rel: QCheckBox) -> None:
        """全屏取点；勾了「相对窗口」就换算成目标窗口客户区坐标。"""
        self.hide()
        QApplication.processEvents()
        time.sleep(0.25)
        pt = pick_point(None, "把鼠标移到要点击的位置，单击取点；Esc / 右键取消")
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if not pt:
            return
        x, y = int(pt[0]), int(pt[1])
        if chk_rel.isChecked() and self._qwin_titles:
            rect = winapi.client_rect(winapi.find_by_title(self._qwin_titles[0]))
            if rect:
                x, y = x - rect[0], y - rect[1]
                self._log("info", f"已换算成窗口内坐标 ({x}, {y})")
        spx.setValue(x)
        spy.setValue(y)
        self._log("ok", f"已取到坐标 ({x}, {y})")

    def _add_quick_rule(self) -> None:
        """快捷任务：窗口 + 文字 + 动作 → 一条规则。"""
        text = self.ed_qtext.text().strip()
        if not text:
            QMessageBox.information(self, "提示", "请先填写要盯住的文字，例如「确定」")
            return
        win = self._qwin_titles[0] if self._qwin_titles else ""
        mode = self.cb_qact.currentData() or "click"
        rule = Rule(
            name=f"看到「{text}」就{ACTION_LABELS.get(mode, '点击')}",
            enabled=True, trigger="text", text=text, text_mode="contains",
            window=win, windows=list(self._qwin_titles), action=mode, cooldown=1.5, max_times=0,
        )
        rule.click_at_relative = bool(win) and self.chk_qrel.isChecked()
        if mode == "click_at":
            x, y = self.sp_qx.value(), self.sp_qy.value()
            if x == 0 and y == 0:
                QMessageBox.information(self, "提示", "请先点「在屏幕上点一下取坐标」，确定要点击的位置")
                return
            rule.click_at_x, rule.click_at_y = x, y
        elif mode == "macro":
            f = self.cb_qmacro.currentData()
            if not f:
                QMessageBox.information(self, "提示", "还没有录制动作，请先到「录制动作」页录一段")
                self.tabs.setCurrentWidget(self.recorder_view)
                return
            rule.macro = f
        self.engine.add(rule)
        self._refresh_rules()
        self.ed_qtext.clear()
        self._log("ok", f"已新增规则：{rule.name}" + (f"（窗口：{win}）" if win else "（全屏）"))
        self.tb_rules.selectRow(self.tb_rules.rowCount() - 1)
        if self.chk_qstart.isChecked() and not self.engine.is_running():
            self._start_engine()

    def _pick_timer_region(self) -> None:
        hidden = self._hide_for_capture()
        r = select_region(self)
        if hidden:
            self.showNormal()
            self.raise_()
            self.activateWindow()
        QApplication.processEvents()
        if r and sum(r[2:]) > 0:
            self._timer_region = list(r)
            self.lbl_tregion.setText(f"区域 {r[0]},{r[1]} {r[2]}×{r[3]}")
            self._log("ok", "已框选秒数区域，建议框得小一点、只留数字")
        else:
            self._log("warn", "没有框选秒数区域")

    def _add_timer_rule(self) -> None:
        r = self._timer_region
        if sum(r[2:]) <= 0:
            QMessageBox.information(self, "提示", "请先点「框选秒数区域」，把倒计时数字框进去")
            return
        x, y = self.sp_tx.value(), self.sp_ty.value()
        if x == 0 and y == 0:
            QMessageBox.information(self, "提示", "请先点「取坐标」，确定秒数停住后要点击的位置")
            return
        win = self._qwin_titles[0] if self._qwin_titles else ""
        rule = Rule(
            name=f"秒数停住 {self.sp_qstill.value():.1f} 秒就点击",
            enabled=True, trigger="timer", timer_seconds=round(self.sp_qstill.value(), 1),
            region=list(r), window=win, windows=list(self._qwin_titles), action="click_at",
            click_at_x=x, click_at_y=y, click_at_relative=bool(win) and self.chk_trel.isChecked(),
            cooldown=2.0, max_times=0,
        )
        if self._qwin_titles:
            # 框选的是某一瞬间的屏幕位置；多个窗口共用同一套时，按窗口内坐标算才准
            rect = winapi.client_rect(winapi.find_by_title(self._qwin_titles[0]))
            if rect:
                rule.region = [r[0] - rect[0], r[1] - rect[1], r[2], r[3]]
                rule.region_relative = True
            rule.capture = "window" if self._capture_ok_for(self._qwin_titles[0]) else "screen"
        self.engine.add(rule)
        self._refresh_rules()
        self._log("ok", f"已新增规则：{rule.name}")
        self.tb_rules.selectRow(self.tb_rules.rowCount() - 1)
        if self.chk_qstart.isChecked() and not self.engine.is_running():
            self._start_engine()

    # ---- 识别结果右键菜单：直接按「看到它就…」建规则 ----
    def _text_menu(self, pos) -> None:
        row = self.tb_text.rowAt(pos.y())
        if row < 0:
            return
        item = self.tb_text.item(row, 0)
        idx = item.data(Qt.ItemDataRole.UserRole) if item else None
        if idx is None or idx >= len(self._items):
            return
        it = self._items[idx]
        m = QMenu(self)
        acts = {}
        a_now = m.addAction(f"立即点击「{it.text}」")
        acts[a_now] = ("now", None)
        m.addSeparator()
        a_click = m.addAction("看到它就点它（建规则）")
        acts[a_click] = ("rule", "click")
        a_at = m.addAction("看到它就点别处（选位置）")
        acts[a_at] = ("rule", "click_at")
        a_macro = m.addAction("看到它就播放录制动作")
        acts[a_macro] = ("rule", "macro")
        m.addSeparator()
        a_copy = m.addAction("复制这段文字")
        acts[a_copy] = ("copy", None)
        chosen = m.exec(self.tb_text.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        kind, action = acts.get(chosen, (None, None))
        # v1.4：菜单完全关闭之后再执行。以前在菜单的事件循环里直接弹模态对话框
        # / 弹全屏取点，会被菜单的关闭动作连带关掉，看起来就是"弹窗一闪就消失"。
        if kind == "now":
            self._do_click(*it.center, label=f"「{it.text}」")
        elif kind == "rule":
            self._rule_from_hit("text", it.text, action)
        elif kind == "copy":
            QApplication.clipboard().setText(it.text)

    def _match_menu(self, pos) -> None:
        row = self.tb_match.rowAt(pos.y())
        if row < 0:
            return
        item = self.tb_match.item(row, 0)
        idx = item.data(Qt.ItemDataRole.UserRole) if item else None
        if idx is None or idx >= len(self._matches):
            return
        mch = self._matches[idx]
        m = QMenu(self)
        acts = {}
        a_now = m.addAction(f"立即点击 图案 #{idx + 1}")
        acts[a_now] = ("now", None)
        m.addSeparator()
        a_click = m.addAction("看到它就点它（建规则）")
        acts[a_click] = ("rule", "click")
        a_at = m.addAction("看到它就点别处（选位置）")
        acts[a_at] = ("rule", "click_at")
        a_macro = m.addAction("看到它就播放录制动作")
        acts[a_macro] = ("rule", "macro")
        a_newtpl = m.addAction("重新截取图案后再建规则")
        acts[a_newtpl] = ("retake", None)
        chosen = m.exec(self.tb_match.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        kind, action = acts.get(chosen, (None, None))
        if kind == "now":
            self._do_click(*mch.center, label=f"图案 #{idx + 1}")
        elif kind == "rule":
            self._rule_from_match(mch, action)
        elif kind == "retake":
            self._capture_template()

    def _rule_from_hit(self, trigger: str, text: str, action: str) -> None:
        win = self.target_window
        rule = Rule(
            name=f"看到「{text}」就{ACTION_LABELS.get(action, '点击')}",
            enabled=True, trigger="text", text=text, text_mode="contains",
            window=win, action=action, cooldown=1.5, max_times=0,
        )
        if not self._fill_rule_action(rule, win, action):
            return
        self.engine.add(rule)
        self._refresh_rules()
        self.tb_rules.selectRow(self.tb_rules.rowCount() - 1)
        self._log("ok", f"已建规则：{rule.name}　（按 F9 或勾选「加入后立即开始」即开跑）")

    def _rule_from_match(self, mch, action: str) -> None:
        # v1.4：以前这里写的是 self.ed_template（根本不存在的控件）→ AttributeError
        tpl = Path(self._template_path) if self._template_path else None
        if tpl is None or not tpl.is_file():
            QMessageBox.information(self, "提示", "请先截取一次图案模板，再右键结果建规则")
            return
        win = self.target_window
        rule = Rule(
            name=f"看到图案「{tpl.stem}」就{ACTION_LABELS.get(action, '点击')}",
            enabled=True, trigger="image", template=str(tpl),
            threshold=float(self.sp_tpl_thr.value()),
            window=win, action=action, cooldown=1.5, max_times=0,
        )
        # 「点别处 / 播动作」的默认值：点别处会弹取点框，播动作会拿它当默认选项
        if action == "click_at" and mch is not None:
            rule.click_at_x, rule.click_at_y = int(mch.center[0]), int(mch.center[1])
            rule.click_at_relative = False
        if action == "macro":
            files = recorder.macro_files()
            if files:
                rule.macro = files[0]
        # v1.7：这里不再强制弹编辑窗口 —— 右键截取图案后直接建规则。
        # 想改条件/窗口/间隔，建完在规则表里双击那一行再编辑即可。
        if not self._fill_rule_action(rule, win, action):
            return
        self.engine.add(rule)
        self._refresh_rules()
        self.tb_rules.selectRow(self.tb_rules.rowCount() - 1)
        self._log("ok", f"已建规则：{rule.name}　（按 F9 或勾选「加入后立即开始」即开跑）")

    def _fill_rule_action(self, rule: Rule, win: str, action: str) -> bool:
        """补齐「点别处 / 播动作」需要的参数，返回是否可用。"""
        if action == "click":
            return True
        if action == "click_at":
            self.hide()
            QApplication.processEvents()
            time.sleep(0.25)
            pt = pick_point(None, "把鼠标移到「命中后要点击」的位置，单击取点；Esc / 右键取消")
            self.showNormal()
            self.raise_()
            self.activateWindow()
            if not pt:
                return None
            x, y = int(pt[0]), int(pt[1])
            rel = bool(win)
            if rel:
                rect = winapi.client_rect(winapi.find_by_title(win))
                if rect:
                    x, y = x - rect[0], y - rect[1]
                else:
                    rel = False
            rule.click_at_x, rule.click_at_y, rule.click_at_relative = x, y, rel
            return True
        if action == "macro":
            files = recorder.macro_files()
            if not files:
                QMessageBox.information(self, "提示", "还没有录制动作，请先到「录制动作」页按 F8 录一段")
                self.tabs.setCurrentWidget(self.recorder_view)
                return None
            from PySide6.QtWidgets import QInputDialog

            labels = [f"{f}　（{recorder.describe(recorder.load_macro(f))}）" for f in files]
            choice, ok = QInputDialog.getItem(self, "选择动作", "命中后播放哪套动作？", labels, 0, False)
            if not ok:
                return None
            rule.macro = files[labels.index(choice)]
            return True
        return True

    def _auto_start_engine(self) -> None:
        if self.engine.is_running():
            return
        enabled = [r for r in self.engine.snapshot() if r.enabled]
        if not enabled:
            self._log("info", "没有启用的规则，启动自动运行已跳过")
            return
        self._log("info", f"启动即自动运行：{len(enabled)} 条已启用规则")
        self._start_engine()

    def _selected_rule_index(self) -> int:
        rows = self.tb_rules.selectionModel().selectedRows() if self.tb_rules.selectionModel() else []
        return rows[0].row() if rows else -1

    def _add_rule(self) -> None:
        dlg = RuleDialog(None, self)
        if dlg.exec() == QDialogResult.Accepted:
            self.engine.add(dlg.result_rule())
            self._refresh_rules()
            self._log("ok", f"已新增规则：{dlg.result_rule().name}")

    def _edit_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx < 0:
            QMessageBox.information(self, "提示", "请先选中一条规则")
            return
        rules = self.engine.snapshot()
        dlg = RuleDialog(rules[idx], self)
        if dlg.exec() == QDialogResult.Accepted:
            self.engine.update(idx, dlg.result_rule())
            self._refresh_rules()

    def _del_rule(self) -> None:
        idx = self._selected_rule_index()
        if idx < 0:
            return
        rules = self.engine.snapshot()
        if QMessageBox.question(self, "删除规则", f"确定删除「{rules[idx].name}」？") == QMessageBox.StandardButton.Yes:
            self.engine.remove(idx)
            self._refresh_rules()

    def _move_rule(self, delta: int) -> None:
        idx = self._selected_rule_index()
        if idx < 0:
            return
        new = self.engine.move(idx, delta)
        self._refresh_rules()
        self.tb_rules.selectRow(new)

    def _import_rules(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入规则", str(DATA_DIR), "JSON (*.json)")
        if not path:
            return
        try:
            import json

            data = json.loads(Path(path).read_text(encoding="utf-8"))
            rules = [Rule.from_dict(d) for d in data]
            self.engine.replace_all(rules)
            self._refresh_rules()
            self._log("ok", f"已导入 {len(rules)} 条规则")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", str(exc))

    def _export_rules(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出规则", str(DATA_DIR / "规则导出.json"), "JSON (*.json)")
        if not path:
            return
        import json

        rules = self.engine.snapshot()
        Path(path).write_text(json.dumps([r.to_dict() for r in rules], ensure_ascii=False, indent=2), encoding="utf-8")
        self._log("ok", f"规则已导出：{path}")

    # ==================================================================
    # 运行控制
    # ==================================================================
    def _start_engine(self) -> None:
        if self.engine.is_running():
            self._log("warn", "已经在运行了")
            return
        if not self.engine.snapshot():
            QMessageBox.information(self, "提示", "还没有规则，请先到「自动点击」页新增一条，或用上面的快捷任务生成。")
            self.tabs.setCurrentIndex(1)
            return
        self._user_stopped = False
        self.engine.start(dry_run=self.chk_dry.isChecked(), loop=self.chk_loop.isChecked())
        self.tabs.setCurrentIndex(1)

    def engine_stop(self) -> None:
        self._user_stopped = True
        if self.engine.is_running():
            self.engine.stop()

    # ==================================================================
    # 托盘 + 看门狗（长期挂机）
    # ==================================================================
    def _build_tray(self) -> None:
        icon = QIcon(str(asset_path("icon.ico")))
        if icon.isNull():
            icon = QIcon(str(asset_path("icon.png")))
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip(f"{APP_TITLE}　F9 开始 / F10 停止 / F8 录制")
        menu = QMenu()
        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self._show_main)
        act_start = QAction("开始执行 (F9)", self)
        act_start.triggered.connect(self._start_engine)
        act_stop = QAction("停止 (F10)", self)
        act_stop.triggered.connect(self.engine_stop)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit_app)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_start)
        menu.addAction(act_stop)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self._tray_action_show = act_show
        self._tray_action_start = act_start
        self._tray_action_stop = act_stop
        self.tray.activated.connect(self._on_tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self._tray_hint_shown = False

    def _on_tray_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self._show_main()

    def _show_main(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self) -> None:
        self._quitting = True
        if getattr(self, "tray", None):
            self.tray.hide()
        self.shutdown()
        QApplication.quit()

    def _start_watchdog(self) -> None:
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(5000)
        self._watchdog.timeout.connect(self._watchdog_tick)
        self._watchdog.start()

    def _watchdog_tick(self) -> None:
        """引擎意外停了（崩溃 / 被系统回收）且用户没主动按过 F10 → 自动恢复。"""
        if not SETTINGS.auto_recover or self._quitting or self._user_stopped:
            return
        if self.engine.is_running():
            return
        if not any(r.enabled for r in self.engine.snapshot()):
            return
        self._log("warn", "看门狗：发现规则引擎没在跑，自动恢复执行")
        self._start_engine()
        if not self.isVisible():
            self.tray.showMessage(APP_NAME, "已自动恢复执行（引擎曾被停止）", QSystemTrayIcon.MessageIcon.Information, 4000)

    # ==================================================================
    # 设置
    # ==================================================================
    def _save_settings(self) -> None:
        SETTINGS.ocr_min_score = round(self.sp_ocr_score.value(), 2)
        SETTINGS.multi_scale = self.chk_multi.isChecked()
        SETTINGS.max_match_results = self.sp_maxmatch.value()
        SETTINGS.click_delay_ms = self.sp_delay.value()
        SETTINGS.double_click_interval_ms = self.sp_dbl.value()
        SETTINGS.humanize = self.chk_human.isChecked()
        SETTINGS.loop_interval_s = round(self.sp_loop.value(), 1)
        SETTINGS.emergency_corner = self.chk_corner.isChecked()
        SETTINGS.save_log = self.chk_savelog.isChecked()
        SETTINGS.minimize_before_capture = self.chk_minimize.isChecked()
        SETTINGS.preview_interval_ms = self.sp_preview.value()
        SETTINGS.live_preview = self.chk_live_def.isChecked()
        SETTINGS.auto_start = self.chk_autostart.isChecked()
        SETTINGS.auto_recover = self.chk_autorecover.isChecked()
        SETTINGS.close_to_tray = self.chk_tray.isChecked()
        SETTINGS.toast_on_hit = self.chk_toast.isChecked()
        SETTINGS.ocr_upscale = round(self.sp_upscale.value(), 1)
        SETTINGS.frame_skip = self.chk_frameskip.isChecked()
        SETTINGS.save()
        self._timer.setInterval(SETTINGS.preview_interval_ms)
        self.chk_live.setChecked(SETTINGS.live_preview)
        self._log("ok", "设置已保存")

    # ==================================================================
    def shutdown(self) -> None:
        self._quitting = True
        try:
            self.recorder_view.shutdown()
        except Exception:
            pass
        try:
            self.engine.stop()
            self._hotkeys.stop()
            self._timer.stop()
            self._watchdog.stop()
            if getattr(self, "tray", None):
                self.tray.hide()
            SETTINGS.save()
        except Exception:
            pass

    def closeEvent(self, ev) -> None:  # noqa: N802
        if SETTINGS.close_to_tray and not self._quitting and getattr(self, "tray", None) and self.tray.isVisible():
            ev.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    APP_NAME, "已缩到托盘，规则继续在跑；右键托盘图标可以显示窗口或退出。",
                    QSystemTrayIcon.MessageIcon.Information, 4000,
                )
            return
        self.shutdown()
        super().closeEvent(ev)


from PySide6.QtWidgets import QDialog  # noqa: E402

QDialogResult = QDialog.DialogCode
