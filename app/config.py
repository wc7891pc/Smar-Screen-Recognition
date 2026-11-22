"""全局配置与数据目录管理。"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_NAME = "慧眼屏幕助手"
APP_TITLE = "慧眼屏幕助手 · 屏幕识别与自动化"
APP_ID = "ScreenEye"


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


BASE_DIR = _base_dir()
DATA_DIR = BASE_DIR / "data"
ASSET_DIR = BASE_DIR / "assets"
TEMPLATE_DIR = DATA_DIR / "templates"
MACRO_DIR = DATA_DIR / "macros"
LOG_DIR = DATA_DIR / "logs"
SETTINGS_FILE = DATA_DIR / "settings.json"
RULES_FILE = DATA_DIR / "rules.json"

for _d in (DATA_DIR, TEMPLATE_DIR, MACRO_DIR, LOG_DIR, ASSET_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def asset_path(name: str) -> Path:
    """打包成 EXE 后资源在 _internal 下，这里做一次兜底查找。"""
    candidates = [ASSET_DIR / name]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "assets" / name)
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


@dataclass
class Settings:
    """可持久化的用户设置。"""

    ocr_min_score: float = 0.60          # OCR 置信度下限
    template_threshold: float = 0.85     # 找图相似度下限
    multi_scale: bool = True             # 找图多尺度搜索（应对窗口缩放）
    click_delay_ms: int = 120            # 点击前后的等待时间
    double_click_interval_ms: int = 140  # 双击间隔
    humanize: bool = True                # 拟人化：轻微抖动 + 随机延时
    loop_interval_s: float = 1.0         # 规则扫描间隔
    emergency_corner: bool = True        # 鼠标甩到左上角紧急停止
    save_log: bool = True                # 是否落盘日志
    preview_interval_ms: int = 800       # 预览刷新间隔
    max_match_results: int = 20          # 单次最多返回的匹配数
    minimize_before_capture: bool = True # 识别前自动最小化本窗口，避免把自己的界面拍进去
    live_preview: bool = True            # 是否默认开启实时预览
    auto_start: bool = False             # 启动程序后自动开始执行规则（监听文字/图案）
    # ---- v1.1：速度 / 长期运行 ----
    frame_skip: bool = True              # 画面没变化就复用上次识别结果（省 CPU、提速）
    ocr_upscale: float = 2.0             # 小字放大倍数（秒数、小按钮文字更准）
    auto_recover: bool = True            # 引擎意外停止时自动恢复（长期挂机用）
    close_to_tray: bool = True           # 点关闭按钮时最小化到托盘，规则继续跑
    toast_on_hit: bool = False           # 命中时托盘弹提示
    record_skip_injected: bool = False    # 录制时忽略本程序注入的点击/按键（规则边跑边录）
    # ---- v1.6：多窗口 ----
    window_whitelist: list[str] = field(default_factory=list)
    # 只对这些窗口生效（为空 = 不限制）。条目是窗口标题关键字，勾选时按当时标题写入。

    @classmethod
    def load(cls) -> "Settings":
        if SETTINGS_FILE.exists():
            try:
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
                return cls(**known)
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        SETTINGS_FILE.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )


SETTINGS = Settings.load()
