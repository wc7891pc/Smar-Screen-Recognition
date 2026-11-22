"""自动化规则模型与执行引擎：识别到文字/图案 → 执行点击、输入等动作。

v1.6：一条规则可以同时盯多个窗口 —— 每个窗口一条独立的"通道"，
      各自识别、各自算冷却，互不阻塞；也可以只盯某一个窗口。
"""
from __future__ import annotations

import copy
import json
import queue
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from config import LOG_DIR, RULES_FILE, SETTINGS, TEMPLATE_DIR
from core import actions, recorder
from core.capture import GRABBER
from core.ocr_engine import OCR, find_text
from core.timer import TimerState, parse_seconds_strict
from core.vision import find_template, load_template

ACTION_LABELS = {
    "click": "单击目标",
    "double_click": "双击目标",
    "right_click": "右键目标",
    "click_all": "点击全部匹配",
    "click_at": "点击指定位置",
    "macro": "播放录制动作",
    "type": "输入文本",
    "key": "按下按键",
    "scroll": "滚轮滚动",
    "move": "仅移动鼠标",
    "wait": "仅等待",
}

TRIGGER_LABELS = {
    "image": "图案（找图）",
    "text": "文字（OCR）",
    "timer": "秒数（数字不动）",
    "fixed": "固定坐标",
}

CAPTURE_LABELS = {
    "screen": "屏幕区域（窗口需要可见）",
    "window": "窗口后台截图（被挡住也能识别）",
}


def _split_titles(value) -> List[str]:
    """把 "窗口A; 窗口B" 这类文本拆成标题列表。"""
    if isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        raw = str(value or "").replace("；", ";").replace("，", ";").replace(",", ";").split(";")
    out: List[str] = []
    for v in raw:
        t = str(v).strip()
        if t and t not in out:
            out.append(t)
    return out


@dataclass
class Rule:
    name: str = "新规则"
    enabled: bool = True
    trigger: str = "image"
    text: str = ""
    text_mode: str = "contains"
    template: str = ""
    region: List[int] = field(default_factory=lambda: [0, 0, 0, 0])  # 0,0,0,0=全屏
    window: str = ""              # 目标窗口标题关键字（空 = 整个桌面）；兼容旧版单窗口
    windows: List[str] = field(default_factory=list)  # v1.6：多个窗口，每个窗口各跑一遍
    activate_window: bool = True  # 动作执行前把目标窗口切到前台（多窗口时很关键）
    capture: str = "screen"       # screen=抓屏幕区域；window=抓窗口自身画面（可被遮挡）
    region_relative: bool = False # True = 上面的"搜索区域"按目标窗口客户区内坐标算
    threshold: float = 0.85
    action: str = "click"
    click_button: str = "left"
    offset_x: int = 0
    offset_y: int = 0
    param: str = ""
    cooldown: float = 1.0
    max_times: int = 0
    order: str = "first"      # first / last / random
    note: str = ""
    # ---- v1.1：点击"另一个位置" / 播放录制动作 / 秒数不动 ----
    click_at_x: int = 0              # 目标坐标（屏幕绝对 或 窗口客户区内相对）
    click_at_y: int = 0
    click_at_relative: bool = True   # True = 相对目标窗口客户区；没有窗口时按屏幕绝对坐标
    macro: str = ""                  # 录制动作文件名（data/macros/xxx.json）
    macro_speed: float = 1.0         # 回放倍速
    timer_seconds: float = 3.0       # 秒数静置多少秒后触发

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Rule":
        known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
        if "windows" in known:
            known["windows"] = _split_titles(known["windows"])
        rule = cls(**known)
        if not rule.windows and rule.window.strip():
            # 老规则只有单个窗口字段，升级成多窗口列表（只含这一个）
            rule.windows = [rule.window.strip()]
        return rule

    def target_titles(self) -> List[str]:
        """这条规则要盯的窗口标题列表；返回 [""] 表示整个桌面。"""
        titles = _split_titles(self.windows)
        if titles:
            return titles
        single = self.window.strip()
        return [single] if single else [""]

    def window_label(self, max_len: int = 46) -> str:
        titles = [t for t in self.target_titles() if t]
        if not titles:
            return "全屏"
        text = "；".join(titles)
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text

    def summary(self) -> str:
        if self.trigger == "image":
            t = Path(self.template).name if self.template else "（未选模板）"
            trig = f"图案 {t}"
        elif self.trigger == "text":
            trig = f"文字「{self.text}」"
        elif self.trigger == "timer":
            trig = f"秒数静止 {self.timer_seconds:g}s"
        else:
            trig = f"坐标 {self.region[:2]}"
        act = ACTION_LABELS.get(self.action, self.action)
        if self.action == "type":
            act += f"「{self.param}」"
        elif self.action in ("key",):
            act += f"「{self.param}」"
        elif self.action == "click_at":
            act += f" ({self.click_at_x}, {self.click_at_y})"
        elif self.action == "macro":
            act += f"「{Path(self.macro).stem or '未选'}」"
        return f"{trig} → {act}"


