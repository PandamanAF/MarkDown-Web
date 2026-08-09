"""
元数据 —— front matter 解析工具。

MDW 文档可在文件头部使用 ``---`` 包裹的 YAML 风格元数据：:

    ---
    title: 页面标题
    nav: 侧边栏显示名
    parent: /guide
    order: 1
    hidden: false
    ---

解析结果是一个普通字典，供路由系统（导航定制）与扩展（自定义行为）
共同使用。支持的标量类型：字符串（可带引号）、整数、浮点数、布尔值。
"""

from __future__ import annotations
import re
from typing import Tuple

# 匹配文件开头的 front matter 块：---\n 键: 值 ... \n---
_FM_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


def parse_front_matter(text: str) -> Tuple[dict, str]:
    """
    解析文本头部元数据。

    返回 ``(meta, rest)``：
        meta —— 元数据字典（无元数据时返回空字典）
        rest —— 去掉元数据后的正文
    """
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text
    return parse_kv_lines(m.group(1)), text[m.end():]


def parse_kv_lines(block: str) -> dict:
    """解析 ``key: value`` 多行文本为字典。"""
    meta: dict = {}
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = _coerce(value.strip())
    return meta


def _coerce(value: str):
    """将字符串转换为合适的标量类型（引号 / 布尔 / 数字）。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value
