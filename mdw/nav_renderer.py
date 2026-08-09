"""
导航渲染器 —— 从路由树生成侧边栏导航 HTML。

职责：
    - 递归渲染任意层级嵌套的导航树
    - 折叠/展开动画（grid 0fr→1fr 过渡）
    - 高亮当前页面路径
    - 生成目录列表页（无独立文档的目录兜底页面）
"""

from __future__ import annotations
import html
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .router import Route
    from .templating import TemplateSet


class NavRenderer:
    """根据路由树与模板集生成侧边栏导航 HTML。"""

    def __init__(self, templates: "TemplateSet"):
        """
        参数：
            templates: 模板集实例（用于渲染 nav_item.html / dir_listing.html）
        """
        self._templates = templates

    def render_nav(self, roots: list["Route"], current: "Route") -> str:
        """从导航根节点列表生成完整侧边栏导航。"""
        return self._render_level(roots, current)

    def render_dir_listing(self, route: "Route") -> str:
        """为没有独立文档的目录生成子页面列表。"""
        items = []
        for child in route.children:
            if child.nav_hidden:
                continue
            icon = child.nav_icon or ("📁" if child.children else "📄")
            items.append(
                f'<li><a href="{html.escape(child.path)}">'
                f'<span class="mdw-dir-icon">{icon}</span>'
                f"<span>{html.escape(child.nav_title)}</span></a></li>"
            )
        return self._templates.render("dir_listing.html", {
            "title": html.escape(route.nav_title),
            "items": "\n".join(items),
        })

    # -- 内部方法 -----------------------------------------------------

    def _render_level(self, routes: list["Route"], current: "Route") -> str:
        items = [self._render_item(r, current) for r in routes]
        return '<ul class="mdw-nav-tree">' + "".join(items) + "</ul>"

    def _render_item(self, r: "Route", current: "Route") -> str:
        """渲染单个导航节点（模板驱动，含子节点递归）。"""
        sub = ""
        tree_wrap = ""
        toggle = ""
        extra_cls = ""
        data_attr = ""
        expanded = True

        if r.children:
            sub = self._render_level(r.children, current)
            # 默认收起：元数据 collapsed 且当前页不在该分组下
            if r.meta.get("collapsed") and not self._is_ancestor(r, current):
                extra_cls = " collapsed"
                expanded = False
            # 折叠动画包裹层：grid 0fr/1fr 过渡
            tree_wrap = (
                '<div class="mdw-nav-tree-wrap">'
                '<div class="mdw-nav-tree-inner">' + sub + "</div></div>"
            )
            toggle = (
                '<button type="button" class="mdw-nav-toggle" '
                f'aria-label="展开或收起分组" aria-expanded="{"true" if expanded else "false"}"></button>'
            )
            data_attr = f' data-path="{html.escape(r.path)}"'

        active = ' active' if r.path == current.path else ""
        icon = f'<span class="mdw-nav-icon">{html.escape(r.nav_icon)}</span>' if r.nav_icon else ""

        if r.source_path and r.source_path.is_file():
            label = (
                f'<a class="mdw-nav-link{active}" href="{html.escape(r.path)}">'
                f"{icon}<span>{html.escape(r.nav_title)}</span></a>"
            )
        else:
            # 无独立文档的目录节点：整行可点击（点击切换折叠）
            label = (
                f'<span class="mdw-nav-dir{active}" role="button" tabindex="0">'
                f"{icon}<span>{html.escape(r.nav_title)}</span></span>"
            )

        return self._templates.render("nav_item.html", {
            "kind": "group" if r.children else "leaf",
            "extra_cls": extra_cls,
            "data_attr": data_attr,
            "label": label,
            "toggle": toggle,
            "tree_wrap": tree_wrap,
        })

    @staticmethod
    def _is_ancestor(node: "Route", current: "Route") -> bool:
        """node 是否为当前页面的祖先（或自身），用于展开当前路径。"""
        if current.path == node.path:
            return True
        prefix = node.path.rstrip("/") + "/"
        return current.path.startswith(prefix)