def _invert_for_ocr(img):
    """深色界面上的亮色文字，OCR 检测器常常漏掉；反色成"浅底深字"再识别一次。"""
    import cv2

    if img is None or img.size == 0:
        return None
    gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    inv = cv2.bitwise_not(gray)
    return cv2.cvtColor(inv, cv2.COLOR_GRAY2BGR)


class RuleEngine:
    """在后台线程循环扫描屏幕，命中条件即执行动作。"""

    def __init__(
        self,
        emit: Callable[[str, dict], None],
        rules_file: Path = RULES_FILE,
    ) -> None:
        self._emit = emit                 # 回调：("log", {...}) / ("state", {...})
        self._rules: List[Rule] = []
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._dry_run = False
        self._loop_mode = True
        # 运行时状态都以 (规则序号, 窗口句柄) 为键：一个窗口一条通道
        self._next_run: dict[tuple, float] = {}
        self._counts: dict[tuple, int] = {}
        self._tpl_cache: dict[str, tuple[float, tuple]] = {}
        self._win_warn: dict[tuple, float] = {}
        self._win_log: dict[int, float] = {}
        self._active_hwnd: int = 0
        self._active_at: float = 0.0
        # ---- v1.1：画面指纹缓存（没变化就不重复识别）+ 秒数状态 ----
        self._frame_cache: dict[tuple, tuple] = {}
        self._timer_state: dict[tuple, TimerState] = {}
        self._timer_sig: dict[tuple, tuple] = {}
        self._timer_warn: dict[tuple, float] = {}
        self._timer_marker_warn: dict[tuple, float] = {}
        # ---- v1.2：录制动作放到独立线程播，规则照常继续扫（两件事同时干）----
        self._macro_q: "queue.Queue[tuple]" = queue.Queue()
        self._macro_thread: Optional[threading.Thread] = None
        self._macro_busy: set[str] = set()
        self._macro_lock = threading.Lock()
        self.rules_file = rules_file
        self.load()

    def _reset_runtime_state(self) -> None:
        self._frame_cache.clear()
        self._timer_state.clear()
        self._timer_sig.clear()
        self._timer_warn.clear()
        self._timer_marker_warn.clear()
        self._win_warn.clear()

    # ------------------------- 规则存取 -------------------------
    def load(self) -> None:
        if self.rules_file.exists():
            try:
                data = json.loads(self.rules_file.read_text(encoding="utf-8"))
                with self._lock:
                    self._rules = [Rule.from_dict(d) for d in data]
            except Exception as exc:
                self._emit("log", {"level": "warn", "text": f"规则文件读取失败：{exc}"})
        else:
            with self._lock:
                self._rules = _default_rules()

    def save(self) -> None:
        with self._lock:
            data = [r.to_dict() for r in self._rules]
        self.rules_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def snapshot(self) -> List[Rule]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._rules]

    def replace_all(self, rules: List[Rule]) -> None:
        with self._lock:
            self._rules = [copy.deepcopy(r) for r in rules]
            self._counts.clear()
            self._next_run.clear()
        self._reset_runtime_state()
        self.save()

    def add(self, rule: Rule) -> None:
        with self._lock:
            self._rules.append(rule)
        self._reset_runtime_state()
        self.save()

    def update(self, index: int, rule: Rule) -> None:
        with self._lock:
            if 0 <= index < len(self._rules):
                self._rules[index] = rule
        self._reset_runtime_state()
        self.save()

    def remove(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._rules):
                self._rules.pop(index)
        self._reset_runtime_state()
        self.save()

    def move(self, index: int, delta: int) -> int:
        with self._lock:
            new = index + delta
            if 0 <= index < len(self._rules) and 0 <= new < len(self._rules):
                self._rules[index], self._rules[new] = self._rules[new], self._rules[index]
                return new
        return index

    def counts(self) -> dict[int, int]:
        """按规则汇总（多窗口时把各个窗口的次数加起来）。"""
        with self._lock:
            agg: dict[int, int] = {}
            for key, n in self._counts.items():
                idx = key[0] if isinstance(key, tuple) else int(key)
                agg[idx] = agg.get(idx, 0) + int(n)
            return agg

    def _total_count(self, index: int) -> int:
        with self._lock:
            return sum(
                int(n) for key, n in self._counts.items()
                if (key[0] if isinstance(key, tuple) else int(key)) == index
            )

    # ------------------------- 运行控制 -------------------------
    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, dry_run: bool = False, loop: bool = True) -> bool:
        if self.is_running():
            # 上一次 stop() 还没退出完毕，给它一点时间收尾
            self._stop.set()
            if self._thread is not None:
                self._thread.join(1.5)
            if self.is_running():
                return False
        with self._lock:
            self._counts.clear()
            self._next_run.clear()
        self._reset_runtime_state()
        self._dry_run = dry_run
        self._loop_mode = loop
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="RuleEngine", daemon=True)
        self._thread.start()
        self._emit("state", {"running": True, "dry_run": dry_run, "loop": loop})
        self._log("info", f"开始执行（{'演练模式·只识别不操作' if dry_run else '实战模式'}，"
                          f"{'循环' if loop else '单轮'}）")
        return True

    def stop(self) -> None:
        self._stop.set()
        with self._macro_lock:
            self._macro_busy.clear()
        while True:                     # 清空还没开播的排队动作
            try:
                self._macro_q.get_nowait()
            except queue.Empty:
                break
        self._emit("state", {"running": False})

    def _log(self, level: str, text: str) -> None:
        self._emit("log", {"level": level, "text": text, "time": datetime.now().strftime("%H:%M:%S")})
        if SETTINGS.save_log:
            try:
                today = LOG_DIR / f"{datetime.now():%Y-%m-%d}.log"
                with today.open("a", encoding="utf-8") as f:
                    f.write(f"{datetime.now():%H:%M:%S} [{level}] {text}\n")
            except Exception:
                pass

    # ------------------------- 主循环 -------------------------
    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._lock:
                    rules = [copy.deepcopy(r) for r in self._rules]
                for idx, rule in enumerate(rules):
                    if self._stop.is_set():
                        break
                    if not rule.enabled:
                        continue
                    if rule.max_times and self._total_count(idx) >= rule.max_times:
                        continue
                    # 一条规则 × 多个窗口：每个窗口各跑一遍，互不等
                    for keyword in rule.target_titles():
                        lanes = self._resolve_targets(idx, rule, keyword)
                        for hwnd, title in lanes:
                            if self._stop.is_set():
                                break
                            if rule.max_times and self._total_count(idx) >= rule.max_times:
                                break
                            key = (idx, hwnd)
                            now = time.monotonic()
                            if now < self._next_run.get(key, 0):
                                continue
                            try:
                                executed = self._process_rule(idx, rule, hwnd, title)
                            except Exception as exc:  # noqa: BLE001
                                self._log("error", f"[{rule.name}] 执行异常：{exc}")
                                executed = False
                            if executed:
                                self._counts[key] = self._counts.get(key, 0) + 1
                                gap = rule.cooldown
                                if SETTINGS.humanize:
                                    gap += random.uniform(0, 0.6)
                                self._next_run[key] = time.monotonic() + max(0.05, gap)
                                total = self._total_count(idx)
                                self._emit("rule_item", {
                                    "index": idx,
                                    "times": total,
                                    "disabled": bool(rule.max_times and total >= rule.max_times),
                                    "window": title,
                                })
                            if self._stop.is_set():
                                break
                if SETTINGS.emergency_corner:
                    pos = actions.cursor_pos()
                    if pos[0] <= 2 and pos[1] <= 2 and self._counts:
                        self._log("warn", "检测到鼠标移到左上角，已紧急停止")
                        break
                if not self._loop_mode:
                    self._log("info", "单轮扫描完成，自动停止")
                    break
                wait = SETTINGS.loop_interval_s
                step = 0.05
                elapsed = 0.0
                while elapsed < wait and not self._stop.is_set():
                    time.sleep(step)
                    elapsed += step
        finally:
            self._thread = None
            self._emit("state", {"running": False})

    # ------------------------- 目标窗口 -------------------------
    @staticmethod
    def _allowed_window(title: str) -> bool:
        """「参与窗口」勾选清单：为空表示不限制。"""
        wl = [str(w).strip() for w in (getattr(SETTINGS, "window_whitelist", None) or []) if str(w).strip()]
        if not wl:
            return True
        lt = title.casefold()
        return any(w.casefold() in lt for w in wl)

    def _resolve_targets(self, idx: int, rule: Rule, keyword: str) -> list[tuple[int, str]]:
        """把一个窗口标题关键字解析成当前可用的窗口列表。"""
        from core import windows as win

        if not keyword:
            return [(0, "整个桌面")]
        hwnds = win.find_all_by_title(keyword)
        lanes: list[tuple[int, str]] = []
        blocked = False
        for hwnd in hwnds:
            title = win.title_of(hwnd)
            if not title:
                continue
            if win.is_minimized(hwnd):
                lane = (idx, keyword)
                now = time.monotonic()
                if now - self._win_warn.get(lane, 0.0) > 5.0:
                    self._win_warn[lane] = now
                    self._log("warn", f"[{rule.name}] 窗口「{title}」最小化了，先跳过（恢复后自动继续）")
                continue
            if not self._allowed_window(title):
                blocked = True
                continue
            lanes.append((hwnd, title))
        lane = (idx, keyword)
        now = time.monotonic()
        if not lanes and now - self._win_warn.get(lane, 0.0) > 5.0:
            self._win_warn[lane] = now
            if blocked:
                self._log("warn", f"[{rule.name}] 窗口「{keyword}」不在「参与窗口」勾选清单里，先跳过")
            else:
                self._log("warn", f"[{rule.name}] 没找到窗口「{keyword}」，先跳过")
        return lanes

    def _window_region(self, rule: Rule, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        """目标窗口客户区（物理像素）；窗口不在了返回 None。"""
        from core import windows as win

        if not hwnd or not win.is_alive(hwnd):
            return None
        rect = win.client_rect(hwnd)
        if rect is None:
            return None
        if sum(int(v) for v in rule.region[2:]) > 0:
            if rule.region_relative:
                rx, ry, rw, rh = (int(v) for v in rule.region)
                cut = win.intersect(rect, (rect[0] + rx, rect[1] + ry, rw, rh))
            else:
                cut = win.intersect(rect, tuple(int(v) for v in rule.region))
            if cut:
                rect = cut
        return rect

    def _load_template(self, path: str):
        p = Path(path)
        if not p.is_absolute():
            p = TEMPLATE_DIR / path
        if not p.exists():
            raise FileNotFoundError(f"模板图片不存在：{p}")
        mtime = p.stat().st_mtime
        cached = self._tpl_cache.get(str(p))
        if cached and cached[0] == mtime:
            return cached[1]
        tpl = load_template(p)
        self._tpl_cache[str(p)] = (mtime, tpl)
        return tpl

    # ------------------------- 画面指纹（提速） -------------------------
    @staticmethod
    def _signature(img):
        """32×32 灰度缩略图：判断两块画面是否"几乎一样"。"""
        import cv2

        return cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), (32, 32),
                          interpolation=cv2.INTER_AREA)

    @staticmethod
    def _sig_same(a, b) -> bool:
        import numpy as np

        if a is None or b is None:
            return False
        return float(np.abs(a.astype("int16") - b.astype("int16")).mean()) < 1.2

    def _crop_window_capture(self, rule: Rule, img, origin):
        """窗口后台截图按「搜索区域」裁剪；origin 是截图左上角的屏幕坐标。"""
        if img is None or sum(int(v) for v in rule.region[2:]) <= 0:
            return img, origin
        x0, y0 = int(origin[0]), int(origin[1])
        ih, iw = img.shape[0], img.shape[1]
        rx, ry, rw, rh = (int(v) for v in rule.region)
        if rule.region_relative:
            dx, dy = rx, ry
        else:
            dx, dy = rx - x0, ry - y0
        x1 = max(0, dx)
        y1 = max(0, dy)
        x2 = min(iw, dx + rw)
        y2 = min(ih, dy + rh)
        if x2 - x1 <= 4 or y2 - y1 <= 4:
            return None, origin
        return img[y1:y2, x1:x2].copy(), (x0 + x1, y0 + y1)

    def _locate(self, key, rule: Rule, region, img=None, origin=None):
        """返回 (匹配列表, 是否使用固定坐标)。region = 目标窗口的客户区。

        画面和上一次几乎一样时直接复用上次结果，省掉一次 OCR / 找图（挂机时 CPU 明显更低）。
        """
        if rule.trigger == "fixed":
            from core.vision import Match

            # 固定坐标（始终是屏幕绝对坐标，与窗口无关）
            return [Match(rule.region[0], rule.region[1], 1, 1, 1.0)], True

        if img is None:
            use_region = region if region and region[2] > 0 else None
            img, origin = GRABBER.grab(use_region)
        else:
            use_region = (int(origin[0]), int(origin[1]), img.shape[1], img.shape[0])

        sig = self._signature(img) if SETTINGS.frame_skip else None
        if sig is not None:
            cached = self._frame_cache.get(key)
            if cached and cached[0] == (use_region, tuple(origin)) and self._sig_same(cached[1], sig):
                return list(cached[2]), False

        if rule.trigger == "image":
            tpl, mask = self._load_template(rule.template)
            matches = find_template(
                img, tpl, mask,
                threshold=rule.threshold or SETTINGS.template_threshold,
                multi_scale=SETTINGS.multi_scale,
                max_results=SETTINGS.max_match_results,
                origin=origin,
            )
        else:
            items = OCR.recognize(img, min_score=SETTINGS.ocr_min_score, origin=origin)
            hits = find_text(items, rule.text, rule.text_mode)
            if not hits:
                # v1.2：深色界面上"亮字"有时检测不出来 —— 反色再识别一遍（坐标不变）
                try:
                    inv = _invert_for_ocr(img)
                    if inv is not None:
                        items2 = OCR.recognize(inv, min_score=SETTINGS.ocr_min_score, origin=origin)
                        if items2:
                            hits = find_text(items2, rule.text, rule.text_mode)
                            if hits:
                                self._log("info", f"[{rule.name}] 反色补识成功：找到「{rule.text}」")
                except Exception:  # noqa: BLE001
                    pass
            # 复用 Match 结构
            from core.vision import Match

            matches = [Match(t.rect[0], t.rect[1], t.rect[2], t.rect[3], t.score) for t in hits]

        if sig is not None:
            self._frame_cache[key] = ((use_region, tuple(origin)), sig, list(matches))
        return matches, False

    def _pick(self, matches, order: str):
        if not matches:
            return None
        if order == "last":
            return matches[-1]
        if order == "random":
            return random.choice(matches)
        return matches[0]

    # ------------------------- 单条规则 × 单个窗口 -------------------------
    def _process_rule(self, idx: int, rule: Rule, hwnd: int, window_title: str) -> bool:
        from core import windows as win

        key = (idx, hwnd)
        region = None
        if rule.trigger != "fixed":
            if hwnd:
                region = self._window_region(rule, hwnd)
                if region is None:
                    return False
            else:
                region = tuple(int(v) for v in rule.region) if sum(rule.region[2:]) > 0 else None

        img = origin = None
        if hwnd and rule.capture == "window" and rule.trigger != "fixed":
            cap = win.capture_client(hwnd)
            if cap is not None:
                img, origin = self._crop_window_capture(rule, cap[0], cap[1])
                if img is not None:
                    region = (int(origin[0]), int(origin[1]), img.shape[1], img.shape[0])
                else:
                    img = None
            if img is None:
                now = time.monotonic()
                lane = (idx, hwnd, "cap")
                if now - self._win_warn.get(lane, 0.0) > 10.0:
                    self._win_warn[lane] = now
                    self._log("warn", f"[{rule.name}] 窗口「{window_title}」后台截图失败，"
                                      "这次改用屏幕区域（窗口需可见）")

        if rule.trigger == "timer":
            return self._process_timer(idx, rule, key, region, img, origin)

        matches, is_fixed = self._locate(key, rule, region, img=img, origin=origin)
        if not matches:
            return False
        if rule.action == "click_all" and not is_fixed:
            self._log("info", f"[{rule.name}] 命中 {len(matches)} 处，依次点击")
            gap = float(rule.param) if rule.param.strip().replace('.', '', 1).isdigit() else 0.2
            if not self._dry_run:
                self._activate_for_action(rule, hwnd, key, window_title)
            for m in matches[:SETTINGS.max_match_results]:
                cx, cy = m.center
                if self._dry_run:
                    self._log("dry", f"[{rule.name}] 演练：点击 ({cx + rule.offset_x}, {cy + rule.offset_y})")
                else:
                    actions.click(cx + rule.offset_x, cy + rule.offset_y, button=rule.click_button,
                                  humanize=SETTINGS.humanize, delay_before=SETTINGS.click_delay_ms / 1000)
                time.sleep(gap)
            return True

        target = self._pick(matches, rule.order)
        cx, cy = (target.x, target.y) if is_fixed else target.center
        return self._do_action(rule, region, cx, cy, _desc(rule), hwnd, key, window_title)

    # ------------------------- 秒数不动 -------------------------
    def _process_timer(self, idx: int, rule: Rule, key, region, img=None, origin=None) -> bool:
        """读取一小块区域里的"秒数"，数字静止超过 timer_seconds 就执行动作。"""
        rect = region if (region and sum(region[2:]) > 0) else (
            tuple(int(v) for v in rule.region) if sum(rule.region[2:]) > 0 else None)
        if img is None and rect is None:
            now = time.monotonic()
            if now - self._timer_warn.get(key, 0.0) > 8.0:
                self._timer_warn[key] = now
                self._log("warn", f"[{rule.name}] 还没框选「秒数区域」，这条规则先跳过")
            return False
        if img is None:
            img, origin = GRABBER.grab(rect)

        sig = self._signature(img) if SETTINGS.frame_skip else None
        value = None
        cached = self._timer_sig.get(key)
        if sig is not None and cached and self._sig_same(cached[0], sig):
            value = cached[1]                  # 画面没变 → 数字必然没变，免一次 OCR
        else:
            items = OCR.recognize(img, min_score=SETTINGS.ocr_min_score, origin=origin)
            text = " ".join(it.text for it in items)
            value, why = parse_seconds_strict(text)
            if value is None and why == "no_marker":
                now = time.monotonic()
                if now - self._timer_marker_warn.get(key, 0.0) > 8.0:
                    self._timer_marker_warn[key] = now
                    self._log("warn", f"[{rule.name}] 读到「{text.strip()[:30]}」里有数字，"
                                      "但数字后面没有「秒」字，不按秒数触发")
            if sig is not None:
                self._timer_sig[key] = (sig, value)

        st = self._timer_state.setdefault(key, TimerState())
        now = time.monotonic()
        unchanged = st.update(value, now)
        if value is None or not unchanged:
            return False
        if now - st.since < max(0.2, float(rule.timer_seconds)):
            return False
        st.mark_fired()
        self._log("ok", f"[{rule.name}] 秒数停在 {st.value} 超过 {rule.timer_seconds:g} 秒，执行动作")
        x, y = self._click_at_point(rule, None if key[1] == 0 else key[1])
        window_title = ""
        if key[1]:
            from core import windows as win
            window_title = win.title_of(key[1])
        return self._do_action(rule, region, x, y, f"秒数 {st.value} 不动", key[1], key, window_title)

    # ------------------------- 执行动作 -------------------------
    def _click_at_point(self, rule: Rule, hwnd: Optional[int]) -> tuple[int, int]:
        """点击"另一个位置"：绑了窗口就按该窗口客户区的相对坐标算；没绑窗口用屏幕绝对坐标。"""
        x, y = int(rule.click_at_x), int(rule.click_at_y)
        if rule.click_at_relative and hwnd:
            from core import windows as win

            rect = win.client_rect(hwnd) if hwnd else None
            if rect:
                return rect[0] + x, rect[1] + y
            self._log("warn", f"[{rule.name}] 点别处时窗口不在了，这次先跳过")
        return x, y

    def _activate_for_action(self, rule: Rule, hwnd: int, key, window_title: str) -> None:
        """只在真的要操作前切前台 —— 扫描阶段不抢焦点，多个窗口才能一起识别。"""
        if self._dry_run or not hwnd or not rule.activate_window:
            return
        from core import windows as win

        if win.foreground_is(hwnd) or win.is_desktop_window(hwnd):
            return
        if win.activate(hwnd, wait=0.3):
            self._active_hwnd = hwnd
            self._active_at = time.monotonic()
            now = time.monotonic()
            if now - self._win_log.get(hwnd, 0.0) > 8.0:
                self._win_log[hwnd] = now
                self._log("info", f"[{rule.name}] 操作前切到窗口「{window_title}」")

    def _run_macro(self, rule: Rule, desc: str) -> bool:
        if not rule.macro.strip():
            self._log("warn", f"[{rule.name}] 动作是「播放录制动作」，但还没选动作文件")
            return False
        events = recorder.load_macro(rule.macro)
        if not events:
            self._log("warn", f"[{rule.name}] 动作文件不存在或为空：{rule.macro}")
            return False
        summary = recorder.describe(events)
        name = Path(rule.macro).stem
        if self._dry_run:
            self._log("dry", f"[{rule.name}] 演练：命中「{desc}」→ 播放动作「{name}」（{summary}）")
            return True
        with self._macro_lock:
            if rule.macro in self._macro_busy:
                # 同一套动作还在播：不再叠一份，但也不卡住其他规则
                self._log("info", f"[{rule.name}] 动作「{name}」还在播，这次不重复播放（其他规则照常执行）")
                return True
            self._macro_busy.add(rule.macro)
        self._log(
            "info",
            f"[{rule.name}] 命中「{desc}」→ 播放动作「{name}」（{summary}，{rule.macro_speed:g}×）"
            "· 后台播放，其他规则同时继续",
        )
        self._macro_q.put((rule.name, rule.macro, float(rule.macro_speed or 1.0), len(events)))
        self._ensure_macro_thread()
        return True

    def _ensure_macro_thread(self) -> None:
        t = self._macro_thread
        if t is not None and t.is_alive():
            return
        self._macro_thread = threading.Thread(target=self._macro_worker, name="MacroPlayer", daemon=True)
        self._macro_thread.start()

    def _macro_worker(self) -> None:
        """独立线程按顺序播放动作；扫描线程（自动点击）全程不停。"""
        while not self._stop.is_set():
            try:
                item = self._macro_q.get(timeout=1.0)
            except queue.Empty:
                with self._macro_lock:
                    if self._macro_q.empty() and not self._macro_busy:
                        return          # 没活儿了，线程退出；下次有动作再起
                continue
            rule_name, macro, speed, total = item
            name = Path(macro).stem
            try:
                events = recorder.load_macro(macro)
                if not events:
                    self._log("warn", f"[{rule_name}] 排队的动作文件不见了：{macro}")
                    continue
                done = recorder.RECORDER.play(events, speed=speed, stop_flag=self._stop)
                if self._stop.is_set():
                    self._log("warn", f"[{rule_name}] 动作「{name}」被停止，已播 {done}/{len(events)}")
                else:
                    self._log("ok", f"[{rule_name}] 动作「{name}」播完：{done}/{len(events)} 个事件（期间其他规则未中断）")
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"[{rule_name}] 播放动作失败：{exc}")
            finally:
                with self._macro_lock:
                    self._macro_busy.discard(macro)

    def _do_action(self, rule: Rule, region, bx: int, by: int, desc: str,
                   hwnd: int = 0, key=None, window_title: str = "") -> bool:
        act = rule.action
        if act == "macro":
            self._activate_for_action(rule, hwnd, key, window_title)
            return self._run_macro(rule, desc)
        if act == "click_at":
            x, y = self._click_at_point(rule, hwnd or None)
        else:
            x, y = bx + rule.offset_x, by + rule.offset_y
        if act == "click_all":          # 固定坐标没有"全部"，退化为单击
            act = "click"
        detail = f"({x}, {y})"

        if self._dry_run:
            self._log("dry", f"[{rule.name}] 演练：命中「{desc}」→ {ACTION_LABELS.get(act, act)} {detail}")
            return True

        self._activate_for_action(rule, hwnd, key, window_title)
        delay = SETTINGS.click_delay_ms / 1000
        if act in ("click", "click_at", "double_click", "right_click"):
            clicks = 2 if act == "double_click" else 1
            button = "right" if act == "right_click" else rule.click_button
            actions.click(x, y, button=button, count=clicks,
                          interval=SETTINGS.double_click_interval_ms / 1000,
                          humanize=SETTINGS.humanize, delay_before=delay)
        elif act == "type":
            actions.click(x, y, humanize=SETTINGS.humanize, delay_before=delay)
            actions.type_text(rule.param)
        elif act == "key":
            actions.click(x, y, humanize=SETTINGS.humanize, delay_before=delay)
            actions.hotkey(rule.param)
        elif act == "scroll":
            actions.move_to(x, y)
            try:
                amount = int(float(rule.param or 3))
            except ValueError:
                amount = 3
            actions.scroll(amount)
        elif act == "move":
            actions.move_to(x, y, steps=SETTINGS.humanize and 4 or 1)
        elif act == "wait":
            pass
        self._log("ok", f"[{rule.name}] 命中「{desc}」→ {ACTION_LABELS.get(act, act)} {detail}")
        return True


