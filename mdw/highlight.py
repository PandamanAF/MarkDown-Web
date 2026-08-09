"""
代码高亮引擎 —— 基于 Pygments（500+ 语言支持）。

轻量封装，外部 API 不变：``HighlightRegistry.highlight(code, lang)``。
Pygments 不可用时降级为纯文本，不会导致启动失败。
"""

from __future__ import annotations
import html as _html
import logging

logger = logging.getLogger("mdw.highlight")

_HAS_PYGMENTS = False
_pg_highlight = None
_get_lexer_by_name = None
_HtmlFormatter = None
_ClassNotFound = None

try:
    from pygments import highlight as _pg_highlight
    from pygments.lexers import get_lexer_by_name as _get_lexer_by_name
    from pygments.formatters import HtmlFormatter as _HtmlFormatter
    from pygments.util import ClassNotFound as _ClassNotFound
    _HAS_PYGMENTS = True
except ImportError:
    logger.warning("Pygments 未安装，代码块将不显示语法高亮")


class HighlightRegistry:
    """全局高亮注册表。底层使用 Pygments，支持别名与自定义映射。"""

    _aliases: dict[str, str] = {}
    _formatter = None

    @classmethod
    def _ensure_formatter(cls):
        if cls._formatter is None and _HAS_PYGMENTS:
            cls._formatter = _HtmlFormatter(nowrap=True, style='material')

    @classmethod
    def register_alias(cls, alias: str, pygments_lexer_name: str) -> None:
        cls._aliases[alias.lower().strip()] = pygments_lexer_name

    @classmethod
    def registered(cls) -> list[str]:
        return sorted(cls._aliases.keys())

    @classmethod
    def highlight(cls, code: str, language: str) -> str:
        lang = language.strip().lower()
        pyg_name = cls._aliases.get(lang, lang)

        if not _HAS_PYGMENTS:
            return _html.escape(code)

        cls._ensure_formatter()
        try:
            lexer = _get_lexer_by_name(pyg_name, stripall=True)
        except (_ClassNotFound, Exception):
            try:
                lexer = _get_lexer_by_name(pyg_name, stripnl=False)
            except (_ClassNotFound, Exception):
                return _html.escape(code)
        return _pg_highlight(code, lexer, cls._formatter)
