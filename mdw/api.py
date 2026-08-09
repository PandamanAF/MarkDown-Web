"""
管理 API —— 提供服务器管理接口。

路由前缀 ``/api/``，通过 Bearer Token 鉴权。
支持 stdlib 和 aiohttp 双后端。

端点：
    GET    /api/routes         路由列表
    GET    /api/status         服务器状态
    GET    /api/page/{path}    页面渲染结果
    POST   /api/reload         触发热重载
    POST   /api/upload         上传文档文件
    DELETE /api/page/{path}    删除文档文件
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .site import MDWSite

logger = logging.getLogger("mdw.api")

# ---------------------------------------------------------------------------
# Token 鉴权
# ---------------------------------------------------------------------------

DEFAULT_TOKEN = "mdw-admin"

# 已知 API 端点；其余 /api/* 路径回退为文档渲染（docs/api/ 下的文档）
API_ACTIONS = {"routes", "status", "reload", "upload", "page", "source"}

# 源文件类型 → Content-Type（不含 charset，aiohttp 需分开传）
_SOURCE_TYPES = {
    ".md": "text/markdown",
    ".mdw": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
}


def _check_auth(site: "MDWSite", request: Any, is_aiohttp: bool = False) -> bool:
    """验证 Bearer Token。"""

    def _extract_auth_header() -> Optional[str]:
        if is_aiohttp:
            auth = request.headers.get("Authorization", "")
        else:
            # stdlib 已经统一为小写 key
            auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return None

    def _extract_query_param() -> Optional[str]:
        if is_aiohttp:
            return request.query.get("token", None)
        else:
            import urllib.parse
            parsed = urllib.parse.urlparse(request.path)
            qs = urllib.parse.parse_qs(parsed.query)
            return qs.get("token", [None])[0]

    expected = getattr(site, "_api_token", None) or DEFAULT_TOKEN
    token = _extract_auth_header() or _extract_query_param() or ""
    return token == expected


# ===================================================================
# API 路由分发
# ===================================================================

class APIHandler:
    """统一 API 请求处理器。"""

    def __init__(self, site: "MDWSite"):
        self.site = site

    # -- stdlib 入口 -------------------------------------------------

    def handle_stdlib(self, handler) -> bool:
        """处理 stdlib HTTP 请求。返回 True 表示已处理。"""
        import urllib.parse
        parsed = urllib.parse.urlparse(handler.path)
        rest = parsed.path[len("/api/"):].strip("/")
        method = handler.command
        action = rest.split("/")[0] if rest else ""
        if action not in API_ACTIONS:
            # /api/ 下的文档页面（docs/api/）回退文档渲染
            return self._fallback_doc_stdlib(handler, rest)
        return self._dispatch(method, rest, handler, is_aiohttp=False)

    # -- aiohttp 入口 -------------------------------------------------

    async def handle_aiohttp(self, request) -> Any:
        from aiohttp import web
        rest = request.match_info.get("tail", "").strip("/")
        method = request.method
        action = rest.split("/")[0] if rest else ""
        if action not in API_ACTIONS:
            # /api/ 下的文档页面（docs/api/）回退文档渲染
            return await self._fallback_doc_aiohttp(request, rest)
        result = self._dispatch(method, rest, request, is_aiohttp=True)
        # _upload_aiohttp 是异步的，需要 await
        if asyncio.iscoroutine(result):
            result = await result
        return result

    # -- /api/ 下文档页面的回退渲染 ---------------------------------

    def _fallback_doc_stdlib(self, handler, rest: str):
        """stdlib：渲染 docs/api/ 下的文档页（如 /api/、/api/reference）。"""
        url_path = "/api" + ("/" + rest if rest else "/")
        route = self.site.router.resolve(url_path)
        if route is None:
            return {"__html__": "404 未找到", "status": 404}
        html = self.site._render_route(route)
        return {"__html__": html, "status": 200}

    async def _fallback_doc_aiohttp(self, request, rest: str):
        """aiohttp：渲染 docs/api/ 下的文档页。"""
        from aiohttp import web
        url_path = "/api" + ("/" + rest if rest else "/")
        route = self.site.router.resolve(url_path)
        if route is None:
            return web.Response(text="404 未找到", status=404)
        html = self.site._cache.get(route.path, "")
        if not html:
            html = self.site._render_route(route)
            self.site._cache[route.path] = html
        return web.Response(text=html, status=200,
                            content_type="text/html", charset="utf-8")

    # -- 分发 ---------------------------------------------------------

    def _dispatch(self, method: str, rest: str, request, is_aiohttp: bool):
        if not _check_auth(self.site, request, is_aiohttp):
            return self._json({"error": "unauthorized"}, 401, is_aiohttp)

        # 提取子路径参数
        sub = None
        if rest:
            parts = rest.split("/", 1)
            action = parts[0]
            sub = parts[1] if len(parts) > 1 else None
        else:
            action = ""

        try:
            if action == "routes" and method == "GET":
                return self._routes(is_aiohttp)
            if action == "status" and method == "GET":
                return self._status(is_aiohttp)
            if action == "reload" and method == "POST":
                return self._reload(is_aiohttp)
            if action == "upload" and method == "POST":
                if is_aiohttp:
                    return self._upload_aiohttp(request)
                else:
                    return self._upload_stdlib(request)
            if action == "page":
                # 空路径（/api/page 或 /api/page/）→ 返回首页
                path_arg = sub or ""
                if method == "GET":
                    return self._get_page(path_arg, is_aiohttp)
                if method == "DELETE":
                    return self._delete_page(path_arg, is_aiohttp)
            if action == "source" and method == "GET":
                # 下载原始文档文件（.md/.mdw），空路径 → 首页源文件
                return self._get_source(sub or "", is_aiohttp)
            return self._json({"error": "not found", "action": action}, 404, is_aiohttp)
        except Exception as exc:
            logger.error("API error %s %s: %s\n%s", method, rest, exc,
                         traceback.format_exc())
            return self._json({"error": str(exc)}, 500, is_aiohttp)

    # ── 响应工具 ───────────────────────────────────────────────

    def _json(self, data, status=200, is_aiohttp=False):
        if is_aiohttp:
            from aiohttp import web
            body = json.dumps(data, ensure_ascii=False, indent=2)
            return web.Response(text=body, status=status,
                                content_type="application/json",
                                charset="utf-8")
        else:
            class FakeRequest:
                pass
            # We need the stdlib handler reference
            return {"data": data, "status": status}

    # ── API 实现 ────────────────────────────────────────────────

    def _routes(self, is_aiohttp: bool):
        """GET /api/routes — 返回所有路由列表。"""
        routes = []
        for r in self.site.router.routes:
            routes.append({
                "path": r.path,
                "title": r.title,
                "source": str(r.source_path) if r.source_path else None,
                "sort_key": r.sort_key,
            })
        routes.sort(key=lambda r: (r["sort_key"], r["path"]))
        return self._json({"routes": routes, "total": len(routes)}, is_aiohttp=is_aiohttp)

    def _status(self, is_aiohttp: bool):
        """GET /api/status — 服务器状态信息。"""
        return self._json({
            "site_title": self.site.site_title,
            "docs_dir": str(self.site.docs_dir),
            "host": self.site.host,
            "port": self.site.port,
            "auto_reload": self.site.auto_reload,
            "route_count": len(self.site.router.routes),
            "extensions": [getattr(e, "name", type(e).__name__)
                           for e in self.site._extensions],
        }, is_aiohttp=is_aiohttp)

    def _reload(self, is_aiohttp: bool):
        """POST /api/reload — 触发热重载。"""
        self.site.router.rebuild()
        self.site._rebuild_cache()
        return self._json({
            "ok": True,
            "message": "已重载",
            "route_count": len(self.site.router.routes),
        }, is_aiohttp=is_aiohttp)

    def _upload(self, request, is_aiohttp: bool):
        """POST /api/upload — 上传文档文件。

        Content-Type: multipart/form-data
        字段：
            file: 文件内容
            path: 目标路径（相对于 docs_dir，如 "guide/new.mdw"）
        """
        if is_aiohttp:
            return self._upload_aiohttp(request)
        else:
            return self._upload_stdlib(request)

    async def _upload_aiohttp(self, request):
        from aiohttp import web
        content_type = request.headers.get("Content-Type", "").lower()

        # JSON 格式：{"file": "...", "path": "..."}
        if "multipart" not in content_type:
            try:
                payload = await request.json()
            except Exception:
                return self._json({"error": "Content-Type 需为 multipart/form-data 或 application/json"},
                                  400, is_aiohttp=True)
            file_data = str(payload.get("file", "")).encode("utf-8")
            target_path = str(payload.get("path", ""))
            return self._save_upload(file_data, target_path, True)

        # multipart/form-data 格式
        reader = await request.multipart()
        file_data = None
        target_path = None

        async for field in reader:
            if field.name == "file":
                file_data = await field.read()
            elif field.name == "path":
                target_path = await field.text()

        return self._save_upload(file_data, target_path, True)

    def _upload_stdlib(self, handler):
        import cgi
        import io

        content_type = handler.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            # 尝试 JSON body
            length = int(handler.headers.get("Content-Length", 0))
            raw = handler.rfile.read(length)
            return self._save_upload_json(raw, False)

        # 解析 multipart
        boundary = content_type.split("boundary=")[1].strip()
        raw_body = handler.rfile.read(int(handler.headers["Content-Length"]))
        # 简易 multipart 解析
        parts = raw_body.split(b"--" + boundary.encode())
        file_data = None
        target = None
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            if b'name="file"' in part:
                idx = part.find(b"\r\n\r\n")
                if idx > 0:
                    file_data = part[idx + 4:]
                    if file_data.endswith(b"\r\n"):
                        file_data = file_data[:-2]
            elif b'name="path"' in part:
                idx = part.find(b"\r\n\r\n")
                if idx > 0:
                    target = part[idx + 4:].decode("utf-8", errors="ignore").strip()
        return self._save_upload(file_data, target, False)

    def _save_upload(self, file_data, target_path, is_aiohttp: bool):
        if not file_data:
            return self._json({"error": "缺少文件内容"}, 400, is_aiohttp)
        if not target_path:
            return self._json({"error": "缺少 target 参数"}, 400, is_aiohttp)

        # 安全检查
        docs = Path(self.site.docs_dir).resolve()
        target = (docs / target_path.lstrip("/")).resolve()
        if not str(target).startswith(str(docs)):
            return self._json({"error": "路径越界"}, 403, is_aiohttp)
        if not target.suffix.lower() in (".md", ".mdw", ".txt", ".html"):
            return self._json({"error": "不支持的文件类型"}, 400, is_aiohttp)

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(file_data)

        # 触发热重载
        self.site.router.rebuild()
        self.site._rebuild_cache()

        return self._json({
            "ok": True,
            "path": str(target.relative_to(docs)),
            "size": len(file_data),
        }, is_aiohttp=is_aiohttp)

    def _save_upload_json(self, raw: bytes, is_aiohttp: bool):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return self._json({"error": "无效的 JSON"}, 400, is_aiohttp)
        file_data = payload.get("file", "").encode("utf-8")
        target_path = payload.get("path", "")
        return self._save_upload(file_data, target_path, is_aiohttp)

    def _get_page(self, sub: str, is_aiohttp: bool):
        """GET /api/page/{path} — 获取渲染后的页面 HTML。"""
        url = "/" + sub if not sub.startswith("/") else sub
        route = self.site.router.resolve(url)
        if route is None:
            return self._json({"error": "页面未找到", "path": url}, 404, is_aiohttp)

        try:
            html_content = self.site._render_route(route)
        except Exception as exc:
            return self._json({"error": f"渲染失败: {exc}"}, 500, is_aiohttp)
        return self._json({"path": route.path, "title": route.title, "html": html_content},
                          is_aiohttp=is_aiohttp)

    def _get_source(self, sub: str, is_aiohttp: bool):
        """GET /api/source/{path} — 下载原始文档文件（.md/.mdw，非渲染 HTML）。

        空路径返回首页的源文件。响应为文件字节流 + Content-Disposition 下载头。
        """
        url = "/" + sub if not sub.startswith("/") else sub
        route = self.site.router.resolve(url)
        if route is None or route.source_path is None or not Path(route.source_path).is_file():
            return self._json({"error": "源文件未找到", "path": url}, 404, is_aiohttp)

        source = Path(route.source_path)
        # 目录合成路由（无 index）没有单一源文件
        if source.is_dir():
            return self._json({"error": "该路径是目录，无独立源文件", "path": url},
                              404, is_aiohttp)

        try:
            raw = source.read_bytes()
        except OSError as exc:
            return self._json({"error": f"读取失败: {exc}"}, 500, is_aiohttp)

        content_type = _SOURCE_TYPES.get(source.suffix.lower(), "application/octet-stream")
        filename = source.name
        disposition = f'attachment; filename="{filename}"'

        if is_aiohttp:
            from aiohttp import web
            return web.Response(
                body=raw,
                status=200,
                content_type=content_type,
                charset="utf-8",
                headers={"Content-Disposition": disposition},
            )
        else:
            # stdlib：交给 _serve_api 以原始字节发送
            return {"__raw__": raw, "content_type": content_type,
                    "status": 200, "filename": filename}

    def _delete_page(self, sub: str, is_aiohttp: bool):
        """DELETE /api/page/{path} — 删除文档文件。

        删除后若所在目录为空（无任何文件/子目录），自动连同目录一并删除。
        安全限制：不会删除 docs 根目录本身。
        """
        url = "/" + sub if not sub.startswith("/") else sub
        route = self.site.router.resolve(url)
        if route is None or route.source_path is None:
            return self._json({"error": "页面未找到"}, 404, is_aiohttp)

        path = Path(route.source_path)
        if not path.is_file():
            return self._json({"error": "文件不存在"}, 404, is_aiohttp)
        # 安全检查
        docs = Path(self.site.docs_dir).resolve()
        if not str(path.resolve()).startswith(str(docs)):
            return self._json({"error": "路径越界"}, 403, is_aiohttp)

        parent = path.parent
        path.unlink()

        # 目录清理：逐级向上删除空目录（保留 docs 根目录）
        removed_dirs: list[str] = []
        cur = parent
        while docs in cur.parents:
            try:
                next(cur.iterdir())  # 非空则停止
                break
            except StopIteration:
                cur.rmdir()
                removed_dirs.append(str(cur))
                cur = cur.parent
            except OSError:
                break

        self.site.router.rebuild()
        self.site._rebuild_cache()
        resp = {"ok": True, "deleted": route.path}
        if removed_dirs:
            resp["removed_dirs"] = removed_dirs
        return self._json(resp, is_aiohttp=is_aiohttp)


# ===================================================================
# 模块级便捷函数
# ===================================================================

def register(site: "MDWSite", token: str = DEFAULT_TOKEN) -> None:
    """在 MDWSite 上注册全部 API 路由。"""
    site._api_token = token
    api = APIHandler(site)

    # stdlib 模式：通过 _custom_handlers 注册前缀
    def stdlib_adapter(handler):
        return api.handle_stdlib(handler)

    site._custom_handlers["/api/routes"] = _make_stdlib_wrapper(api, "routes")
    site._custom_handlers["/api/status"] = _make_stdlib_wrapper(api, "status")
    site._custom_handlers["/api/reload"] = _make_stdlib_wrapper(api, "reload")
    site._custom_handlers["/api/upload"] = _make_stdlib_wrapper(api, "upload")

    # 对于带参数的端点，用通配适配
    site._api_handler = api
    logger.info("管理 API 已注册（token=%s）", token[:4] + "***")


def _make_stdlib_wrapper(api: APIHandler, action: str) -> Callable:
    """生成 stdlib handler 适配器。"""
    def wrapper(handler) -> str | None:
        import json as _json
        result = None
        if action == "routes":
            result = api._routes(is_aiohttp=False)
        elif action == "status":
            result = api._status(is_aiohttp=False)
        elif action == "reload":
            result = api._reload(is_aiohttp=False)
        elif action == "upload":
            result = api._upload(handler, is_aiohttp=False)

        if isinstance(result, dict) and "data" in result:
            handler.send_response(result.get("status", 200))
            body = _json.dumps(result["data"], ensure_ascii=False, indent=2).encode("utf-8")
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return None
        elif isinstance(result, str):
            return result
        return None
    return wrapper
