"""
解析器 —— 通过可扩展管线将 MDW / Markdown 源码转换为 HTML。

管线流程：
    原始源码
        → 解析前钩子（扩展）
        → Markdown 核心（Python-Markdown 或内置）
        → 渲染后钩子（扩展）
        → 最终 HTML

内置渲染器特性：
    - 标题自动生成 id 锚点（用于目录跳转）
    - 表格、有序/无序列表、引用块、代码块
    - 图片/视频/音频自动识别
    - 并排布局（``::: row`` / ``::: col``）
    - 结构化 CSS 类映射（标题层级 → 语义化类名）
"""

from __future__ import annotations
import html
import re
from pathlib import Path
from typing import Optional
from .extensions import Extension
from .highlight import HighlightRegistry


# ---------------------------------------------------------------------------
# 内置最小化 Markdown 渲染器（无需外部依赖）
# ---------------------------------------------------------------------------

# 媒体扩展名 → MIME 类型（用于 <video>/<audio> 的 <source type>）
_MEDIA_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".ogv": "video/ogg", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".oga": "audio/ogg", ".ogg": "audio/ogg",
    ".flac": "audio/flac",
}

# HTML 块级元素标签集合 —— 用于段落包装检测
_HTML_BLOCK_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "p", "div", "blockquote", "pre",
    "table", "thead", "tbody", "tr", "th", "td",
    "video", "audio", "img", "figure", "figcaption",
    "section", "article", "header", "footer", "nav",
    "hr", "br", "dl", "dt", "dd", "form", "fieldset",
}


def _is_html_block(line: str) -> bool:
    """判断一行是否以 HTML 块级元素标签或注释开头。"""
    stripped = line.lstrip()
    if not stripped.startswith("<"):
        return False
    # HTML 注释
    if stripped.startswith("<!--"):
        return True
    # 提取标签名（< 已通过 startswith 检查，此处匹配 /tag 或 tag）
    m = re.match(r"/?([a-zA-Z][a-zA-Z0-9]*)", stripped[1:])
    if not m:
        return False
    return m.group(1).lower() in _HTML_BLOCK_TAGS


def _slugify(text: str) -> str:
    """将标题文本转换为 URL 友好的锚点 ID。"""
    # 移除 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 保留中英文、数字，其余转为连字符
    slug = re.sub(r"[^\w\u4e00-\u9fff-]", "-", text.strip().lower())
    # 合并连续连字符
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _render_media(m: re.Match) -> str:
    """把 ``![标题](资源)`` 按资源扩展名渲染为图片 / 视频 / 音频。

    语法：``![alt](path)`` 或 ``![alt](path "title")``
      - 视频扩展名（.mp4/.webm/...）→ ``<video controls>``
      - 音频扩展名（.mp3/.wav/...）→ ``<audio controls>``
      - 其余 → ``<img loading="lazy">``
    """
    alt = html.escape(m.group(1), quote=True)
    url = html.escape(m.group(2), quote=True)
    title = html.escape(m.group(3) or "", quote=True)
    ext = Path(m.group(2)).suffix.lower()
    mime = _MEDIA_MIME.get(ext, "")
    type_attr = f' type="{mime}"' if mime else ""

    if ext in {".mp4", ".m4v", ".webm", ".ogv", ".mov", ".mkv", ".avi"}:
        return (
            f'<video controls preload="metadata" playsinline>'
            f'<source src="{url}"{type_attr}>{alt}</video>'
        )
    if ext in {".mp3", ".m4a", ".aac", ".wav", ".oga", ".ogg", ".flac"}:
        return (
            f'<audio controls preload="metadata">'
            f'<source src="{url}"{type_attr}>{alt}</audio>'
        )

    attrs = f' alt="{alt}"' if alt else ""
    if title:
        attrs += f' title="{title}"'
    return f'<img src="{url}"{attrs} loading="lazy">'


# GFM 表格：表头行 + 分隔行（含对齐标记）+ 数据行（可省略）
_TABLE_RE = re.compile(
    r"^(\|?[^\n|]*\|[^\n|]*(?:\|[^\n|]*)*\|?)\n"
    r"^(\|?[ \t:|-]+\|(?:[ \t:|-]+\|)*[ \t:|-]*)\n?"
    r"((?:^(\|?[^\n|]*\|[^\n|]*(?:\|[^\n|]*)*\|?)$\n?)*)",
    re.M,
)

