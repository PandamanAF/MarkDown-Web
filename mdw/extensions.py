"""
扩展系统 — MDW 的自定义接口。

三个钩子类别（按顺序执行）：
  1. 解析前钩子（pre_parse） — 在 Markdown 转 HTML 之前处理原始文本
  2. 渲染后钩子（post_render）—— 在 Markdown→HTML 转换之后处理 HTML
  3. 中间件钩子（middleware） — 在每个 HTTP 请求上执行

每个 Extension 子类可以覆盖任意组合的钩子方法。
扩展通过 ``site.use(MyExtension())`` 注册。

内置扩展：
  - FrontMatterExtension  —— 解析文档头部元数据（供路由与扩展共同使用）
  - CustomBlockExtension  —— 自定义样式块（可配置识别字符、外部 CSS / HTML）
  - CodeHighlightExtension —— 代码块 CSS 类包装
  - TemplateExtension     —— ``{{ 变量 }}`` 模板变量替换
"""

from __future__ import annotations
import html as _html
import re
from pathlib import Path
from typing import Any, Callable, Optional

from .metadata import parse_front_matter

# ---------------------------------------------------------------------------
# 钩子类型别名
# ---------------------------------------------------------------------------

# 自定义块头部变量声明：key="值"（多个，空格分隔）
_BLOCK_VAR_RE = re.compile(r'([A-Za-z_][\w-]*)\s*=\s*"([^"]*)"')

PreParseHook = Callable[[str], str]             # 原始源码 → 原始源码
PostRenderHook = Callable[[str], str]           # HTML → HTML
MiddlewareHook = Callable[["请求对象", Callable], "响应对象"]  # 异步


class Extension:
    """基类 —— 可以覆盖任意数量的钩子方法。"""

    name: str = "未命名扩展"

    # -- 解析管线钩子 -----------------------------------------------
    def pre_parse(self, raw_source: str) -> str:
        """在 Markdown 解析器运行之前转换原始源码。"""
        return raw_source

    def post_render(self, html: str) -> str:
        """在 Markdown 解析器运行之后转换最终的 HTML。"""
        return html

    # -- 页面级钩子 --------------------------------------------------
    def head_assets(self, site: Any) -> list[str]:
        """
        返回需要注入页面 <head> 的标签列表（如外部 CSS / JS）。

        在 ``_wrap_html`` 组装完整页面时被调用，可依赖 ``site.docs_dir`` 等。
        """
        return []

    # -- 路由 / 中间件钩子 ------------------------------------------
    def before_request(self, request: Any, handler: Callable[..., Any]):
        """在路由处理器之前调用的异步中间件。"""
        return handler(request)

    def after_request(self, request: Any, response: Any):
        """发送之前对响应进行后处理。"""
        return response

    # -- 生命周期钩子 -----------------------------------------------
    def on_startup(self, site: Any):
        """服务器启动时调用一次。"""

    def on_shutdown(self, site: Any):
        """服务器关闭时调用一次。"""

    def on_reload(self, paths: list[str]):
        """热重载检测到文件变更后调用。"""


# ===================================================================
# 内置扩展
# ===================================================================

class FrontMatterExtension(Extension):
    """解析 MD 源码头部 YAML 风格的元数据 ``---\\n键: 值\\n---``。

    元数据被剥离出正文，并可通过 ``get_meta()`` 读取（供模板/自定义行为使用）。
    路由系统在扫描时也会独立解析元数据以定制导航，两者互不干扰。
    """

    name = "front_matter"
    _current_meta: dict = {}

    def pre_parse(self, raw_source: str) -> str:
        meta, rest = parse_front_matter(raw_source)
        self._current_meta = meta
        return rest

    def get_meta(self) -> dict:
        """获取当前页面解析出的元数据。"""
        return dict(getattr(self, "_current_meta", {}))


class CodeHighlightExtension(Extension):
    """为前端高亮库包装 ``<code class=\"language-xxx\">`` 代码块。

    给所有 ``<pre><code>`` 块添加 ``mdw-code`` CSS 类。
    """

    name = "code_highlight"

    def post_render(self, html: str) -> str:
        return html.replace('<pre><code', '<pre class="mdw-code"><code')


