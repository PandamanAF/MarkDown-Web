"""
路由 —— 从文档目录树自动发现路由，并支持通过元数据定制导航结构。

约定：
  - ``docs/`` 是文档根目录。
  - ``docs/index.mdw`` → 路由 ``/``（首页）
  - ``docs/guide/`` → 路由 ``/guide/`` → 查找 ``index.mdw``、``README.mdw``
  - ``docs/guide/start.mdw`` → 路由 ``/guide/start``
  - 名为 ``admin`` 或 ``_admin`` 的文件/文件夹为管理后台保留，自动路由时忽略。
  - 以 ``_``（下划线）开头的文件/文件夹视为片段/资源文件，不会注册为路由。
  - 目录结构天然形成侧边栏层级（嵌套无上限）；亦可借助元数据自由调整。

元数据字段（文件头部 ``--- ... ---``）：
  - ``title``     页面标题（浏览器标签 + 侧边栏默认显示名）
  - ``nav``       侧边栏显示名（例如 ``nav: 快速上手``）
  - ``parent``    挂载到指定父级路由，如 ``parent: /guide``（可跨目录、任意层级）
  - ``order``     同级排序数字，越小越靠前（目录默认 0，文件默认 99）
  - ``hidden``    设为 ``true`` 从侧边栏隐藏（URL 仍可访问）
  - ``icon``      侧边栏前缀图标（任意文字 / emoji）
  - ``collapsed`` 设为 ``true`` 时该分组默认收起
"""

from __future__ import annotations
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from .metadata import parse_front_matter

logger = logging.getLogger("mdw.router")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Route:
    """一个已解析的路由，对应一个 .mdw / .md 文档。"""

    path: str                     # URL 路径，例如 "/guide/start"
    source_path: Path             # 源文件的绝对路径
    title: str = ""               # 页面标题（元数据 title 或文件名推导）
    sort_key: int = 99            # 导航排序用（元数据 order）
    meta: dict = field(default_factory=dict)   # 文件头部元数据
    children: list["Route"] = field(default_factory=list)   # 导航树子节点

    @property
    def is_index(self) -> bool:
        """是否为目录索引路由（路径以 / 结尾）。"""
        return self.path.endswith("/")

    @property
    def nav_title(self) -> str:
        """侧边栏显示名：优先元数据 nav，其次 title。"""
        return self.meta.get("nav") or self.title

    @property
    def nav_icon(self) -> str:
        """侧边栏前缀图标。"""
        return str(self.meta.get("icon") or "")

    @property
    def nav_hidden(self) -> bool:
        """是否从侧边栏隐藏（URL 仍可访问）。"""
        return bool(self.meta.get("hidden", False))

    # --- 导航辅助方法 ------------------------------------------------
    def depth(self) -> int:
        """返回路由的层级深度。"""
        return len(self.path.strip("/").split("/")) if self.path != "/" else 0


# ---------------------------------------------------------------------------
# 保留关键字（可在 Site 配置中覆盖）
# ---------------------------------------------------------------------------

RESERVED = frozenset({"admin", "_admin"})


