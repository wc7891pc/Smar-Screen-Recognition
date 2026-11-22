"""规则编辑对话框。"""
from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from config import MACRO_DIR, TEMPLATE_DIR
from core import recorder, vision
from core import windows as winapi
from core.capture import GRABBER
from core.ocr_engine import OCR, find_text
from core.rules import ACTION_LABELS, CAPTURE_LABELS, TRIGGER_LABELS, Rule
from core.timer import parse_seconds_strict
from ui.point_picker import pick_point
from ui.region_selector import capture_template, select_region


class WindowPickDialog(QDialog):
    """勾选窗口（可多选）：既用来给规则选"盯哪些窗口"，也用来选"参与自动化的窗口"。"""

    def __init__(self, selected=None, parent=None, title: str = "选择窗口"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(600, 460)
        self._selected = [str(s).strip() for s in (selected or []) if str(s).strip()]

        lay = QVBoxLayout(self)
        tip = QLabel("勾选要参与的窗口（可多选）。标题完全相同的多个窗口（比如两个游戏窗口）会各跑一遍。")
        tip.setObjectName("Muted")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.lst = QListWidget()
        self.lst.setAlternatingRowColors(True)
        lay.addWidget(self.lst, 1)

        row = QHBoxLayout()
        btn_refresh = QPushButton("刷新列表")
        btn_refresh.clicked.connect(self._fill)
        btn_all = QPushButton("全选")
        btn_all.clicked.connect(lambda: self._check_all(True))
        btn_none = QPushButton("全不选")
        btn_none.clicked.connect(lambda: self._check_all(False))
        row.addWidget(btn_refresh)
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(QLabel("额外关键字（可留空；手输的标题没在列表里也能用）："))
        self.ed_manual = QLineEdit()
        self.ed_manual.setPlaceholderText("例如 游戏窗口; 浏览器")
        lay.addWidget(self.ed_manual)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("确定")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._fill()

    def _fill(self) -> None:
        current = self.selected_titles()
        self.lst.clear()
        try:
            wins = winapi.list_windows(exclude_pid=os.getpid())
        except Exception:  # noqa: BLE001
            wins = []
        for w in wins:
            title = w["title"]
            item = QListWidgetItem(f"{title}　·　{w['exe'] or w['class']}")
            item.setData(Qt.ItemDataRole.UserRole, title)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = any(k.casefold() in title.casefold() for k in current)
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.lst.addItem(item)
        # 已选但当前没打开的窗口也列出来，避免"看不见就被丢掉"
        shown = [str(self.lst.item(i).data(Qt.ItemDataRole.UserRole) or "") for i in range(self.lst.count())]
        for title in current:
            if not any(title.casefold() in s.casefold() for s in shown):
                item = QListWidgetItem(f"{title}　（当前没打开，会继续等它出现）")
                item.setData(Qt.ItemDataRole.UserRole, title)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.lst.addItem(item)

    def _check_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self.lst.count()):
            self.lst.item(i).setCheckState(state)

    def selected_titles(self) -> list[str]:
        out: list[str] = []
        for i in range(self.lst.count()):
            item = self.lst.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                t = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
                if t and t not in out:
                    out.append(t)
        for t in str(self.ed_manual.text()).replace("；", ";").replace("，", ";").replace(",", ";").split(";"):
            t = t.strip()
            if t and t not in out:
                out.append(t)
        return out


