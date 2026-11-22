"""把屏幕上识别到的文字解析成"秒数"，并维护"数字是否不动"的状态。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_FULLWIDTH = {ord(c): ord("0") + (ord(c) - ord("０")) for c in "０１２３４５６７８９"}


def normalize_digits(text: str) -> str:
    """全角数字转半角，去掉逗号/百分号之类干扰字符（保留空格，避免把两段数字粘成一个）。"""
    text = text.translate(_FULLWIDTH)
    text = text.replace("：", ":").replace("。", ".").replace("，", ",")
    return re.sub(r"[，,。%]", "", text)


def parse_seconds(text: str) -> Optional[int]:
    """从一段识别文字里提取"第几秒"（宽松版：纯数字也当成秒）。

    仅用于展示/兼容；真正触发规则请用 parse_seconds_strict（必须带「秒」字）。
    """
    if not text:
        return None
    s = normalize_digits(text)
    # 中文分秒
    m = re.search(r"(\d+)\s*分\s*(\d+)\s*秒", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"(\d+)\s*分", s)
    if m:
        return int(m.group(1)) * 60
    # 冒号式（最后一个冒号后是秒）
    m = re.search(r"(\d+)\s*:\s*(\d{1,2})\s*(?::\s*(\d{1,2}))?\s*(?:\.\d+)?", s)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is not None:
            return int(a) * 3600 + int(b) * 60 + int(c)
        return int(a) * 60 + int(b)
    # 带"秒"的汉字
    m = re.search(r"(\d+(?:\.\d+)?)\s*秒", s)
    if m:
        return int(float(m.group(1)))
    # 纯数字（取最长的连续数字，避免把 "第3关 120秒" 里的 3 当成秒）
    nums = re.findall(r"\d+", s)
    if nums:
        return int(max(nums, key=len))
    return None


def parse_seconds_strict(text: str) -> tuple[Optional[int], str]:
    """严格版：数字后面必须紧跟「秒」字，才算识别到秒数。

    返回 (秒数, 状态)：
    - ("ok")     读到了带「秒」的数字
    - ("no_marker") 有数字，但数字后面没有「秒」（这种情况不触发，避免把普通数字当倒计时）
    - ("no_number") 连数字都没读到

    支持：30秒 / 30 秒钟 / 剩余 30 秒 / 1分30秒 / 1:30 秒（秒字可以和冒号式分开被识别到）。
    """
    if not text:
        return None, "no_number"
    s = normalize_digits(text)
    # 必须存在"数字 + 秒"的相邻结构，才承认这是秒数
    if not re.search(r"\d+\s*秒", s):
        return None, ("no_marker" if re.search(r"\d", s) else "no_number")
    # 分 + 秒
    m = re.search(r"(\d+)\s*分\s*(\d+)\s*秒", s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2)), "ok"
    # 冒号式 + 秒（秒字可能在 OCR 里被单独切开，join 后中间有空格）
    m = re.search(r"(\d+)\s*:\s*(\d{1,2})\s*(?::\s*(\d{1,2}))?\s*秒", s)
    if m:
        a, b, c = m.group(1), m.group(2), m.group(3)
        if c is not None:
            return int(a) * 3600 + int(b) * 60 + int(c), "ok"
        return int(a) * 60 + int(b), "ok"
    # 纯"数字 + 秒"
    m = re.search(r"(\d+(?:\.\d+)?)\s*秒", s)
    if m:
        return int(float(m.group(1))), "ok"
    return None, "no_marker"


@dataclass
class TimerState:
    """一条"秒数不动"规则的状态。"""

    value: Optional[int] = None
    since: float = 0.0          # 这个值第一次出现的时间（monotonic）
    fired: bool = False         # 本次静止是否已经触发过动作
    reads: int = 0

    def update(self, value: Optional[int], now: float) -> bool:
        """喂入最新读数，返回"是否应当在这一次触发"。"""
        self.reads += 1
        if value is None:
            # 读不到数字（窗口被遮挡 / 页面切换）→ 重置，不触发
            self.value = None
            self.since = now
            self.fired = False
            return False
        if value != self.value:
            self.value = value
            self.since = now
            self.fired = False
            return False
        return not self.fired

    def mark_fired(self) -> None:
        self.fired = True

    def idle_seconds(self, now: float) -> float:
        return max(0.0, now - self.since) if self.value is not None else 0.0