# 行内代码：`...`（在应用内联规则前提取保护）
_CODE_INLINE_RE = re.compile(r"`([^`]+)`")

# 有序列表：1. item（至少一个数字+点号开头）
_ORDERED_LIST_RE = re.compile(
    r"(?:^[ \t]*\d+\.[ \t]+.+$(?:\n?|$))+",
    re.MULTILINE,
)

# 无序列表：- * + item
_UNORDERED_LIST_RE = re.compile(
    r"(?:^[ \t]*[-*+][ \t]+.+$(?:\n?|$))+",
    re.MULTILINE,
)

# 引用块：> text
_BLOCKQUOTE_RE = re.compile(
    r"(?:^>[ \t]?.+$(?:\n?|$))+",
    re.MULTILINE,
)

# 代码围栏：```lang\n...\n```
_FENCE_RE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)

# 注入变量：{{ 名称 }}（名称允许字母/数字/下划线/连字符）
_VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")

# 变量替换时受保护的代码元素（<pre> 代码块与 <code> 行内代码）
_CODE_BLOCK_RE = re.compile(r"(<pre[^>]*>.*?</pre>|<code[^>]*>.*?</code>)", re.DOTALL)


def _split_row(row: str) -> list[str]:
    """按管道符拆分表格行（支持 ``\\|`` 转义），去掉首尾空单元格。"""
    cells = re.split(r"(?<!\\)\|", row.strip().strip("|"))
    return [c.strip().replace(r"\|", "|") for c in cells]


def _table_align(cell: str) -> str:
    """解析 GFM 对齐标记：``:---`` 左对齐、``:---:`` 居中、``---:`` 右对齐。"""
    left = cell.startswith(":")
    right = cell.endswith(":")
    if left and right:
        return "center"
    if right:
        return "right"
    return "left"


def _render_table(m: re.Match) -> str:
    """把 GFM 表格块渲染为 ``<table>``（表头 + 数据行，含对齐）。"""
    head_cells = _split_row(m.group(1))
    aligns = [_table_align(c) for c in _split_row(m.group(2))]
    ncols = len(head_cells)

    def cell_html(content: str, idx: int, tag: str) -> str:
        align = aligns[idx] if idx < len(aligns) else "left"
        style = f' style="text-align:{align}"' if align != "left" else ""
        return f"<{tag}{style}>{content}</{tag}>"

    thead = (
        "<thead>\n<tr>"
        + "".join(cell_html(c, i, "th") for i, c in enumerate(head_cells))
        + "</tr>\n</thead>"
    )

    rows = []
    for line in (m.group(3) or "").strip().splitlines():
        if not line.strip():
            continue
        cells = _split_row(line)
        if len(cells) < ncols:
            cells += [""] * (ncols - len(cells))
        else:
            cells = cells[:ncols]
        rows.append("<tr>" + "".join(cell_html(c, i, "td") for i, c in enumerate(cells)) + "</tr>")
    tbody = ("<tbody>\n" + "\n".join(rows) + "\n</tbody>") if rows else ""

    return f'<table class="mdw-table">\n{thead}\n{tbody}\n</table>'


def _heading_render(level: int) -> callable:
    """生成标题渲染函数，支持 GFM 风格 ``{#custom-id}`` 锚点定义。"""
    def _render(m: re.Match) -> str:
        text = m.group(1).strip()
        custom_id = (m.group(2) or "").strip()
        # 使用自定义 id，否则自动生成
        if custom_id:
            slug = custom_id
        else:
            slug = _slugify(text)
        id_attr = f' id="{slug}"' if slug else ""
        # 标题本身不显示 {#id}（已在正则中分离）
        return f'<h{level} class="mdw-h{level}"{id_attr}>{text}</h{level}>'
    return _render


# ---------------------------------------------------------------------------
# Emoji 短码映射与渲染
# ---------------------------------------------------------------------------

