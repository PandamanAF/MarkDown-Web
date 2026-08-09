"""
模板引擎 —— 极简的 ``{{ 变量 }}`` 替换与模板集加载。

设计目标：
  - 无第三方依赖，纯标准库
  - 页面外壳（head / 导航 / 页脚 / 目录列表 / 错误页等）全部以外部
    HTML 文件形式存在，代码只负责组装上下文并引用渲染
  - 支持用户覆盖：``MDWSite(template_dir="templates")`` 指向的目录
    中若存在同名模板文件，则优先使用；否则回退到内置模板
    （``mdw/templates/``），实现不改代码即可定制页面外观

用法：::

    ts = TemplateSet(user_dir="templates", builtin_dir=Path("mdw/templates"))
    html = ts.render("base.html", {"title": "首页", "content": "<h1>...</h1>"})
"""

from __future__ import annotations
import re
from pathlib import Path

# 匹配 {{ 变量名 }}（变量名支持字母/数字/下划线/连字符）
_TOKEN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def render(text: str, ctx: dict) -> str:
    """将模板文本中的 ``{{ name }}`` 替换为上下文中的值（缺失为空串）。

    替换过程不会递归扫描已插入的值，因此值中即使包含 ``{{`` 也不会
    被二次处理。
    """
    return _TOKEN.sub(lambda m: str(ctx.get(m.group(1), "")), text)


class TemplateSet:
    """一组外部 HTML 模板，支持用户目录覆盖内置模板。"""

    def __init__(
        self,
        user_dir: str | Path = "",
        builtin_dir: str | Path | None = None,
    ):
        """
        参数：
            user_dir: 用户模板目录（可选）。存在同名文件时优先使用
            builtin_dir: 内置模板目录（默认 ``mdw/templates/``）
        """
        self._user_dir = Path(user_dir).resolve() if user_dir else None
        self._builtin_dir = (
            Path(builtin_dir).resolve()
            if builtin_dir
            else Path(__file__).resolve().parent / "templates"
        )
        # 模板缓存：路径 → (mtime, 文本)，文件变更后自动失效
        self._cache: dict[str, tuple[float, str]] = {}

    # -- 公开 API -----------------------------------------------------

    def names(self) -> list[str]:
        """列出可用的模板名（内置 + 用户覆盖）。"""
        names = {p.name for p in self._builtin_dir.glob("*.html")}
        if self._user_dir and self._user_dir.is_dir():
            names |= {p.name for p in self._user_dir.glob("*.html")}
        return sorted(names)

    def get(self, name: str) -> str:
        """读取模板文本（带 mtime 失效缓存）。"""
        path = self._resolve(name)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        hit = self._cache.get(str(path))
        if hit is not None and abs(hit[0] - mtime) < 0.001:
            return hit[1]
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        self._cache[str(path)] = (mtime, text)
        return text

    def render(self, name: str, ctx: dict) -> str:
        """读取并渲染一个模板。"""
        return render(self.get(name), ctx)

    # -- 内部方法 -----------------------------------------------------

    def _resolve(self, name: str) -> Path:
        """确定模板文件的最终路径（用户目录优先，内置兜底）。"""
        if self._user_dir:
            p = self._user_dir / name
            if p.is_file():
                return p
        return self._builtin_dir / name