class Router:
    """扫描文档目录并构建路由树。"""

    def __init__(
        self,
        docs_dir: str | Path,
        index_names: tuple[str, ...] = ("index", "README"),
        extensions: tuple[str, ...] = (".mdw", ".md", ".markdown"),
        reserved: frozenset[str] = RESERVED,
    ):
        """
        参数：
            docs_dir: 文档根目录
            index_names: 被视为首页的文件名（不含后缀）
            extensions: 识别的文档扩展名
            reserved: 保留的文件/文件夹名，自动路由时跳过
        """
        self._docs_dir = Path(docs_dir).resolve()
        self._index_names = index_names
        self._extensions = extensions
        self._reserved = reserved
        self._routes: list[Route] = []            # 平铺路由列表
        self._flat: dict[str, Route] = {}         # 路径 → Route 快速查找
        self._tree: list[Route] = []              # 导航树顶层
        self._dir_index: dict[Path, Route] = {}   # 目录 → 该目录的路由

    # -- 公开 API ----------------------------------------------------------

    @property
    def routes(self) -> list[Route]:
        """返回所有已注册的路由（平铺列表）。"""
        return self._routes

    def nav_roots(self) -> list[Route]:
        """返回导航树顶层路由列表。"""
        return self._tree

    def resolve(self, url_path: str) -> Optional[Route]:
        """
        根据 URL 路径查找对应的 Route。
        例如 ``/guide/start`` 或 ``/guide``。
        """
        # 规范化：确保以 / 开头、去掉多余的 /
        url_path = "/" + url_path.strip("/")
        # 优先精确匹配
        hit = self._flat.get(url_path)
        if hit is not None:
            return hit
        # 尝试带尾部斜杠（目录索引）
        hit = self._flat.get(url_path + "/")
        if hit is not None:
            return hit
        # 尝试去掉尾部斜杠
        if url_path.endswith("/"):
            return self._flat.get(url_path.rstrip("/"))
        return None

    def rebuild(self) -> None:
        """完整扫描 —— 启动时和热重载后调用。"""
        self._routes.clear()
        self._flat.clear()
        self._tree.clear()
        self._dir_index.clear()
        self._scan(self._docs_dir)
        self._build_tree()
        logger.info("路由扫描完成 —— %d 个路由", len(self._routes))

    # -- 扫描 ----------------------------------------------------------

    def _scan(self, dir_path: Path) -> None:
        """递归遍历目录：注册文件路由，并为目录登记索引路由。"""
        if not dir_path.is_dir():
            return

        index_route: Optional[Route] = None
        child_dirs: list[Path] = []

        for entry in sorted(dir_path.iterdir()):
            stem_lower = entry.stem.lower()

            # --- 跳过保留名 / 部分文件 -------------------------------------
            if stem_lower in self._reserved:
                continue
            if stem_lower.startswith("_"):
                continue

            if entry.is_dir():
                child_dirs.append(entry)
            elif entry.is_file() and entry.suffix.lower() in self._extensions:
                route = self._make_route(entry)
                self._register(route)
                if self._is_index_file(entry):
                    index_route = route

        for child_dir in child_dirs:
            self._scan(child_dir)

        # 目录自身的索引路由：index 文件，或合成索引（无 index 时）
        if index_route is None:
            index_route = self._make_dir_route(dir_path)
        if index_route is not None:
            self._dir_index[dir_path] = index_route

    def _make_route(self, file: Path) -> Route:
        """根据文件路径与头部元数据创建 Route 对象。"""
        url = self._url_path(file)
        meta, _ = self._read_meta(file)
        title = meta.get("title") or self._humanize(file.stem)
        sort_key = self._sort_key(meta.get("order"), is_index=self._is_index_file(file))
        return Route(path=url, source_path=file, title=title, sort_key=sort_key, meta=meta)

    def _make_dir_route(self, dir_path: Path) -> Optional[Route]:
        """为没有 index 文件的目录创建合成索引路由（可浏览、可导航）。"""
        url = self._url_path(dir_path, is_dir=True)
        route = Route(
            path=url,
            source_path=dir_path,
            title=self._humanize(dir_path.name),
            sort_key=0,
        )
        self._register(route)
        return route

    @staticmethod
    def _read_meta(path: Path):
        """读取文件头部元数据（只需文件开头，避免读入大文件）。"""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                head = fh.read(8192)
        except OSError:
            return {}, None
        return parse_front_matter(head)

    @staticmethod
    def _sort_key(order, is_index: bool) -> int:
        """计算导航排序键：元数据 order 优先，否则目录 0 / 文件 99。"""
        if order is not None:
            try:
                return int(order)
            except (TypeError, ValueError):
                pass
        return 0 if is_index else 99

    @staticmethod
    def _humanize(stem: str) -> str:
        """将文件名转换为可读标题。"""
        return stem.replace("-", " ").replace("_", " ").title()

    def _url_path(self, path: Path, is_dir: bool = False) -> str:
        """将文件/目录路径转换为 URL 路径。"""
        try:
            relative = path.relative_to(self._docs_dir)
        except ValueError:
            return "/"

        # 路径等于文档根目录时 relative 为 '.'，其 stem 为空，
        # with_suffix('') 在 Windows 上会抛出 ValueError。
        if relative == Path("."):
            return "/"

        try:
            url_path = relative.with_suffix("")
        except ValueError:
            url_path = relative

        parts = url_path.parts
        if not parts:
            return "/"
        if not is_dir and self._is_index_file(path):
            if len(parts) == 1:
                return "/"
            return "/" + "/".join(parts[:-1]) + "/"
        base = "/" + "/".join(parts)
        return base + "/" if is_dir else base

    @staticmethod
    def _is_index_file(path: Path) -> bool:
        """判断文件是否为首页文件（index / readme）。"""
        return path.stem.lower() in {"index", "readme"}

    def _register(self, route: Route) -> None:
        """将路由添加到平铺列表和快速查找字典中。"""
        self._routes.append(route)
        self._flat[route.path] = route

    # -- 导航树构建 ----------------------------------------------------

    def _build_tree(self) -> None:
        """
        依据元数据（parent / order / hidden）将平铺路由组装为导航树。

        默认规则：
          - 目录索引路由是其目录内文件的父节点，目录层级自然形成嵌套（无上限）；
          - ``parent`` 元数据可把任意路由挂到指定父级下（跨目录、跨层级）；
          - ``hidden`` 路由不进入导航树，但其可见子节点会向上浮动。
        """
        parent_of: dict[str, Optional[Route]] = {}
        for r in self._routes:
            if r.nav_hidden:
                continue  # 隐藏路由不进入导航树（URL 仍可访问）
            parent_of[r.path] = self._resolve_parent(r)

        # 组装 children
        for path, parent in parent_of.items():
            route = self._flat[path]
            if parent is not None:
                parent.children.append(route)
            else:
                self._tree.append(route)

        # 同级排序：order 优先，其次显示名
        for r in self._routes:
            r.children.sort(key=lambda c: (c.sort_key, c.nav_title.lower()))

        # 环保护：DFS 时发现子节点已在当前路径则断开该边（元数据配置错误兜底）
        def _prune(route: Route, stack: set) -> None:
            stack.add(route.path)
            kept = []
            for child in route.children:
                if child.path in stack:
                    logger.warning("导航环检测：断开 %s → %s", route.path, child.path)
                    continue
                kept.append(child)
                _prune(child, stack)
            route.children = kept
            stack.discard(route.path)

        for root in list(self._tree):
            _prune(root, set())

    def _resolve_parent(self, r: Route) -> Optional[Route]:
        """计算路由的导航父级：元数据 parent 优先，其次目录结构。"""
        target: Optional[Route] = None
        p = r.meta.get("parent")
        if p:
            p = str(p)
            cand = self._flat.get(p) or self._flat.get(p.rstrip("/") + "/")
            if (
                cand is not None
                and cand is not r
                and not self._is_descendant_of(cand, r)
            ):
                target = cand
        if target is None:
            target = self._natural_parent(r)
        # 透明隐藏：父级被隐藏时，子节点向上浮动
        while target is not None and target.nav_hidden:
            target = self._natural_parent(target)
        return target

    def _natural_parent(self, r: Route) -> Optional[Route]:
        """目录结构决定的自然父级。"""
        src = r.source_path
        if src.is_file():
            if r.path.endswith("/"):
                # 目录索引页 → 上级目录的索引路由
                return self._dir_index.get(src.parent.parent)
            return self._dir_index.get(src.parent)
        # 合成目录路由 → 上级目录的索引路由
        return self._dir_index.get(src.parent)

    @staticmethod
    def _is_descendant_of(cand: Route, r: Route) -> bool:
        """cand 是否为 r 自身或其后代（用于阻止把 r 挂到自己的子树下）。"""
        if cand.path == r.path:
            return True
        prefix = r.path.rstrip("/") + "/"
        return cand.path.startswith(prefix)