_EMOJI_MAP = {
    "smile": "😄", "laughing": "😆", "joy": "😂", "rofl": "🤣",
    "wink": "😉", "blush": "😊", "heart_eyes": "😍", "kissing_heart": "😘",
    "thinking": "🤔", "neutral_face": "😐", "expressionless": "😑",
    "unamused": "😒", "roll_eyes": "🙄", "sweat_smile": "😅",
    "sweat": "😓", "disappointed": "😞", "worried": "😟", "confused": "😕",
    "cry": "😢", "sob": "😭", "angry": "😠", "rage": "😡", "triumph": "😤",
    "sunglasses": "😎", "heart": "❤️", "broken_heart": "💔",
    "star": "⭐", "sparkles": "✨", "fire": "🔥", "zap": "⚡",
    "boom": "💥", "rocket": "🚀", "bulb": "💡", "book": "📖",
    "memo": "📝", "wrench": "🔧", "hammer": "🔨", "gear": "⚙️",
    "check": "✅", "x": "❌", "warning": "⚠️", "info": "ℹ️",
    "question": "❓", "exclamation": "❗", "plus": "➕", "minus": "➖",
    "arrow_right": "➡️", "arrow_left": "⬅️", "arrow_up": "⬆️", "arrow_down": "⬇️",
    "link": "🔗", "lock": "🔒", "unlock": "🔓", "key": "🔑",
    "mag": "🔍", "package": "📦", "tada": "🎉", "gift": "🎁",
    "email": "📧", "phone": "📞", "calendar": "📅", "clock": "🕐",
    "pin": "📌", "clip": "📎", "scissors": "✂️", "pencil": "✏️",
    "bug": "🐛", "beetle": "🪲", "construction": "🚧",
    "one": "1️⃣", "two": "2️⃣", "three": "3️⃣",
    "+1": "👍", "-1": "👎", "clap": "👏", "pray": "🙏",
    "100": "💯", "ok": "🆗", "new": "🆕", "cool": "🆒", "free": "🆓",
    "up": "🆙", "top": "🔝", "soon": "🔜", "on": "🔛", "end": "🔚",
    "back": "🔙", "copyright": "©️", "registered": "®️", "tm": "™️",
}

def _render_emoji(m: re.Match) -> str:
    """将 :shortcode: 转换为 emoji 字符。"""
    code = m.group(1).lower()
    return _EMOJI_MAP.get(code, m.group(0))


# ---------------------------------------------------------------------------
# Markdown 规则集
# ---------------------------------------------------------------------------

_MD_RULES: list[tuple[str | re.Pattern, int, object]] = [
    # (正则表达式, 标志位, 替换文本或回调)
    (_TABLE_RE, re.M, _render_table),
    (r"^###### (.+?)(?:\s*\{#([\w-]+)\})?\s*$", re.M, _heading_render(6)),
    (r"^##### (.+?)(?:\s*\{#([\w-]+)\})?\s*$",  re.M, _heading_render(5)),
    (r"^#### (.+?)(?:\s*\{#([\w-]+)\})?\s*$",   re.M, _heading_render(4)),
    (r"^### (.+?)(?:\s*\{#([\w-]+)\})?\s*$",    re.M, _heading_render(3)),
    (r"^## (.+?)(?:\s*\{#([\w-]+)\})?\s*$",     re.M, _heading_render(2)),
    (r"^# (.+?)(?:\s*\{#([\w-]+)\})?\s*$",      re.M, _heading_render(1)),
    (r"^---+\s*$",     re.M, '<hr class="mdw-hr">'),
    (r"\*\*\*(.+?)\*\*\*", 0, "<strong><em>\\1</em></strong>"),
    (r"\*\*(.+?)\*\*",      0, "<strong>\\1</strong>"),
    (r"\*(.+?)\*",          0, "<em>\\1</em>"),
    # 删除线（优先匹配 ~~ 再匹配 ~，避免冲突）
    (r"~~(.+?)~~", 0, "<del>\\1</del>"),
    # 下标 ~text~（注意：必须在 ~~ 之后匹配，[^~\n] 阻止跨行）
    (r"~([^~\s][^~\n]*[^~\s]|[^~\s])~", 0, "<sub>\\1</sub>"),
    # 上标 ^text^（[^^\n] 阻止跨行匹配）
    (r"\^([^\s^][^^\n]*[^\s^]|[^\s^])\^", 0, "<sup>\\1</sup>"),
    # 标记高亮 ==text==
    (r"==(.+?)==", 0, "<mark>\\1</mark>"),
    # 媒体（图片/视频/音频）
    (r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"([^\"]*)\")?\)", 0, _render_media),
    (r"\[([^\]]+)\]\(([^)\s]+)\)", 0, '<a href="\\2">\\1</a>'),
    # 自动链接 <url> <email>
    (r"<(https?://[^\s>]+)>", 0, '<a href="\\1">\\1</a>'),
    (r"<([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})>", 0, '<a href="mailto:\\1">\\1</a>'),
    # Emoji 短码 :smile:
    (r":([a-z0-9_+-]+):", 0, _render_emoji),
    # 行内数学公式 $...$
    (r"(?<!\$)\$(?!\$)([^$\n]+?)(?<!\$)\$(?!\$)", 0, '<span class="mdw-math-inline">\\1</span>'),
]


