"""
Site —— 高级编排器。

创建 HTTP 服务器，串接 Router、Parser、HotReloader 和 Admin 界面。
这是用户实例化的主类。
"""

from __future__ import annotations
import asyncio
import base64
import html
import io
import logging
import os
import sys
import traceback
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

from .router import Router, Route, RESERVED
from .parser import Parser, parse_file, apply_vars
from .extensions import Extension
from .hot_reload import HotReloader
from .templating import TemplateSet
from .nav_renderer import NavRenderer

logger = logging.getLogger("mdw.site")


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

_PACKAGE_DIR = Path(__file__).resolve().parent


def _get_internal_data_dir(name: str) -> Path | None:
    """获取包内捆绑的 data 目录路径（兼容开发/打包环境）。"""
    try:
        import importlib.resources as ir
        pkg = ir.files("mdw") / name
        if hasattr(pkg, "iterdir") and any(pkg.iterdir()):
            return Path(str(pkg))
    except Exception:
        pass
    # 回退：文件系统路径
    p = _PACKAGE_DIR / name
    return p if p.is_dir() else None


def _is_frozen() -> bool:
    """打包后的单文件 exe 环境。"""
    return getattr(sys, "frozen", False)


def _bundle_target_dir() -> Path:
    """内置资源应解压到的目标目录（exe 同目录，开发时为 CWD）。"""
    if _is_frozen():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def ensure_bundle_extracted() -> None:
    """首次运行：若目标目录缺少 docs/static/config，从内置资源解压。

    - 只在顶层目录缺失/为空时解压，**不会覆盖**用户已有内容；
    - 解压后自动补全 config/site.yaml（默认配置）；
    - 开发模式（无 _bundle_assets 模块）时静默跳过。
    """
    try:
        from . import _bundle_assets as bundle
    except ImportError:
        return  # 开发模式：资源已在源码目录

    target = _bundle_target_dir()
    raw = base64.b64decode(bundle.BUNDLE_ZIP_B64)

    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        # 按顶层目录分组
        by_top: dict[str, list[zipfile.ZipInfo]] = {}
        for info in z.infolist():
            if info.is_dir() or not info.filename:
                continue
            top = info.filename.split("/", 1)[0]
            by_top.setdefault(top, []).append(info)

        extracted_any = False
        for top, members in by_top.items():
            dest = target / top
            if dest.is_dir() and any(dest.iterdir()):
                continue  # 已存在且非空，不覆盖
            dest.mkdir(parents=True, exist_ok=True)
            for info in members:
                # 路径穿越防护
                rel = info.filename[len(top) + 1:] if "/" in info.filename else ""
                if not rel:
                    continue
                out = (dest / rel).resolve()
                if not str(out).startswith(str(dest.resolve())):
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(out, "wb") as dst:
                    dst.write(src.read())
            extracted_any = True
            logger.info("已从内置资源解压: %s", dest)

        if extracted_any:
            # 解压了任何资源时，确保 config 也在
            if not (target / "config" / "site.yaml").exists() and "config" not in by_top:
                logger.info("已生成默认配置: %s", target / "config")


def _find_resource_dir(name: str) -> Path:
    """查找资源目录，优先级：exe侧 > 包内捆绑 > 项目根。"""
    candidates = []

    if _is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / name)
        # PyInstaller
        if hasattr(sys, "_MEIPASS"):
            candidates.append(Path(sys._MEIPASS) / name)
    else:
        candidates.append(Path.cwd() / name)
        candidates.append(_PACKAGE_DIR.parent / name)

    # 包内捆绑数据（打包/开发均检查）
    internal = _get_internal_data_dir(name)
    if internal and internal not in candidates:
        candidates.append(internal)

    for p in candidates:
        if p.is_dir():
            return p
    return candidates[0]

    for p in candidates:
        if p.is_dir():
            return p
    # 回退到 exe 侧或 CWD 侧，等候用户创建
    return candidates[0]


# ---------------------------------------------------------------------------
# 简易 HTTP 服务器 —— 不依赖外部框架
# ---------------------------------------------------------------------------

