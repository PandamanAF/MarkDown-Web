"""
管理后台 Web 界面 —— 内置管理仪表盘。

默认挂载在 ``/admin``。提供：
  - 文件树浏览
  - 源代码实时查看
  - 状态 / 健康概览
  - 手动重载触发器
"""

from __future__ import annotations
import html
from pathlib import Path
from typing import Any, Optional

# 管理后台 HTML 模板（使用双花括号转义 Python format）
_ADMIN_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — MDW Admin</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
          background: #f5f5f5; color: #333; }}
  .sidebar {{ position: fixed; left: 0; top: 0; bottom: 0; width: 280px;
              background: #1a1a2e; color: #eee; overflow-y: auto; padding: 16px; }}
  .sidebar h2 {{ font-size: 16px; margin-bottom: 12px; color: #a0a0ff; }}
  .sidebar a {{ display: block; color: #ccc; text-decoration: none; padding: 4px 8px;
               border-radius: 4px; font-size: 14px; }}
  .sidebar a:hover {{ background: #2a2a4e; color: #fff; }}
  .main {{ margin-left: 280px; padding: 24px 32px; }}
  .header {{ border-bottom: 1px solid #ddd; padding-bottom: 12px; margin-bottom: 24px; }}
  .header h1 {{ font-size: 22px; }}
  .card {{ background: #fff; border-radius: 6px; padding: 16px 20px; margin-bottom: 16px;
           box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .card h3 {{ font-size: 14px; color: #888; margin-bottom: 8px; text-transform: uppercase; }}
  pre {{ background: #272822; color: #f8f8f2; padding: 16px; border-radius: 6px;
         overflow-x: auto; font-size: 13px; }}
  button {{ background: #4a6cf7; color: #fff; border: none; padding: 8px 20px;
            border-radius: 4px; cursor: pointer; font-size: 14px; }}
  button:hover {{ background: #3b5de7; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px;
            font-size: 12px; font-weight: 600; }}
  .badge.ok {{ background: #d4edda; color: #155724; }}
  .badge.info {{ background: #d1ecf1; color: #0c5460; }}
</style>
</head>
<body>
<div class="sidebar">
  <h2>🔧 MDW Admin</h2>
  <nav>
    <a href="/admin">📊 Dashboard</a>
    <a href="/admin/files">📁 文件树</a>
    <a href="/admin/reload">🔄 重载</a>
  </nav>
  <hr style="border-color:#333;margin:16px 0;">
  <div style="font-size:12px;color:#888;">
    <p>路由数: <strong>{route_count}</strong></p>
    <p>运行时间: {uptime}</p>
  </div>
</div>
<div class="main">
  <div class="header"><h1>{title}</h1></div>
  {content}
</div>
</body>
</html>
"""


class AdminPage:
    """从路由、文件和状态数据生成管理后台 HTML 页面。"""

    def __init__(self, site_ref: Any):
        """
        参数：
            site_ref: MDWSite 实例引用
        """
        self._site = site_ref

    def dashboard(self) -> str:
        """生成仪表盘页面，显示系统状态和路由表。"""
        site = self._site
        routes = site.router.routes
        route_list = "".join(
            f'<tr><td>{html.escape(r.path)}</td><td>{html.escape(r.title)}</td></tr>'
            for r in routes
        )
        content = f"""
        <div class="card">
          <h3>📊 系统状态</h3>
          <table style="width:100%;border-collapse:collapse;">
            <tr><td style="padding:6px 0;">服务状态</td><td><span class="badge ok">运行中</span></td></tr>
            <tr><td style="padding:6px 0;">文档目录</td><td>{site.docs_dir}</td></tr>
            <tr><td style="padding:6px 0;">文档数</td><td><strong>{len(routes)}</strong></td></tr>
            <tr><td style="padding:6px 0;">热重载</td><td><span class="badge info">开启</span></td></tr>
          </table>
        </div>
        <div class="card">
          <h3>🗺️ 路由表</h3>
          <table style="width:100%;border-collapse:collapse;">
            <tr style="border-bottom:1px solid #eee;"><th style="text-align:left;padding:6px 0;">路径</th><th style="text-align:left;">标题</th></tr>
            {route_list}
          </table>
        </div>
        """
        return _ADMIN_TEMPLATE.format(
            title="仪表盘",
            content=content,
            route_count=len(routes),
            uptime="N/A",
        )

    def file_tree(self) -> str:
        """生成文件树页面，列出文档目录下的所有文档文件。"""
        docs_dir = Path(self._site.docs_dir)
        files = sorted(docs_dir.rglob("*"))
        entries = []
        for f in files:
            if f.is_file() and f.suffix.lower() in (".mdw", ".md", ".markdown"):
                rel = f.relative_to(docs_dir)
                url = f"/admin/view/{rel.as_posix()}"
                entries.append(
                    f'<li><a href="{url}">📄 {html.escape(rel.as_posix())}</a></li>'
                )
        content = (
            '<div class="card"><h3>📁 文档文件</h3>'
            '<ul style="list-style:none;padding:0;">'
            + "".join(entries)
            + "</ul></div>"
        )
        return _ADMIN_TEMPLATE.format(
            title="文件树",
            content=content,
            route_count=len(self._site.router.routes),
            uptime="N/A",
        )

    def view_source(self, rel_path: str) -> str:
        """显示指定文档文件的原始源代码（含路径越界防护）。"""
        docs_dir = Path(self._site.docs_dir).resolve()
        full_path = (docs_dir / rel_path).resolve()
        # 路径越界防护：禁止通过 .. 访问 docs 目录以外的文件
        if not full_path.is_relative_to(docs_dir):
            return self._error(f"禁止访问: {rel_path}")
        if not full_path.is_file():
            return self._error(f"文件未找到: {rel_path}")
        try:
            source = full_path.read_text(encoding="utf-8")
        except OSError:
            return self._error(f"文件未找到: {rel_path}")
        escaped = html.escape(source)
        content = f"""
        <div class="card">
          <h3>📄 {html.escape(rel_path)}</h3>
          <pre>{escaped}</pre>
        </div>
        """
        return _ADMIN_TEMPLATE.format(
            title=rel_path,
            content=content,
            route_count=len(self._site.router.routes),
            uptime="N/A",
        )

    def reload_page(self, message: str = "") -> str:
        """生成重载页面，包含手动触发重载的按钮。"""
        msg = f'<p style="color:#155724;">✅ {message}</p>' if message else ""
        content = f"""
        <div class="card">
          <h3>🔄 重新加载文档</h3>
          {msg}
          <form method="post" action="/admin/reload">
            <button type="submit">立即重载</button>
          </form>
        </div>
        """
        return _ADMIN_TEMPLATE.format(
            title="重载",
            content=content,
            route_count=len(self._site.router.routes),
            uptime="N/A",
        )

    @staticmethod
    def _error(msg: str) -> str:
        """生成错误页面。"""
        return f"<html><body><h1>错误</h1><p>{html.escape(msg)}</p></body></html>"

    @staticmethod
    def not_found() -> str:
        """生成 404 页面。"""
        return "<html><body><h1>404</h1><p>管理页面未找到。</p></body></html>"