def apply_vars(text: str, variables: dict) -> str:
    """把 ``{{ 变量名 }}`` 替换为 ``variables`` 中的值。

    - 只替换已注册的变量，未知占位符原样保留
    - 代码块（``<pre>`` / ``<code>``）内容不会被替换
    - 值中的 HTML 原样插入（可用于注入样式、链接等）
    """
    if not variables:
        return text

    protected: list[str] = []

    def _hide(m: re.Match) -> str:
        protected.append(m.group(0))
        return f"\x00MDW_VAR_HIDE_{len(protected) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(_hide, text)
    text = _VAR_PATTERN.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)
    for i, original in enumerate(protected):
        text = text.replace(f"\x00MDW_VAR_HIDE_{i}\x00", original)
    return text


# ---------------------------------------------------------------------------
# 解析器类
# ---------------------------------------------------------------------------

class Parser:
    """Markdown → HTML 转换器，带扩展管线。"""

    def __init__(self, use_builtin: bool = True, variables: dict | None = None):
        """
        参数：
            use_builtin: 为 True 使用内置渲染器，为 False 尝试导入 python-markdown
            variables: 注入变量字典（渲染时替换 ``{{ 名称 }}`` 占位符）
        """
        self._extensions: list[Extension] = []
        self._use_builtin = use_builtin
        self.variables: dict = variables if variables is not None else {}
        # 可选：导入 python-markdown
        self._md_lib = None
        if not use_builtin:
            try:
                import markdown
                self._md_lib = markdown
            except ImportError:
                self._md_lib = None

    def register(self, ext: Extension) -> None:
        """注册一个扩展到解析管线。"""
        self._extensions.append(ext)

    def render(self, raw_source: str) -> str:
        """
        完整管线：解析前 → Markdown 转换 → 渲染后 → 注入变量。

        扩展的 pre_parse 和 post_render 方法按注册顺序依次调用。
        """
        text = raw_source

        # 0. 将 FrontMatterExtension 实例传递给需要它的扩展（如 TemplateExtension）
        fm_ext = None
        for ext in self._extensions:
            if ext.name == "front_matter":
                fm_ext = ext
                break
        if fm_ext:
            for ext in self._extensions:
                if hasattr(ext, "_fm_ext"):
                    ext._fm_ext = fm_ext

        # 1. 解析前：所有扩展依次处理原始源码
        for ext in self._extensions:
            text = ext.pre_parse(text)

        # 2. 核心 Markdown 转换
        text = self._to_html(text)

        # 3. 渲染后：所有扩展依次处理生成的 HTML
        for ext in self._extensions:
            text = ext.post_render(text)

        # 4. 注入变量：{{ 名称 }} → 值（代码块内容除外）
        text = apply_vars(text, self.variables)

        return text

    # ------------------------------------------------------------------
    # 核心 Markdown → HTML 转换
    # ------------------------------------------------------------------

    def _to_html(self, source: str) -> str:
        """
        如果可用且 use_builtin=False，使用 python-markdown；
        否则使用内置渲染器。
        """
        if self._md_lib and not self._use_builtin:
            return self._md_lib.markdown(
                source, extensions=["fenced_code", "codehilite", "tables"]
            )

        return _builtin_render(source)


# ---------------------------------------------------------------------------
# 任务列表渲染
# ---------------------------------------------------------------------------

def _render_task_list_item(line: str) -> str:
    """检测 - [ ] / - [x] 并渲染为 checkbox。"""
    m = re.match(r"^([ \t]*)[-*+][ \t]+\[([ xX])\][ \t]+(.*)", line)
    if not m:
        return f"<li>{line}</li>"
    indent, checked, content = m.groups()
    chk = " checked" if checked.strip().lower() == "x" else ""
    return f'<li class="mdw-task-item"><input type="checkbox" disabled{chk}>{content}</li>'