def _desc(rule: Rule) -> str:
    if rule.trigger == "text":
        return rule.text
    if rule.trigger == "image":
        return Path(rule.template).name if rule.template else "图案"
    if rule.trigger == "timer":
        return "秒数"
    return "固定坐标"


def _default_rules() -> List[Rule]:
    """首次启动给两条禁用状态的示例，方便理解怎么配。"""
    return [
        Rule(
            name="示例：看到「确定」就点它（勾选启用后生效）",
            enabled=False,
            trigger="text",
            text="确定",
            action="click",
            cooldown=2.0,
            max_times=10,
            note="文字识别 + 单击目标。先开演练模式跑一遍看日志。",
        ),
        Rule(
            name="示例：看到「开始游戏」就点击并等待",
            enabled=False,
            trigger="text",
            text="开始游戏",
            action="click",
            cooldown=3.0,
            max_times=0,
            note="把「动作」换成输入文本或按键，就能做登录、填表这类流程。",
        ),
        Rule(
            name="示例：倒计时停住就点「重新连接」（先框秒数区域）",
            enabled=False,
            trigger="timer",
            timer_seconds=3.0,
            action="click_at",
            click_at_relative=False,
            click_at_x=960,
            click_at_y=700,
            cooldown=5.0,
            max_times=0,
            note="识别秒数区域里的数字，连续 3 秒不变 → 点击指定坐标。",
        ),
    ]