class RuleDialog(QDialog):
    def __init__(self, rule: Rule | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑自动化规则")
        self.setMinimumWidth(560)
        self.resize(640, 720)
        self.rule = rule or Rule()
        self._region = list(self.rule.region)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 12, 16, 10)
        root.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;border:none;}")
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 6, 0)
        body_lay.setSpacing(10)

        # ---------------- 基本 ----------------
        basic = QGroupBox("基本信息")
        form = QFormLayout(basic)
        self.ed_name = QLineEdit(self.rule.name)
        self.ed_enabled = QCheckBox("启用（参与自动执行）")
        self.ed_enabled.setChecked(self.rule.enabled)
        form.addRow("规则名称", self.ed_name)
        form.addRow("", self.ed_enabled)
        body_lay.addWidget(basic)

        # ---------------- 触发条件 ----------------
        trig_box = QGroupBox("① 什么时候动手（触发条件）")
        trig_form = QFormLayout(trig_box)
        self.cb_trigger = QComboBox()
        for key, label in TRIGGER_LABELS.items():
            self.cb_trigger.addItem(label, key)
        self.cb_trigger.setCurrentIndex(max(0, self.cb_trigger.findData(self.rule.trigger)))

        self.ed_text = QLineEdit(self.rule.text)
        self.ed_text.setPlaceholderText("屏幕上出现的文字，如「确定」「开始游戏」")
        self.cb_text_mode = QComboBox()
        self.cb_text_mode.addItem("包含该文字", "contains")
        self.cb_text_mode.addItem("完全等于", "equals")
        self.cb_text_mode.addItem("正则表达式", "regex")
        self.cb_text_mode.setCurrentIndex(max(0, self.cb_text_mode.findData(self.rule.text_mode)))

        tpl_row = QHBoxLayout()
        self.ed_template = QLineEdit(self.rule.template)
        self.ed_template.setPlaceholderText("图案模板（PNG）")
        self.ed_template.setReadOnly(True)
        btn_pick_tpl = QPushButton("框选图案")
        btn_pick_tpl.clicked.connect(self._capture_template)
        btn_file_tpl = QPushButton("选文件")
        btn_file_tpl.clicked.connect(self._choose_template)
        tpl_row.addWidget(self.ed_template, 1)
        tpl_row.addWidget(btn_pick_tpl)
        tpl_row.addWidget(btn_file_tpl)

        self.lbl_tpl_preview = QLabel()
        self.lbl_tpl_preview.setFixedSize(96, 72)
        self.lbl_tpl_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_tpl_preview.setStyleSheet("border:1px solid #2f3540;border-radius:6px;background:#14161a;")

        reg_row = QHBoxLayout()
        self.lbl_region = QLabel()
        btn_region = QPushButton("框选屏幕区域")
        btn_region.clicked.connect(self._pick_region)
        btn_full = QPushButton("全屏")
        btn_full.clicked.connect(self._reset_region)
        reg_row.addWidget(self.lbl_region, 1)
        reg_row.addWidget(btn_region)
        reg_row.addWidget(btn_full)

        self.sp_threshold = QDoubleSpinBox()
        self.sp_threshold.setRange(0.40, 0.99)
        self.sp_threshold.setSingleStep(0.01)
        self.sp_threshold.setDecimals(2)
        self.sp_threshold.setValue(self.rule.threshold)

        # 秒数不动多久后触发（只在"秒数"条件下用）
        self.sp_timer = QDoubleSpinBox()
        self.sp_timer.setRange(0.5, 300.0)
        self.sp_timer.setSingleStep(0.5)
        self.sp_timer.setDecimals(1)
        self.sp_timer.setSuffix(" 秒")
        self.sp_timer.setValue(self.rule.timer_seconds)
        self.sp_timer.setToolTip("数字后面必须带「秒」字才会触发；框选区域要把「秒」也框进去")

        # 目标窗口（v1.6：可以一次选多个，这套规则会在每个窗口里各跑一遍）
        self._windows = list(self.rule.windows) or (
            [self.rule.window.strip()] if self.rule.window.strip() else []
        )
        self.ed_windows = QLineEdit("; ".join(self._windows))
        self.ed_windows.setPlaceholderText("留空 = 整个桌面；多个窗口用 ; 分隔")
        self.ed_windows.setMinimumWidth(240)
        btn_win_pick = QPushButton("选择窗口…")
        btn_win_pick.clicked.connect(self._pick_windows)
        self.lbl_win_hint = QLabel("可多选：一条规则 = 这几个窗口各跑一遍")
        self.lbl_win_hint.setObjectName("Muted")
        self.lbl_win_hint.setWordWrap(True)
        self.chk_activate = QCheckBox("操作前把目标窗口切到前台（点得准，多窗口建议开）")
        self.chk_activate.setChecked(self.rule.activate_window)
        self.chk_activate.setToolTip("扫描时不抢焦点；只在真的要点击/按键前切到那个窗口")
        win_row = QHBoxLayout()
        win_row.addWidget(self.ed_windows, 1)
        win_row.addWidget(btn_win_pick)

        # 识别方式：屏幕区域（需可见） / 窗口后台截图（被挡住也能识别）
        self.cb_capture = QComboBox()
        for key, label in CAPTURE_LABELS.items():
            self.cb_capture.addItem(label, key)
        self.cb_capture.setCurrentIndex(max(0, self.cb_capture.findData(self.rule.capture)))
        self.cb_capture.setToolTip(
            "多个窗口同时跑时推荐「窗口后台截图」：各抓各的画面，谁都不用抢前台。"
        )

        self.chk_region_rel = QCheckBox("搜索区域按窗口内坐标算（多个窗口共用同一区域时勾上）")
        self.chk_region_rel.setChecked(self.rule.region_relative)

        trig_form.addRow("识别类型", self.cb_trigger)
        trig_form.addRow("目标窗口", win_row)
        trig_form.addRow("", self.lbl_win_hint)
        trig_form.addRow("", self.chk_activate)
        trig_form.addRow("目标文字", self.ed_text)
        trig_form.addRow("匹配方式", self.cb_text_mode)
        trig_form.addRow("图案模板", tpl_row)
        trig_form.addRow("模板预览", self.lbl_tpl_preview)
        trig_form.addRow("搜索区域", reg_row)
        trig_form.addRow("相似度阈值", self.sp_threshold)
        trig_form.addRow("数字不动多久（须带秒）", self.sp_timer)
        trig_form.addRow("识别方式", self.cb_capture)
        trig_form.addRow("", self.chk_region_rel)
        self._trig_form = trig_form
        body_lay.addWidget(trig_box)

        # ---------------- 动作 ----------------
        act_box = QGroupBox("② 命中后做什么（动作）")
        act_form = QFormLayout(act_box)
        self.cb_action = QComboBox()
        for key, label in ACTION_LABELS.items():
            self.cb_action.addItem(label, key)
        self.cb_action.setCurrentIndex(max(0, self.cb_action.findData(self.rule.action)))

        self.cb_button = QComboBox()
        self.cb_button.addItem("鼠标左键", "left")
        self.cb_button.addItem("鼠标右键", "right")
        self.cb_button.addItem("鼠标中键", "middle")
        self.cb_button.setCurrentIndex(max(0, self.cb_button.findData(self.rule.click_button)))

        off_row = QHBoxLayout()
        self.sp_off_x = QSpinBox()
        self.sp_off_y = QSpinBox()
        for sp in (self.sp_off_x, self.sp_off_y):
            sp.setRange(-4000, 4000)
            sp.setSuffix(" px")
        self.sp_off_x.setValue(self.rule.offset_x)
        self.sp_off_y.setValue(self.rule.offset_y)
        off_row.addWidget(QLabel("X"))
        off_row.addWidget(self.sp_off_x)
        off_row.addWidget(QLabel("Y"))
        off_row.addWidget(self.sp_off_y)

        self.ed_param = QLineEdit(self.rule.param)
        self.cb_order = QComboBox()
        self.cb_order.addItem("第一个匹配", "first")
        self.cb_order.addItem("最后一个匹配", "last")
        self.cb_order.addItem("随机一个", "random")
        self.cb_order.setCurrentIndex(max(0, self.cb_order.findData(self.rule.order)))

        self.sp_cooldown = QDoubleSpinBox()
        self.sp_cooldown.setRange(0.05, 60)
        self.sp_cooldown.setSingleStep(0.1)
        self.sp_cooldown.setDecimals(2)
        self.sp_cooldown.setSuffix(" 秒")
        self.sp_cooldown.setValue(self.rule.cooldown)

        self.sp_max = QSpinBox()
        self.sp_max.setRange(0, 100000)
        self.sp_max.setSpecialValueText("不限")
        self.sp_max.setValue(self.rule.max_times)

        # ---- 动作：点击"另一个位置" ----
        self.sp_at_x = QSpinBox()
        self.sp_at_y = QSpinBox()
        for sp in (self.sp_at_x, self.sp_at_y):
            sp.setRange(-8000, 8000)
            sp.setSuffix(" px")
        self.sp_at_x.setValue(self.rule.click_at_x)
        self.sp_at_y.setValue(self.rule.click_at_y)
        btn_pick_at = QPushButton("在屏幕上点一下取坐标")
        btn_pick_at.clicked.connect(self._pick_at_point)
        at_row = QHBoxLayout()
        at_row.addWidget(QLabel("X"))
        at_row.addWidget(self.sp_at_x)
        at_row.addWidget(QLabel("Y"))
        at_row.addWidget(self.sp_at_y)
        at_row.addWidget(btn_pick_at)
        at_row.addStretch(1)
        self.chk_relative = QCheckBox("坐标相对目标窗口客户区（推荐：窗口移动后也点得准）")
        self.chk_relative.setChecked(self.rule.click_at_relative)

        # ---- 动作：播放录制动作 ----
        self.cb_macro = QComboBox()
        self.cb_macro.setMinimumWidth(220)
        self._fill_macros(self.rule.macro)
        btn_refresh_macro = QPushButton("刷新")
        btn_refresh_macro.clicked.connect(lambda: self._fill_macros(self._macro_value()))
        self.sp_macro_speed = QDoubleSpinBox()
        self.sp_macro_speed.setRange(0.2, 10.0)
        self.sp_macro_speed.setSingleStep(0.5)
        self.sp_macro_speed.setDecimals(1)
        self.sp_macro_speed.setSuffix("×")
        self.sp_macro_speed.setValue(self.rule.macro_speed)
        macro_row = QHBoxLayout()
        macro_row.addWidget(self.cb_macro, 1)
        macro_row.addWidget(btn_refresh_macro)
        macro_row.addWidget(QLabel("倍速"))
        macro_row.addWidget(self.sp_macro_speed)

        act_form.addRow("动作类型", self.cb_action)
        act_form.addRow("点击位置", at_row)
        act_form.addRow("", self.chk_relative)
        act_form.addRow("动作文件", macro_row)
        act_form.addRow("鼠标按键", self.cb_button)
        act_form.addRow("点击偏移", off_row)
        act_form.addRow("参数", self.ed_param)
        act_form.addRow("选择目标", self.cb_order)
        act_form.addRow("动作间隔", self.sp_cooldown)
        act_form.addRow("最多执行次数", self.sp_max)
        self._act_form = act_form
        body_lay.addWidget(act_box)
        body_lay.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        # ---------------- 底部 ----------------
        bottom = QHBoxLayout()
        btn_test = QPushButton("测试识别（不操作）")
        btn_test.clicked.connect(self._test)
        bottom.addWidget(btn_test)
        bottom.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        bb.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        bottom.addWidget(bb)
        root.addLayout(bottom)

        self.cb_trigger.currentIndexChanged.connect(self._sync_enabled)
        self.cb_action.currentIndexChanged.connect(self._sync_enabled)
        self._sync_enabled()
        self._show_region()
        self._show_template()

    # ------------------------------------------------------------------
    def _pick_windows(self) -> None:
        dlg = WindowPickDialog(self._windows_value(), self, "这条规则要盯哪些窗口（可多选）")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._windows = dlg.selected_titles()
        self.ed_windows.setText("; ".join(self._windows))

    def _windows_value(self) -> list[str]:
        raw = str(self.ed_windows.text()).replace("；", ";").replace("，", ";").replace(",", ";")
        out: list[str] = []
        for t in raw.split(";"):
            t = t.strip()
            if t and t not in out:
                out.append(t)
        return out

    # ------------------------------------------------------------------
    def _fill_macros(self, keep: str = "") -> None:
        self.cb_macro.blockSignals(True)
        self.cb_macro.clear()
        self.cb_macro.addItem("（还没录制动作，去「录制动作」页录一个）", "")
        for p in recorder.macro_files():
            self.cb_macro.addItem(f"{p.stem}　（{recorder.describe(recorder.load_macro(p.name))}）", p.name)
        idx = self.cb_macro.findData(keep) if keep else 0
        self.cb_macro.setCurrentIndex(max(0, idx))
        self.cb_macro.blockSignals(False)

    def _macro_value(self) -> str:
        return str(self.cb_macro.currentData() or "")

    def _pick_at_point(self) -> None:
        """去屏幕上点一下，把坐标填进来（勾了"相对窗口"就自动换算成相对坐标）。"""
        self.hide()
        time.sleep(0.3)
        pt = pick_point(None, "移动鼠标到要点击的位置，单击取点；Esc / 右键取消")
        self.show()
        self.raise_()
        if not pt:
            return
        x, y = pt
        wins = self._windows_value()
        if self.chk_relative.isChecked() and wins:
            hwnd = winapi.find_by_title(wins[0])
            rect = winapi.client_rect(hwnd) if hwnd else None
            if rect:
                x, y = x - rect[0], y - rect[1]
        self.sp_at_x.setValue(int(x))
        self.sp_at_y.setValue(int(y))

    def _sync_enabled(self) -> None:
        """只显示当前触发/动作真正会用到的行 —— 界面越简单越好上手。"""
        t = self.cb_trigger.currentData()
        a = self.cb_action.currentData()

        def rows(form, visible: set[int]) -> None:
            for i in range(form.rowCount()):
                try:
                    form.setRowVisible(i, i in visible)
                except Exception:  # noqa: BLE001  老版本 Qt 回退成禁用
                    pass

        # 触发条件行号：0 识别类型 / 1 目标窗口 / 2 提示 / 3 切前台 / 4 目标文字 / 5 匹配方式
        # 6 图案模板 / 7 模板预览 / 8 搜索区域 / 9 相似度 / 10 数字不动 / 11 识别方式 / 12 区域相对
        vis_t = {0}
        if t == "text":
            vis_t |= {1, 2, 3, 4, 5, 8, 11, 12}
        elif t == "image":
            vis_t |= {1, 2, 3, 6, 7, 8, 9, 11, 12}
        elif t == "timer":
            vis_t |= {1, 2, 3, 8, 10, 11, 12}
        rows(self._trig_form, vis_t)

        # 动作行号：0 动作类型 / 1 点击位置 / 2 相对窗口 / 3 动作文件 / 4 鼠标按键
        # 5 点击偏移 / 6 参数 / 7 选择目标 / 8 动作间隔 / 9 最多次数
        vis_a = {0, 8, 9}
        if a in ("click", "double_click", "right_click", "click_all"):
            vis_a |= {4, 5, 7}
        elif a == "click_at":
            vis_a |= {1, 2}
        elif a == "macro":
            vis_a |= {3, 8}
        elif a in ("type", "key", "scroll"):
            vis_a |= {6}
            if a == "type":
                vis_a |= {4}
        rows(self._act_form, vis_a)

        self.ed_text.setEnabled(t == "text")
        self.cb_text_mode.setEnabled(t == "text")
        self.ed_template.setEnabled(t == "image")
        self.sp_threshold.setEnabled(t == "image")
        self.lbl_tpl_preview.setEnabled(t == "image")
        self.sp_timer.setEnabled(t == "timer")
        self.ed_windows.setEnabled(t != "fixed")
        self.chk_activate.setEnabled(t != "fixed")
        self.cb_capture.setEnabled(t != "fixed")
        self.chk_region_rel.setEnabled(t != "fixed")
        self.cb_button.setEnabled(a in ("click", "double_click", "right_click", "click_all", "type"))
        self.cb_order.setEnabled(a in ("click", "double_click", "right_click", "click_all"))
        self.ed_param.setEnabled(a in ("type", "key", "scroll", "click_all"))
        for w in (self.sp_at_x, self.sp_at_y, self.chk_relative):
            w.setEnabled(a == "click_at")
        self.cb_macro.setEnabled(a == "macro")
        self.sp_macro_speed.setEnabled(a == "macro")
        placeholder = {
            "type": "要输入的文本，支持中文",
            "key": "按键或组合键，如 enter / ctrl+s / f5",
            "scroll": "滚动格数，如 3 或 -3",
            "click_all": "每次点击之间的间隔秒数，如 0.2",
        }.get(a, "")
        self.ed_param.setPlaceholderText(placeholder)
        hint = {
            "click": "在命中的位置单击",
            "double_click": "在命中的位置双击",
            "right_click": "在命中的位置右键",
            "click_all": "把所有匹配到的位置依次点一遍",
            "click_at": "不管命中在哪里，都去点上面填的那个位置",
            "macro": "播放录制好的鼠标动作（一串点击 / 滚轮 / 按键）",
            "type": "点击目标位置后输入文本",
            "key": "点击目标位置后按键",
            "scroll": "移动到目标位置后滚动滚轮",
            "move": "只把鼠标移过去，不点击",
            "wait": "只做识别，不操作（用于观察）",
        }.get(a, "")
        self.ed_param.setToolTip(hint)

    def _show_region(self) -> None:
        if sum(self._region[2:]) > 0:
            self.lbl_region.setText(
                f"X={self._region[0]}  Y={self._region[1]}  {self._region[2]}×{self._region[3]}"
            )
        else:
            self.lbl_region.setText("整个屏幕")

    def _show_template(self) -> None:
        path = self.ed_template.text().strip()
        if not path:
            self.lbl_tpl_preview.setText("未选择")
            self.lbl_tpl_preview.setPixmap(QPixmap())
            return
        p = Path(path)
        if not p.is_absolute():
            p = TEMPLATE_DIR / path
        if p.exists():
            pix = QPixmap(str(p))
            if pix.isNull():
                self.lbl_tpl_preview.setText("无法预览")
            else:
                self.lbl_tpl_preview.setPixmap(
                    pix.scaled(92, 68, Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation)
                )
                self.lbl_tpl_preview.setText("")
        else:
            self.lbl_tpl_preview.setText("文件不存在")

    # ------------------------------------------------------------------
    def _capture_template(self) -> None:
        parent = self.parent()
        if parent is not None:
            parent.hide()
            time.sleep(0.35)
        img = capture_template(None)
        if parent is not None:
            parent.show()
            parent.raise_()
        if img is None or img.size == 0:
            return
        name = f"tpl_{time.strftime('%Y%m%d_%H%M%S')}.png"
        path = TEMPLATE_DIR / name
        vision.save_image(path, img)
        self.ed_template.setText(name)
        self._show_template()

    def _choose_template(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择模板图片", str(TEMPLATE_DIR), "图片 (*.png *.jpg *.bmp)")
        if path:
            self.ed_template.setText(path)
            self._show_template()

    def _pick_region(self) -> None:
        parent = self.parent()
        if parent is not None:
            parent.hide()
            time.sleep(0.35)
        rect = select_region(None, "拖动框选规则要搜索的屏幕区域，Esc / 右键取消")
        if parent is not None:
            parent.show()
            parent.raise_()
        if rect:
            self._region = list(rect)
            self._show_region()

    def _reset_region(self) -> None:
        self._region = [0, 0, 0, 0]
        self._show_region()

    # ------------------------------------------------------------------
    def _collect(self) -> Rule:
        wins = self._windows_value()
        return Rule(
            name=self.ed_name.text().strip() or "新规则",
            enabled=self.ed_enabled.isChecked(),
            trigger=self.cb_trigger.currentData(),
            text=self.ed_text.text().strip(),
            text_mode=self.cb_text_mode.currentData(),
            template=self.ed_template.text().strip(),
            region=list(self._region),
            window=wins[0] if wins else "",
            windows=list(wins),
            capture=self.cb_capture.currentData(),
            region_relative=self.chk_region_rel.isChecked(),
            activate_window=self.chk_activate.isChecked(),
            threshold=round(self.sp_threshold.value(), 2),
            action=self.cb_action.currentData(),
            click_button=self.cb_button.currentData(),
            offset_x=self.sp_off_x.value(),
            offset_y=self.sp_off_y.value(),
            param=self.ed_param.text().strip(),
            cooldown=round(self.sp_cooldown.value(), 2),
            max_times=self.sp_max.value(),
            order=self.cb_order.currentData(),
            note="",
            click_at_x=self.sp_at_x.value(),
            click_at_y=self.sp_at_y.value(),
            click_at_relative=self.chk_relative.isChecked(),
            macro=self._macro_value(),
            macro_speed=round(float(self.sp_macro_speed.value()), 1),
            timer_seconds=round(float(self.sp_timer.value()), 1),
        )

    def _test(self) -> None:
        rule = self._collect()
        try:
            if rule.trigger == "text" and not rule.text:
                QMessageBox.information(self, "提示", "请先填写要识别的文字")
                return
            if rule.trigger == "image" and not rule.template:
                QMessageBox.information(self, "提示", "请先框选或选择图案模板")
                return
            if rule.trigger == "timer" and sum(rule.region[2:]) <= 0:
                QMessageBox.information(self, "提示", "请用「框选屏幕区域」把秒数所在的一小块框出来")
                return
            parent = self.parent()
            if parent is not None:
                parent.hide()
                time.sleep(0.4)
            region = tuple(rule.region)
            wins = rule.windows or ([rule.window] if rule.window.strip() else [])
            if wins:
                hwnd = winapi.find_by_title(wins[0])
                if not hwnd:
                    if parent is not None:
                        parent.show()
                        parent.raise_()
                    QMessageBox.warning(self, "测试结果", f"没找到窗口「{wins[0]}」，请点「选择窗口…」重新选。")
                    return
                if rule.capture == "window":
                    cap = winapi.capture_client(hwnd)
                    if cap is None:
                        if parent is not None:
                            parent.show()
                            parent.raise_()
                        QMessageBox.warning(self, "测试结果", "这个窗口不支持后台截图，请把「识别方式」改成「屏幕区域」。")
                        return
                    img, origin = cap
                else:
                    winapi.activate(hwnd, wait=0.4)
                    time.sleep(0.2)
                    region = winapi.client_rect(hwnd) or region
                    img, origin = GRABBER.grab(region if sum(region[2:]) > 0 else None)
                if img is not None and sum(region[2:]) > 0 and origin is not None:
                    if rule.capture == "window":
                        # 按「搜索区域」裁剪（相对窗口或绝对坐标都支持）
                        x0, y0 = int(origin[0]), int(origin[1])
                        rx, ry, rw, rh = (int(v) for v in rule.region)
                        dx, dy = (rx, ry) if rule.region_relative else (rx - x0, ry - y0)
                        x1, y1 = max(0, dx), max(0, dy)
                        x2, y2 = min(img.shape[1], dx + rw), min(img.shape[0], dy + rh)
                        if x2 - x1 > 4 and y2 - y1 > 4:
                            img = img[y1:y2, x1:x2]
                            origin = (x0 + x1, y0 + y1)
            else:
                img, origin = GRABBER.grab(region if sum(region[2:]) > 0 else None)
            if parent is not None:
                parent.show()
                parent.raise_()
            if rule.trigger == "image":
                path = Path(rule.template)
                if not path.is_absolute():
                    path = TEMPLATE_DIR / path
                tpl, mask = vision.load_template(path)
                hits = vision.find_template(img, tpl, mask, threshold=rule.threshold or 0.85,
                                            multi_scale=True, origin=origin, max_results=20)
                count, detail = len(hits), " / ".join(f"({m.center[0]},{m.center[1]}) 相似度{m.score:.2f}" for m in hits[:5])
            elif rule.trigger == "text":
                items = OCR.recognize(img, origin=origin)
                hits = find_text(items, rule.text, rule.text_mode)
                count, detail = len(hits), " / ".join(f"「{i.text}」({i.center[0]},{i.center[1]})" for i in hits[:5])
            elif rule.trigger == "timer":
                items = OCR.recognize(img, origin=origin)
                text = " ".join(i.text for i in items)
                secs, why = parse_seconds_strict(text)
                count = 1 if secs is not None else 0
                if secs is not None:
                    detail = (f"读到「{text}」→ {secs} 秒（数字后面带「秒」，"
                              f"数字不动 {rule.timer_seconds:g} 秒后就会触发）")
                elif why == "no_marker":
                    detail = (f"识别到的文字是「{text}」：有数字，但数字后面没有「秒」字，"
                              "所以不会触发。请把框选区域拉大一点，把数字后面的「秒」也框进来。")
                else:
                    detail = (f"识别到的文字是「{text}」，没读到数字：把区域框小一点、"
                              "只框倒计时那一小块再试")
            else:
                count, detail = 1, f"固定坐标 ({rule.region[0]},{rule.region[1]})"
            if count:
                QMessageBox.information(self, "测试结果", f"找到 {count} 处匹配：\n{detail}")
            else:
                QMessageBox.warning(self, "测试结果", "没有找到匹配目标。可以降低相似度阈值、扩大搜索区域，或确认目标当前是否在屏幕上。")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "测试失败", f"{exc}")

    def _on_ok(self) -> None:
        t = self.cb_trigger.currentData()
        if t == "text" and not self.ed_text.text().strip():
            QMessageBox.information(self, "提示", "请填写目标文字")
            return
        if t == "image" and not self.ed_template.text().strip():
            QMessageBox.information(self, "提示", "请先设置图案模板")
            return
        if t == "timer" and sum(self._region[2:]) <= 0:
            QMessageBox.information(self, "提示", "「秒数」条件需要先框选秒数所在的区域")
            return
        if self.cb_action.currentData() == "macro" and not self._macro_value():
            QMessageBox.information(self, "提示", "请先选一个录制好的动作文件（去「录制动作」页录制）")
            return
        self.rule = self._collect()
        self.accept()

    def result_rule(self) -> Rule:
        return self.rule