def _render_unordered_list(match: re.Match) -> str:
    """将匹配到的无序列表片段渲染为 HTML <ul>，自动识别任务列表。"""
    lines = match.group(0).strip().splitlines()
    items = []
    is_task = False
    for line in lines:
        content_stripped = re.sub(r"^[ \t]*[-*+][ \t]+", "", line)
        if re.match(r"^\[[ xX]\]", content_stripped):
            is_task = True
            items.append(_render_task_list_item(line))
        else:
            items.append(f"  <li>{content_stripped}</li>")
    cls = ' class="mdw-task-list"' if is_task else ' class="mdw-list"'
    return f"<ul{cls}>\n" + "\n".join(items) + "\n</ul>"


# ---------------------------------------------------------------------------
# 数学公式块 $$...$$
# ---------------------------------------------------------------------------

_MATH_BLOCK_RE = re.compile(r"\$\$\n?(.*?)\$\$", re.DOTALL)


def _render_math_block(m: re.Match) -> str:
    """将 $$...$$ 渲染为 KaTeX 兼容的数学公式块。"""
    formula = m.group(1).strip()
    return f'<div class="mdw-math-block">{html.escape(formula)}</div>'


# ---------------------------------------------------------------------------
# TOC 目录生成
# ---------------------------------------------------------------------------

_TOC_PLACEHOLDER_RE = re.compile(r"^\[\[TOC\]\]|<!--\s*toc\s*-->", re.MULTILINE | re.IGNORECASE)


def _generate_toc(text: str) -> str:
    """从已渲染的 HTML 中提取标题生成目录。"""
    headings = re.findall(r'<h([1-3])\s+class="mdw-h\1"\s+id="([^"]+)"[^>]*>(.+?)</h\1>', text)
    if not headings:
        return text
    items = []
    for level, sid, content in headings:
        indent = "  " * (int(level) - 1)
        items.append(f'{indent}<li class="mdw-toc-h{level}"><a href="#{sid}">{content}</a></li>')
    # 清理标题中的 HTML 标签用于显示
    toc = '<nav class="mdw-toc"><ul>\n' + "\n".join(items) + "\n</ul></nav>"
    return _TOC_PLACEHOLDER_RE.sub(toc, text)


# ---------------------------------------------------------------------------
# 脚注 [^label]
# ---------------------------------------------------------------------------

_FOOTNOTE_DEF_RE = re.compile(r"^\[\^(\w+)\]:\s+(.+)$", re.MULTILINE)
_FOOTNOTE_REF_RE = re.compile(r"\[\^(\w+)\](?!\s*:)")


def _process_footnotes(text: str) -> str:
    """收集 [^label]: definition 并替换 [^label] 引用。"""
    defs: dict[str, str] = {}
    # 收集定义
    for m in _FOOTNOTE_DEF_RE.finditer(text):
        defs[m.group(1)] = m.group(2).strip()
    if not defs:
        return text
    # 移除定义行
    text = _FOOTNOTE_DEF_RE.sub("", text)
    # 替换引用
    counter: dict[str, int] = {}

    def _ref_repl(m: re.Match) -> str:
        label = m.group(1)
        if label not in defs:
            return m.group(0)
        idx = counter.get(label, 0) + 1
        counter[label] = idx
        id_suffix = f"-{idx}" if idx > 1 else ""
        return (
            f'<sup class="mdw-footnote-ref">'
            f'<a href="#mdw-fn-{label}{id_suffix}" id="mdw-fnref-{label}{id_suffix}">[{idx}]</a>'
            f'</sup>'
        )

    text = _FOOTNOTE_REF_RE.sub(_ref_repl, text)
    # 追加脚注区域
    fn_html = ['<hr class="mdw-footnote-sep"><section class="mdw-footnotes"><ol>']
    for label, content in defs.items():
        fn_html.append(
            f'<li id="mdw-fn-{label}"><p>{content} '
            f'<a href="#mdw-fnref-{label}" class="mdw-footnote-backref">↩</a></p></li>'
        )
    fn_html.append("</ol></section>")
    return text + "\n" + "\n".join(fn_html)