class CustomBlockExtension(Extension):
    """自定义样式块。

    语法：::

        ::: note
        内容在此
        :::

    渲染为 ``<div class=\"mdw-block mdw-note\">...</div>``。

    特性：
      - **多识别字符** —— ``markers`` 参数可配置多个，例如 ``(":::", "[[[")``，
        每个识别字符各自配对闭合（``::: ... :::``、``[[[ ... [[[``）。
      - **样式行为** —— ``styles`` 参数为每种样式名定义行为：::

            CustomBlockExtension(
                markers=(":::", "[[["),
                styles={
                    "note":  {"class": "mdw-block mdw-note"},
                    "card":  {"html": "card.html"},                 # 外部 HTML 模板
                    "theme": {"css": "theme.css"},                  # 外部 CSS 文件
                },
            )

        - ``class``：最终渲染的 CSS 类（默认 ``mdw-block mdw-<样式名>``）
        - ``css``：引入外部 CSS 文件。以 ``http(s)://`` 开头视为完整 URL，
          否则视为样式目录（默认 ``docs/_styles/``）下的相对路径，
          通过 ``/_styles/<路径>`` 提供给浏览器
        - ``html``：使用外部 HTML 文件作为模板。模板中的 ``{{content}}``
          会被替换为块内容；没有占位符时，块内容追加在模板之后。
        - ``vars``：该样式的**默认模板变量**字典，可声明多个、名称可自定义；
          文档块头部可直接用 ``样式名 key="值" key2="值2"`` 的格式覆盖。

      - **模板变量（多个、可自定义）** —— 变量优先级：
        文档块级变量 > 样式默认变量（``vars``）> 站点全局变量（``variables``）。
        - HTML 模板中用 ``{{ 变量名 }}`` 引用（与 ``{{content}}`` 并存）
        - 样式级 / 块级变量会自动以 CSS 自定义属性 ``--变量名: 值`` 注入
          块的 ``style`` 属性，CSS 文件中用 ``var(--变量名)`` 引用

      - **代码围栏保护** —— 解析时先提取代码围栏（`` ``` `` / ``~~~`` 块），
        避免代码中的识别字符被误识别。
    """

    name = "custom_blocks"

    def __init__(
        self,
        markers: tuple[str, ...] = (":::",),
        styles: Optional[dict] = None,
        style_dir: Optional[str | Path] = None,
    ):
        """
        参数：
            markers: 识别字符集合（每个可含多个字符），默认 ``(":::",)``
            styles: 样式名 → 行为配置字典（class / css / html）
            style_dir: 外部 CSS / HTML 文件所在目录
                       （默认 ``<docs目录>/_styles``，该目录不会被注册为路由）
        """
        self._markers = tuple(markers) if markers else (":::",)
        self._styles: dict = dict(styles or {})
        self._style_dir: Optional[Path] = None
        self._style_dir_hint = style_dir
        # 块头部支持变量声明：样式名 key="值" key2="值2"（可直接识别）
        self._patterns = [
            re.compile(
                rf"^{re.escape(m)}[ \t]+([\w-]+)"
                rf"((?:[ \t]+[A-Za-z_][\w-]*[ \t]*=[ \t]*\"[^\"]*\")*)[ \t]*\n"
                rf"(.*?)\n{re.escape(m)}[ \t]*$",
                re.MULTILINE | re.DOTALL,
            )
            for m in self._markers
        ]
        # 保护代码围栏（```...``` 与 ~~~...~~~），避免内部内容被误识别
        self._fence_pattern = re.compile(r"(```+|~~~+)[^\n]*\n.*?^\1\s*$", re.DOTALL | re.MULTILINE)
        self._placeholder = "\x00MDW_FENCE_{}\x00"

    def on_startup(self, site: Any) -> None:
        """确定样式目录并注册为可访问的资源前缀。"""
        if self._style_dir_hint:
            base = Path(self._style_dir_hint)
            if not base.is_absolute():
                base = Path(site.docs_dir) / base
        else:
            base = Path(site.docs_dir) / "_styles"
        self._style_dir = base
        self._site = site
        site.register_style_dir("_styles", base)

    def head_assets(self, site: Any) -> list[str]:
        """为使用了外部 CSS 的样式生成 <link> 标签。"""
        tags = []
        for conf in self._styles.values():
            css = conf.get("css")
            if not css:
                continue
            css = str(css)
            if css.startswith(("http://", "https://", "//")):
                href = css
            else:
                href = "/_styles/" + css.lstrip("/")
            tags.append(f'<link rel="stylesheet" href="{_html.escape(href, quote=True)}">')
        return tags

    def pre_parse(self, raw_source: str) -> str:
        """
        在 Markdown 转换前解析自定义块（避免块内容被段落包装破坏）。

        代码围栏（``` 块）会先被保护起来，块内若包含识别字符不会被误识别。
        """
        # 1. 保护代码围栏（内置渲染器与 python-markdown 的 fenced_code 均识别）
        protected: list[str] = []

        def _hide(m: re.Match) -> str:
            token = self._placeholder.format(len(protected))
            protected.append(m.group(0))
            return token

        raw_source = self._fence_pattern.sub(_hide, raw_source)

        # 2. 依次应用每种识别字符
        for pattern in self._patterns:
            raw_source = pattern.sub(self._replace, raw_source)

        # 3. 还原代码围栏
        for i, original in enumerate(protected):
            raw_source = raw_source.replace(self._placeholder.format(i), original)
        return raw_source

    def _replace(self, m: re.Match) -> str:
        """将匹配到的块转换为 HTML（支持模板变量声明）。"""
        name = m.group(1)
        kv_text = m.group(2)
        inner = m.group(3)
        conf = self._styles.get(name) or {}
        cls = conf.get("class") or f"mdw-block mdw-{name}"
        content = inner
        html_file = conf.get("html")
        if html_file:
            content = self._load_html_template(str(html_file), inner, self._vars_for(conf, kv_text))
        # 块内容保持多行，交给后续 Markdown 管线处理
        return f'<div class="{cls}"{self._style_attr(conf, kv_text)}>\n{content}\n</div>'

    @staticmethod
    def _parse_block_vars(kv_text: str) -> dict:
        """解析块头部 ``key="值"`` 变量声明（多个、可直接识别）为字典。"""
        return dict(_BLOCK_VAR_RE.findall(kv_text or ""))

    def _vars_for(self, conf: dict, kv_text: str) -> dict:
        """合并模板变量：站点全局 < 样式默认（vars）< 文档块级。"""
        site = getattr(self, "_site", None)
        ctx = dict(getattr(site, "_variables", None) or {})
        ctx.update(conf.get("vars") or {})
        ctx.update(self._parse_block_vars(kv_text))
        ctx.pop("content", None)  # 保留 {{content}} 占位符的语义
        return ctx

    def _style_attr(self, conf: dict, kv_text: str) -> str:
        """把样式/块级变量以 ``--名称: 值`` 注入 style，供 CSS var() 引用。"""
        merged = dict(conf.get("vars") or {})
        merged.update(self._parse_block_vars(kv_text))
        merged.pop("content", None)
        if not merged:
            return ""
        parts = "; ".join(f"--{k}: {_html.escape(str(v), quote=True)}" for k, v in merged.items())
        return f' style="{parts}"'

    def _load_html_template(self, rel: str, content: str, ctx: Optional[dict] = None) -> str:
        """读取外部 HTML 模板并注入块内容与模板变量。"""
        full = self._style_dir / rel
        try:
            template = full.read_text(encoding="utf-8")
        except OSError:
            note = f'<div class="mdw-block mdw-error">[自定义 HTML 模板未找到: {_html.escape(rel)}]</div>'
            return note + "\n" + content
        # 应用模板变量（{{ 名称 }}），{{content}} 占位符保持不变
        if ctx:
            from .parser import apply_vars  # 延迟导入避免循环依赖
            template = apply_vars(template, ctx)
        if "{{content}}" in template:
            return template.replace("{{content}}", content)
        return template.rstrip() + "\n" + content


class TemplateExtension(Extension):
    """在文档源码中做简单的 ``{{ 变量 }}`` 替换。

    变量来自构造参数传入的字典，然后与每页的头部元数据合并。
    """

    name = "template"
    _VAR_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")

    def __init__(self, variables: Optional[dict] = None):
        self.global_vars: dict = variables or {}

    def pre_parse(self, raw_source: str) -> str:
        ctx = {**self.global_vars}
        # 如果已解析头部信息，合并页面级变量
        fm_ext = getattr(self, "_fm_ext", None)
        if fm_ext:
            ctx.update(fm_ext.get_meta())
        return self._VAR_PATTERN.sub(lambda m: ctx.get(m.group(1), m.group(0)), raw_source)