try:
    import aiohttp
    from aiohttp import web

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    import http.server
    import socketserver
    import urllib.parse


_CONTENT_TYPES = {
    ".css": "text/css",
    ".js": "application/javascript",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".ico": "image/x-icon",
    ".avif": "image/avif",
    ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".wav": "audio/wav",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".ogv": "video/ogg",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def _guess_content_type(path: Path) -> str:
    """根据文件后缀猜测 Content-Type。"""
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


# 变量注入适用于这些文本扩展名；其余（图片/音视频/压缩包等）原样输出
_TEXT_EXTENSIONS = {".css", ".html", ".htm", ".js", ".txt", ".json", ".svg", ".xml", ".md"}


class MDWSite:
    """主站点构建器。

    典型用法：::

        from mdw import MDWSite
        from mdw.extensions import FrontMatterExtension, CustomBlockExtension

        site = MDWSite(docs_dir="docs", port=8080)
        site.use(FrontMatterExtension())
        site.use(CustomBlockExtension())
        site.run()
    """

    def __init__(
        self,
        docs_dir: str | Path = "docs",
        host: str = "127.0.0.1",
        port: int = 8080,
        admin_prefix: str = "/admin",
        auto_reload: bool = True,
        poll_interval: float = 1.0,
        use_builtin_parser: bool = True,
        static_dir: str | Path = "static",
        template_dir: str | Path = "",
        brand: str = "MDW",
        style_file: str = "style.css",
        script_file: str = "app.js",
        site_title: str = "MDW",
        variables: dict | None = None,
    ):
        """
        参数：
            docs_dir: 文档目录路径
            host: 监听地址
            port: 监听端口
            admin_prefix: 管理后台 URL 前缀
            auto_reload: 是否开启热重载
            poll_interval: 热重载轮询间隔（秒）
            use_builtin_parser: 使用内置还是 python-markdown
            static_dir: 静态文件目录（CSS/JS/图片）
            template_dir: 自定义模板目录（可选）。放入与内置同名的
                HTML 文件即可覆盖对应页面部件（见 mdw/templates/）
            brand: 侧边栏品牌名
            style_file: 主题样式文件名（static/ 下）
            script_file: 前端脚本文件名（static/ 下）
            site_title: 站点标题，用于浏览器标签标题后缀与页脚
                （默认 "MDW"）
            variables: 注入变量字典。mdw 文档与静态 CSS/HTML 文件中
                可用 ``{{ 名称 }}`` 引用；内置提供 ``site_title`` 与
                ``brand``。可通过 ``set_var()`` / ``update_vars()`` 修改
        """
        self.docs_dir = str(Path(docs_dir).resolve())
        self.host = host
        self.port = port
        self.admin_prefix = admin_prefix.rstrip("/")
        self.auto_reload = auto_reload
        self.use_builtin_parser = use_builtin_parser
        self.static_dir = (
            str(_find_resource_dir(static_dir).resolve())
            if static_dir else ""
        )
        # 首次运行：如果 static/ 为空，从内置嵌入资源写入默认主题
        if self.static_dir:
            self._seed_static_defaults()
        self.site_title = site_title
        self._variables: dict = dict(variables or {})

        # 核心组件
        self.router = Router(docs_dir=self.docs_dir)
        self.parser = Parser(use_builtin=self.use_builtin_parser)
        self.reloader: Optional[HotReloader] = None

        # 扩展列表
        self._extensions: list[Extension] = []

        # 管理后台标志
        self._admin_enabled = True

        # 页面缓存：url_path -> HTML
        self._cache: dict[str, str] = {}

        # 用户自定义的路由处理器
        self._custom_handlers: dict[str, Callable] = {}

        # 扩展注册的样式资源目录：前缀 → 绝对路径
        self._style_dirs: dict[str, str] = {}

        # 用户显式挂载的静态资源目录：前缀 → 绝对路径
        self._asset_dirs: dict[str, str] = {}

        # 页面外观配置
        self.brand = brand
        self.style_file = style_file.lstrip("/")
        self.script_file = script_file.lstrip("/")

        # 注入变量：内置提供 site_title / brand，用户变量随后合并
        self._variables.setdefault("site_title", self.site_title)
        self._variables.setdefault("brand", self.brand)
        self.parser.variables = self._variables

        # 模板集：用户自定义模板目录优先，内置模板兜底
        self._templates = TemplateSet(
            user_dir=template_dir,
            builtin_dir=Path(__file__).resolve().parent / "templates",
        )

        # 导航渲染器
        self._nav_renderer = NavRenderer(self._templates)

        # 服务器引用
        self._server = None
        self._stdlib_server = None
        self._loop = None

        logger.info(
            "MDWSite 初始化 —— docs=%s host=%s port=%d",
            self.docs_dir, self.host, self.port,
        )

    # ── 静态文件初始化 ────────────────────────────────────────

    def _seed_static_defaults(self) -> None:
        """打包后首次运行：确保外部 static/ 有主题文件（从内置嵌入资源写入）。"""
        target = Path(self.static_dir)
        if not target.exists() or not any(target.glob("*.css")) or not any(target.glob("*.js")):
            try:
                from ._static_assets import STYLE_CSS, APP_JS
                target.mkdir(parents=True, exist_ok=True)
                (target / "style.css").write_text(STYLE_CSS, encoding="utf-8")
                (target / "app.js").write_text(APP_JS, encoding="utf-8")
                logger.info("已从内置资源生成默认主题: %s", target)
            except ImportError:
                logger.warning("内置默认主题不可用，请手动放置 style.css / app.js 到 %s", target)

    # -- 公开 API -----------------------------------------------------------

    def use(self, ext: Extension) -> "MDWSite":
        """注册一个扩展。

        扩展会被添加到解析器管线，并触发其 on_startup 生命周期钩子。
        """
        self._extensions.append(ext)
        self.parser.register(ext)
        ext.on_startup(self)
        logger.info("扩展已注册: %s", ext.name)
        return self

    def set_var(self, name: str, value: Any) -> None:
        """设置一个注入变量（mdw 文档与静态 CSS/HTML 文件均可引用）。

        用法：::

            site.set_var("accent", "#6750a4")
            site.set_var("repo_url", "https://github.com/example/repo")
        """
        self._variables[name] = value
        self.parser.variables = self._variables

    def update_vars(self, mapping: dict) -> None:
        """批量设置注入变量。"""
        self._variables.update(mapping or {})
        self.parser.variables = self._variables

    @property
    def variables(self) -> dict:
        """当前注入变量字典（只读视图，修改请用 set_var / update_vars）。"""
        return dict(self._variables)

    def handler(self, path: str) -> Callable:
        """装饰器：注册自定义路由处理器。

        用法：::

            @site.handler("/api/custom")
            async def custom(request):
                return web.json_response({"ok": True})
        """
        def decorator(fn: Callable) -> Callable:
            self._custom_handlers[path] = fn
            return fn
        return decorator

    def register_api(self, token: str = "mdw-admin") -> None:
        """注册管理 API（``/api/`` 前缀）。

        在 ``site.build()`` 之后调用，或通过 ``app.py`` 配置。
        """
        from .api import APIHandler
        self._api_token = token
        self._api = APIHandler(self)

    def register_style_dir(self, prefix: str, path: str | Path) -> None:
        """注册一个可通过 ``/<prefix>/<相对路径>`` 访问的资源目录。

        用于自定义样式扩展引入外部 CSS / HTML 文件；目录路径会被解析为
        绝对路径并做越界防护。
        """
        self._style_dirs[prefix.strip("/")] = str(Path(path).resolve())

    def mount_static(self, prefix: str, path: str | Path) -> None:
        """挂载一个静态资源目录，URL 前缀 → 本地目录。

        例如 ``site.mount_static("/media", "docs/media")`` 后，
        ``docs/media/photo.png`` 可通过 ``/media/photo.png`` 访问。
        图片、音频、视频、PDF 等任意文件均可；目录路径会被解析为
        绝对路径并做越界防护。
        """
        self._asset_dirs[prefix.strip("/")] = str(Path(path).resolve())

    def _resolve_asset(self, url_path: str) -> Optional[tuple[Path, str]]:
        """把 URL 路径解析为可提供的静态资源，返回 ``(文件绝对路径, Content-Type)``。

        解析顺序：
          1. 显式挂载的静态资源目录（``mount_static``）
          2. ``docs/`` 目录内的非文档文件（按相对 URL 提供，含越界防护）

        找不到、越界、或命中文档源文件（.mdw/.md/.markdown）、保留名与
        ``_`` 前缀片段时返回 None。
        """
        doc_exts = (".mdw", ".md", ".markdown")

        # 1) 显式挂载的静态资源目录
        for prefix, base in self._asset_dirs.items():
            marker = "/" + prefix + "/"
            if not url_path.startswith(marker):
                continue
            base_path = Path(base)
            full = (base_path / url_path[len(marker):]).resolve()
            if full.is_relative_to(base_path) and full.is_file():
                return full, _guess_content_type(full)
            return None

        # 2) docs 目录内非文档文件
        rel = url_path.strip("/")
        if not rel:
            return None
        base_path = Path(self.docs_dir)
        full = (base_path / rel).resolve()
        if not full.is_relative_to(base_path) or not full.is_file():
            return None
        stem = full.stem.lower()
        if stem.startswith("_") or stem in RESERVED:
            return None
        if full.suffix.lower() in doc_exts:
            return None
        return full, _guess_content_type(full)

    def _file_with_vars(self, full: Path) -> bytes:
        """读取静态文件；文本类文件（CSS/HTML/JS 等）应用注入变量替换。

        二进制文件（图片/音视频/压缩包等）原样返回。
        """
        data = full.read_bytes()
        if full.suffix.lower() not in _TEXT_EXTENSIONS:
            return data
        text = data.decode("utf-8", errors="replace")
        return apply_vars(text, self._variables).encode("utf-8")

    def build(self) -> None:
        """强制完整重建路由和页面缓存。"""
        logger.info("正在构建站点 ...")
        self.router.rebuild()
        self._rebuild_cache()
        logger.info("构建完成 —— %d 个路由", len(self.router.routes))

    def run(
        self,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        debug: bool = False,
    ) -> None:
        """
        启动 HTTP 服务器。

        参数：
            host: 覆盖构造时的地址
            port: 覆盖构造时的端口
            debug: 开启调试日志和详细输出
        """
        if debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        else:
            logging.basicConfig(
                level=logging.INFO,
                format="[%(levelname)s] %(message)s",
            )

        if host:
            self.host = host
        if port:
            self.port = port

        # 初始构建
        self.build()

        # 启动热重载
        if self.auto_reload:
            self.reloader = HotReloader(
                watch_dir=self.docs_dir,
                interval=1.0,
            )
            self.reloader.start(on_change=self._on_file_change)

        # 启动服务器
        if HAS_AIOHTTP:
            self._run_aiohttp()
        else:
            self._run_stdlib()

    def stop(self) -> None:
        """优雅关闭服务器。"""
        if self.reloader:
            self.reloader.stop()
        if getattr(self, "_stdlib_server", None) is not None:
            try:
                self._stdlib_server.shutdown()
            except Exception:
                pass
        for ext in self._extensions:
            try:
                ext.on_shutdown(self)
            except Exception:
                logger.exception("扩展关闭出错: %s", ext.name)
        logger.info("MDWSite 已停止")

    # -- 文件变更回调 -----------------------------------------------

    def _on_file_change(self, changed_paths: list[str]) -> None:
        """HotReloader 检测到文件变更时调用。"""
        logger.info("热重载触发: %s", changed_paths[:3])
        try:
            self.router.rebuild()
            self._rebuild_cache()
            for ext in self._extensions:
                try:
                    ext.on_reload(changed_paths)
                except Exception:
                    logger.exception("扩展重载出错: %s", ext.name)
            logger.info("热重载完成")
        except Exception as exc:
            logger.error("热重载失败 —— 继续使用过期缓存: %s", exc)

    def _rebuild_cache(self) -> None:
        """重新渲染所有页面到缓存。"""
        self._cache.clear()
        for route in self.router.routes:
            try:
                self._cache[route.path] = self._render_route(route)
            except Exception as exc:
                logger.error("渲染失败 %s: %s\n%s", route.path, exc,
                             traceback.format_exc())
                self._cache[route.path] = self._error_html(route, str(exc))

    def _render_route(self, route: Route) -> str:
        """渲染单个路由的完整页面（文档 / 目录列表）。"""
        try:
            if route.source_path and route.source_path.is_file():
                body = parse_file(route.source_path, self.parser)
            elif route.source_path and route.source_path.is_dir():
                body = self._dir_listing_html(route)
            else:
                return self._error_html(route, "源文件未找到")
        except Exception as exc:
            logger.error("渲染页面失败 %s: %s\n%s", route.path, exc,
                         traceback.format_exc())
            return self._error_html(route, f"渲染失败: {exc}")
        return self._wrap_html(route, body)

    # -- HTML 包装 --------------------------------------------------

    def _wrap_html(self, route: Route, body: str) -> str:
        """将渲染后的内容包裹在完整的 HTML 页面中（模板驱动）。"""
        nav_html = self._nav_renderer.render_nav(self.router.nav_roots(), route)
        head_extra = "".join(
            tag for ext in self._extensions for tag in ext.head_assets(self)
        )
        ctx = {
            "head": self._templates.render("head.html", {
                "title": html.escape(route.title),
                "site_title": html.escape(self.site_title),
                "style_href": f"/static/{self.style_file}",
                "head_extra": head_extra,
                "script_src": f"/static/{self.script_file}",
            }),
            "sidenav_btn": self._templates.render("sidenav_btn.html", {}),
            "brand": html.escape(self.brand),
            "site_title": html.escape(self.site_title),
            "page_title": html.escape(route.title),
            "nav": self._templates.render("nav.html", {
                "brand": html.escape(self.brand),
                "nav_tree": nav_html,
            }),
            "content": body,
            "footer": self._templates.render("footer.html", {
                "site_title": html.escape(self.site_title),
            }),
            "script_src": f"/static/{self.script_file}",
        }
        return self._templates.render("base.html", ctx)

    # -- 侧边栏导航 --------------------------------------------------

    def _render_nav(self, current: Route) -> str:
        """从导航树生成侧栏导航（委托给 NavRenderer）。"""
        return self._nav_renderer.render_nav(self.router.nav_roots(), current)

    def _dir_listing_html(self, route: Route) -> str:
        """为没有独立文档的目录生成子页面列表（委托给 NavRenderer）。"""
        return self._nav_renderer.render_dir_listing(route)

    def _error_html(self, route: Route, message: str) -> str:
        """生成渲染错误的 HTML 页面（模板驱动）。"""
        return self._templates.render("error.html", {
            "style_href": f"/static/{self.style_file}",
            "message": html.escape(message),
        })

    # -- aiohttp 服务器 ----------------------------------------------------

    def _run_aiohttp(self) -> None:
        """使用 aiohttp 启动异步 HTTP 服务器。"""
        app = web.Application()

        # 静态文件
        if self.static_dir and Path(self.static_dir).is_dir():
            app.router.add_static("/static", path=self.static_dir)

        # 扩展注册的样式资源目录（外部 CSS 等）
        for prefix, base in self._style_dirs.items():
            app.router.add_get(
                f"/{prefix}/{{tail:.*}}",
                self._make_style_handler(base),
            )

        # 管理后台路由
        self._mount_admin(app)

        # 自定义路由处理器
        for path, fn in self._custom_handlers.items():
            app.router.add_route("*", path, fn)

        # 管理 API 路由 (匹配 /api/*)
        if hasattr(self, "_api"):
            api = self._api
            app.router.add_route("*", "/api/{tail:.*}", api.handle_aiohttp)

        # 通配路由：从文档目录查找
        app.router.add_get("/{tail:.*}", self._aiohttp_handler)

        logger.info("启动 aiohttp 服务器 %s:%d", self.host, self.port)
        web.run_app(app, host=self.host, port=self.port)

    def _make_style_handler(self, base: str) -> Callable:
        """为样式资源目录创建 aiohttp 处理器（含越界防护）。"""
        base_path = Path(base)

        async def handler(request: Any) -> Any:
            from aiohttp import web
            rel = request.match_info.get("tail", "")
            full = (base_path / rel).resolve()
            if not full.is_relative_to(base_path) or not full.is_file():
                return web.Response(status=404, text="404 Not Found")
            return web.Response(body=self._file_with_vars(full), content_type=_guess_content_type(full))

        return handler

    async def _aiohttp_handler(self, request: Any) -> Any:
        """aiohttp 的文档请求处理器。"""
        from aiohttp import web
        url_path = "/" + request.match_info.get("tail", "").strip("/")

        route = self.router.resolve(url_path)
        if route is None:
            # 静态资源（图片/音频/视频等）兜底
            asset = self._resolve_asset(url_path)
            if asset is not None:
                full, content_type = asset
                return web.Response(body=self._file_with_vars(full), content_type=content_type)
            return web.Response(text="404 未找到", status=404)

        html = self._cache.get(route.path, "")
        if not html:
            try:
                html = self._render_route(route)
                self._cache[route.path] = html
            except Exception as exc:
                html = self._error_html(route, str(exc))

        return web.Response(text=html, content_type="text/html", charset="utf-8")

    def _mount_admin(self, app: Any) -> None:
        """将管理后台路由挂载到 aiohttp 应用。"""
        from aiohttp import web
        admin = AdminPage(self)

        async def dashboard(_request):
            return web.Response(text=admin.dashboard(), content_type="text/html")

        async def files(_request):
            return web.Response(text=admin.file_tree(), content_type="text/html")

        async def view(request):
            rel = request.match_info.get("path", "")
            return web.Response(text=admin.view_source(rel), content_type="text/html")

        async def reload_get(_request):
            return web.Response(text=admin.reload_page(), content_type="text/html")

        async def reload_post(_request):
            self.build()
            return web.Response(text=admin.reload_page("文档已重新加载"), content_type="text/html")

        p = self.admin_prefix
        app.router.add_get(p, dashboard)
        app.router.add_get(p + "/files", files)
        app.router.add_get(p + "/view/{path:.*}", view)
        app.router.add_get(p + "/reload", reload_get)
        app.router.add_post(p + "/reload", reload_post)

    # -- stdlib 回退服务器（无 aiohttp）----------------------------------

    def _run_stdlib(self) -> None:
        """使用 Python 标准库启动同步 HTTP 服务器。"""
        logger.info(
            "启动 stdlib 服务器 %s:%d（安装 aiohttp 可获得更好性能）",
            self.host, self.port,
        )

        class MDWHandler(http.server.BaseHTTPRequestHandler):
            site = self  # 通过闭包绑定

            def do_GET(self):
                self._handle()

            def do_POST(self):
                self._handle()

            def do_DELETE(self):
                self._handle()

            def do_PUT(self):
                self._handle()

            def _handle(self):
                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path

                # 管理后台
                if path.startswith(self.site.admin_prefix):
                    resp = self.site._handle_admin(path)
                    if resp:
                        self._send_text(resp)
                        return

                # 管理 API
                if path.startswith("/api/") and hasattr(self.site, "_api"):
                    self._serve_api(self.path)  # 传完整 URL（含 query string）
                    return

                # 自定义路由
                if path in self.site._custom_handlers:
                    resp = self.site._custom_handlers[path](self)
                    if isinstance(resp, str):
                        self._send_text(resp)
                    return

                # 静态文件
                if path.startswith("/static/"):
                    self._serve_static(path)
                    return

                # 扩展注册的样式资源目录
                if self._serve_style_resource(path):
                    return

                # 文档页面
                route = self.site.router.resolve(path)
                if not route:
                    # 静态资源（图片/音频/视频等）兜底
                    asset = self.site._resolve_asset(path)
                    if asset is not None:
                        full, content_type = asset
                        self._send_bytes(self.site._file_with_vars(full), content_type)
                        return
                    self.send_response(404)
                    self.end_headers()
                    return

                html = self.site._cache.get(route.path, "")
                if not html:
                    try:
                        html = self.site._render_route(route)
                        self.site._cache[route.path] = html
                    except Exception as exc:
                        html = self.site._error_html(route, str(exc))

                self._send_text(html)

            def _send_text(self, text: str, status: int = 200):
                """发送文本响应。"""
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_api(self, full_path: str):
                """将 /api/* 请求委托给 APIHandler。"""
                import json as _json
                # 模拟 request 对象
                class APIRequest:
                    pass
                req = APIRequest()
                req.path = full_path
                req.command = self.command
                req.headers = {k.lower(): v for k, v in self.headers.items()}
                content_len = int(self.headers.get("Content-Length", 0))
                if content_len > 0:
                    req.rfile = self.rfile
                else:
                    req.rfile = None

                result = self.site._api.handle_stdlib(req)
                if result is True:
                    return  # handle_stdlib 自己发送了响应
                if isinstance(result, dict):
                    if "__html__" in result:
                        # /api/ 下的文档页面（docs/api/）
                        body = result["__html__"].encode("utf-8")
                        self.send_response(result.get("status", 200))
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if "__raw__" in result:
                        # 原始文件下载（source 端点）
                        raw = result["__raw__"]
                        fname = result.get("filename", "file")
                        self.send_response(result.get("status", 200))
                        self.send_header("Content-Type", result.get("content_type", "application/octet-stream"))
                        self.send_header("Content-Length", str(len(raw)))
                        self.send_header("Content-Disposition", 'attachment; filename="%s"' % fname)
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    data = result.get("data", {})
                    status = result.get("status", 200)
                    body = _json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
                    self._send_bytes(body, "application/json; charset=utf-8", status)

            def _send_bytes(self, data: bytes, content_type: str, status: int = 200):
                """发送二进制响应。"""
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_static(self, path: str):
                """提供静态文件服务（含变量注入）。"""
                rel = path[len("/static/"):]
                full = Path(self.site.static_dir) / rel
                if not full.is_file():
                    self.send_response(404)
                    self.end_headers()
                    return
                self._send_bytes(self.site._file_with_vars(full), _guess_content_type(full))

            def _serve_style_resource(self, path: str) -> bool:
                """提供扩展注册的样式资源（外部 CSS 等），含越界防护。"""
                for prefix, base in self.site._style_dirs.items():
                    marker = "/" + prefix + "/"
                    if not path.startswith(marker):
                        continue
                    base_path = Path(base)
                    full = (base_path / path[len(marker):]).resolve()
                    if not full.is_relative_to(base_path) or not full.is_file():
                        self.send_response(404)
                        self.end_headers()
                        return True
                    self._send_bytes(self.site._file_with_vars(full), _guess_content_type(full))
                    return True
                return False

            def log_message(self, fmt, *args):
                """将标准库日志重定向到 logging 模块。"""
                logger.info("%s - %s", self.client_address[0], fmt % args)

        server = socketserver.TCPServer((self.host, self.port), MDWHandler)
        self._stdlib_server = server
        logger.info("正在提供 http://%s:%d/", self.host, self.port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()

    def _handle_admin(self, path: str) -> Optional[str]:
        """处理管理后台请求（stdlib 模式）。"""
        from .admin import AdminPage
        admin = AdminPage(self)
        p = self.admin_prefix
        rest = path[len(p):]
        if rest in ("", "/"):
            return admin.dashboard()
        if rest in ("/files", "/files/"):
            return admin.file_tree()
        if rest.startswith("/view/"):
            rel = rest[len("/view/"):]
            return admin.view_source(rel)
        if rest in ("/reload", "/reload/"):
            self.build()
            return admin.reload_page("文档已重新加载")
        return None


# 延迟导入以打破 stdlib 模式下的循环依赖
from .admin import AdminPage