# ---------------------------------------------------------------------------
# 警告/提示块 > [!NOTE] / > [!WARNING] 等
# ---------------------------------------------------------------------------

_ADMONITION_RE = re.compile(
    r"^>\s*\[!(NOTE|TIP|INFO|WARNING|DANGER|IMPORTANT|CAUTION)\]\s*\n"
    r"((?:^>[ \t]?.+$\n?)+)",
    re.MULTILINE,
)


def _render_admonition(m: re.Match) -> str:
    """将 > [!TYPE] 块渲染为带图标的提示框。"""
    atype = m.group(1).lower()
    body = m.group(2)
    # 去掉每行的 > 前缀
    lines = [re.sub(r"^>[ \t]?", "", ln) for ln in body.splitlines()]
    inner = "<br>".join(ln.strip() for ln in lines if ln.strip())

    icons = {
        "note": "ℹ️", "tip": "💡", "info": "ℹ️",
        "warning": "⚠️", "danger": "🚫", "important": "❗", "caution": "⚠️",
    }
    icon = icons.get(atype, "ℹ️")
    title = atype.replace("_", " ").title()

    return (
        f'<div class="mdw-admonition mdw-admonition-{atype}">'
        f'<p class="mdw-admonition-title">{icon} {title}</p>'
        f'<div class="mdw-admonition-body">{inner}</div>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# 代码块渲染
# ---------------------------------------------------------------------------

def _render_code_block(lang: str, code: str) -> str:
    """生成增强代码块 HTML：语言栏 + 复制按钮 + Pygments 语法高亮。"""
    safe_lang = lang.strip() or "text"
    display_lang = safe_lang.upper() if len(safe_lang) <= 6 else safe_lang.title()
    highlighted = HighlightRegistry.highlight(code, safe_lang)

    return (
        f'<div class="mdw-code-block">'
        f'<div class="mdw-code-header">'
        f'<span class="mdw-code-lang">{html.escape(display_lang)}</span>'
        f'<button class="mdw-code-copy" title="复制代码" '
        f'onclick="MDW.copyCode(this)" aria-label="复制代码">'
        f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
        f'<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>'
        f'</svg></button>'
        f'</div>'
        f'<pre class="mdw-code"><code class="language-{html.escape(safe_lang)}">'
        f'<div class="highlight">{highlighted}</div></code></pre>'
        f'</div>'
    )


# ---------------------------------------------------------------------------
# 内置渲染器（独立函数，无状态）
# ---------------------------------------------------------------------------

def _builtin_render(source: str) -> str:
    """内置 Markdown → HTML 渲染器，完整 GFM 扩展支持。

    处理顺序：
        1. 保护代码围栏和行内代码
        2. 应用基础规则（标题、表格、内联格式、媒体、链接、emoji、数学内联）
        3. 渲染数学公式块 $$...$$
        4. 渲染警告/提示块 > [!TYPE]
        5. 渲染引用块
        6. 渲染有序列表
        7. 渲染无序列表（含任务列表）
        8. 处理脚注
        9. 生成 TOC
       10. 还原代码
       11. 包裹剩余行为段落
    """
    text = source

    # 0. 反斜杠转义保护（在任何规则之前）
    _ESCAPABLE_SET = set("\\`*_{}[]()#+-.!|~^<>:")
    _ESCAPE_RE = re.compile(r"\\(.)")
    escapes: list[str] = []

    def _hide_escape(m: re.Match) -> str:
        ch = m.group(1)
        if ch not in _ESCAPABLE_SET:
            return m.group(0)         # 不可转义的字符原样保留
        escapes.append(ch)
        return f"\x00MDW_ESC_{len(escapes) - 1}\x00"
    text = _ESCAPE_RE.sub(_hide_escape, text)

    # 1. 保护代码围栏与行内代码
    fences: list[str] = []
    inlines: list[str] = []

    def _hide_fence(m: re.Match) -> str:
        token = f"\x00MDW_CODE_{len(fences)}\x00"
        lang = m.group(1).strip() or "text"
        code = m.group(2)
        fences.append(_render_code_block(lang, code))
        return token

    text = _FENCE_RE.sub(_hide_fence, text)

    def _hide_inline(m: re.Match) -> str:
        token = f"\x00MDW_INL_{len(inlines)}\x00"
        inlines.append(f"<code>{html.escape(m.group(1))}</code>")
        return token

    text = _CODE_INLINE_RE.sub(_hide_inline, text)

    # 2. 依次应用基础 Markdown 规则（含 emoji/数学内联/自动链接等）
    for pattern, flags, replacement in _MD_RULES:
        text = re.sub(
            pattern, replacement, text,
            flags=flags if isinstance(pattern, str) else 0,
        )

    # 3. 数学公式块 $$...$$（在代码保护之后、其他块之前）
    text = _MATH_BLOCK_RE.sub(_render_math_block, text)

    # 4. 警告/提示块 > [!TYPE]（必须在引用块之前，避免被误匹配）
    text = _ADMONITION_RE.sub(_render_admonition, text)

    # 5. 引用块（> text，排除已被警告块处理的内容）
    text = _BLOCKQUOTE_RE.sub(_render_blockquote, text)

    # 6. 有序列表
    text = _ORDERED_LIST_RE.sub(_render_ordered_list, text)

    # 7. 无序列表（含 GFM 任务列表 - [ ] / - [x]）
    text = _UNORDERED_LIST_RE.sub(_render_unordered_list, text)

    # 8. 脚注处理（收集定义 + 替换引用 + 追加脚注区）
    text = _process_footnotes(text)

    # 9. TOC 目录自动生成
    text = _generate_toc(text)

    # 10. 还原行内代码与代码围栏
    for i, original in enumerate(inlines):
        text = text.replace(f"\x00MDW_INL_{i}\x00", original)
    for i, original in enumerate(fences):
        text = text.replace(f"\x00MDW_CODE_{i}\x00", original)

    # 11. 将剩余行包裹在段落标签中（智能跳过已生成的 HTML 块）
    lines = text.split("\n")
    result: list[str] = []
    in_block = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            in_block = False
            result.append("")
            continue

        # 检查是否为 HTML 块级元素或自定义块
        if _is_html_block(stripped):
            in_block = True
            result.append(line)
            continue
        if in_block:
            # 继续在块内追加
            result.append(line)
            continue

        # 普通文本 → 段落
        result.append(f"<p>{stripped}</p>")

    text = "\n".join(result)

    # 12. 还原转义字符
    for i, ch in enumerate(escapes):
        text = text.replace(f"\x00MDW_ESC_{i}\x00", ch)

    return text


# ---------------------------------------------------------------------------
# 结构化渲染函数（有序列表）
# ---------------------------------------------------------------------------

def _render_ordered_list(match: re.Match) -> str:
    """将匹配到的有序列表片段渲染为 HTML <ol>。"""
    lines = match.group(0).strip().splitlines()
    items = []
    # 检测起始编号
    first = lines[0].strip()
    start_match = re.match(r"^[ \t]*(\d+)\.", first)
    start = int(start_match.group(1)) if start_match else 1
    start_attr = f' start="{start}"' if start != 1 else ""

    for line in lines:
        content = re.sub(r"^[ \t]*\d+\.[ \t]+", "", line)
        items.append(f"  <li>{content}</li>")
    return f'<ol class="mdw-list mdw-list-ordered"{start_attr}>\n' + "\n".join(items) + "\n</ol>"


def _render_blockquote(match: re.Match) -> str:
    """将匹配到的引用块片段渲染为 HTML <blockquote>。

    支持嵌套引用（多个 >）和多段落（空行 > 分隔）。
    """
    lines = match.group(0).splitlines()
    result_lines = []
    in_para = False
    pending: list[str] = []

    for raw in lines:
        # 去除引用标记
        stripped = re.sub(r"^>[ \t]?", "", raw)
        if not stripped.strip():
            # 空行：结束当前段落
            if pending:
                result_lines.append("<p>" + " ".join(pending) + "</p>")
                pending.clear()
            continue
        pending.append(stripped)

    if pending:
        result_lines.append("<p>" + " ".join(pending) + "</p>")

    inner = "\n".join(result_lines)
    return f'<blockquote class="mdw-blockquote">\n{inner}\n</blockquote>'


# ---------------------------------------------------------------------------
# 入口函数
# ---------------------------------------------------------------------------

def parse_file(path: Path, parser: Parser) -> str:
    """读取文件、渲染并返回 HTML 字符串。"""
    raw = path.read_text(encoding="utf-8")
    return parser.render(raw)
