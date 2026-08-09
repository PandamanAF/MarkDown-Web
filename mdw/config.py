"""
初始化与配置管理。

职责：
    - 首次运行时自动生成 ``config/`` 文件夹及默认 ``site.yaml``
    - 检查并自动创建必须的目录（docs/、static/ 等）
    - 配置文件缺失时告警并重新生成
    - CLI 参数 > 配置文件 > 内置默认值 三级优先级

用法：::

    from mdw.config import Config
    cfg = Config.load()
    cfg.ensure_dirs()
    site = MDWSite.from_config(cfg)
"""

from __future__ import annotations
import argparse
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("mdw.config")

# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------

_DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8080,
    "docs_dir": "docs",
    "static_dir": "static",
    "template_dir": None,
    "config_dir": "config",
    "site_title": "MDW",
    "brand": "MDW",
    "admin_prefix": "/admin",
    "auto_reload": True,
    "debug": False,
    "variables": {},
    "extensions": {
        "front_matter": True,
        "code_highlight": True,
        "custom_blocks": {
            "enabled": True,
            "markers": [":::", "[["],
            "styles": {
                "note":      {"class": "mdw-block mdw-note"},
                "warning":   {"class": "mdw-block mdw-warning"},
                "tip":       {"class": "mdw-block mdw-tip"},
                "important": {"class": "mdw-block mdw-important", "css": "important.css"},
                "card": {
                    "html": "card.html",
                    "vars": {"title": "📇 外部 HTML 模板", "accent": "#6750a4", "radius": "12px"},
                },
                "page-card": {
                    "html": "page-card.html",
                    "css": "page-card.css",
                    "vars": {"title": "前往", "url": "/", "icon": "", "accent": "#6750a4", "align": "right", "compact": "1"},
                },
            },
        },
    },
    "api": {
        "enabled": True,
        "token": "mdw-admin",
    },
}


# ===================================================================
# Config
# ===================================================================

@dataclass
class Config:
    """站点配置数据类。所有字段均可序列化为 YAML。"""

    # 网络
    host: str = _DEFAULTS["host"]
    port: int = _DEFAULTS["port"]

    # 目录
    docs_dir: str = _DEFAULTS["docs_dir"]
    static_dir: str = _DEFAULTS["static_dir"]
    template_dir: str | None = _DEFAULTS["template_dir"]
    config_dir: str = _DEFAULTS["config_dir"]

    # 站点
    site_title: str = _DEFAULTS["site_title"]
    brand: str = _DEFAULTS["brand"]
    admin_prefix: str = _DEFAULTS["admin_prefix"]

    # 行为
    auto_reload: bool = _DEFAULTS["auto_reload"]
    debug: bool = _DEFAULTS["debug"]

    # 注入变量
    variables: dict[str, str] = field(default_factory=dict)

    # 扩展
    extensions: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULTS["extensions"]))

    # 管理 API
    api: dict[str, Any] = field(default_factory=lambda: dict(_DEFAULTS["api"]))

    # 运行时元信息（不写入配置文件）
    _loaded_from: str | None = field(default=None, repr=False)
    _cli_overrides: set[str] = field(default_factory=set, repr=False)

    # ── 工厂方法 ──────────────────────────────────────────────

    @classmethod
    def load(
        cls,
        cli_args: argparse.Namespace | None = None,
        config_path: str | Path | None = None,
    ) -> "Config":
        """加载配置：默认值 → YAML 文件 → CLI 参数，逐层覆盖。

        如果配置文件不存在，自动生成默认配置。
        如果必须的目录缺失，记录告警。
        """
        cfg = cls()

        # 1. 确定配置目录和文件路径（基于应用根目录）
        app_root = cls._app_root()
        config_dir = cfg.config_dir
        if config_path:
            config_file = Path(config_path)
        else:
            config_file = app_root / config_dir / "site.yaml"
        if not config_file.is_absolute():
            config_file = app_root / config_file

        # 2. 从 YAML 加载（若存在）
        if config_file.exists():
            try:
                cfg._load_yaml(config_file)
                cfg._loaded_from = str(config_file)
                logger.info("已加载配置: %s", config_file)
            except Exception as exc:
                logger.warning("配置文件损坏 (%s)，回退到默认值", exc)
        else:
            # 首次运行：生成默认配置
            cfg._ensure_config_dir(config_file)
            cfg._save_yaml(config_file)
            cfg._loaded_from = str(config_file)
            logger.info("已生成默认配置: %s", config_file)

        # 3. CLI 参数覆盖
        if cli_args:
            cfg._apply_cli(cli_args)

        # 4. 路径解析：相对路径基于可执行文件所在目录（打包兼容）
        cfg._resolve_paths()

        return cfg

    @classmethod
    def from_defaults(cls) -> "Config":
        """创建纯默认配置实例（不读文件）。"""
        return cls()

    # ── 路径解析 ──────────────────────────────────────────────

    @staticmethod
    def _app_root() -> Path:
        """获取应用根目录。

        打包后指 exe 所在目录；开发时指当前工作目录。
        """
        # PyInstaller / Nuitka 打包后 sys.frozen 为 True
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path.cwd()

    def _resolve_paths(self) -> None:
        """将相对目录路径解析为基于应用根目录的绝对路径。"""
        root = self._app_root()

        def _resolve(attr: str) -> None:
            val = getattr(self, attr, None)
            if val is None:
                return
            p = Path(val)
            if not p.is_absolute():
                setattr(self, attr, str(root / p))

        for attr in ("docs_dir", "config_dir"):
            _resolve(attr)
        # static_dir / template_dir 不解析为绝对路径 ——
        # 它们由 site.py 中的 _find_resource_dir() 在运行时查找，
        # 支持打包后的内部资源回退。

    # ── 目录保障 ──────────────────────────────────────────────

    def ensure_dirs(self) -> list[str]:
        """检查并创建所有必须的目录。返回已创建/存在的目录列表。

        缺失目录只告警并自动创建，不会阻止启动。
        """
        required = [
            self.docs_dir,
            self.static_dir,
        ]
        if self.template_dir:
            required.append(self.template_dir)

        created: list[str] = []
        for d in required:
            p = Path(d)
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                logger.warning("目录缺失已自动创建: %s", p)
                created.append(str(p))
            elif not p.is_dir():
                logger.error("路径存在但不是目录: %s", p)
        return created

    # ── 内部方法 ──────────────────────────────────────────────

    def _ensure_config_dir(self, config_file: Path) -> None:
        """确保配置文件父目录存在。"""
        config_file.parent.mkdir(parents=True, exist_ok=True)

    def _load_yaml(self, path: Path) -> None:
        """从 YAML 文件加载配置项，逐字段覆盖默认值。"""
        try:
            import yaml
        except ImportError:
            logger.warning("PyYAML 未安装，使用内置简易解析器（仅支持平面键值对）")
            data = self._load_yaml_builtin(path)
        else:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

        self._merge_dict(data)

    def _load_yaml_builtin(self, path: Path) -> dict:
        """内置最小 YAML 解析器（无需 PyYAML，仅支持 string/int/bool/list/dict）。"""
        import re
        data: dict = {}
        current_key: str | None = None

        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                m = re.match(r"^(\w[\w_-]*)\s*:\s*(.*?)\s*$", stripped)
                if m:
                    current_key = m.group(1)
                    val = m.group(2).strip()
                    if val in ("true", "True", "yes"):
                        data[current_key] = True
                    elif val in ("false", "False", "no"):
                        data[current_key] = False
                    elif val == "null" or val == "~":
                        data[current_key] = None
                    elif re.match(r"^-?\d+$", val):
                        data[current_key] = int(val)
                    elif re.match(r"^-?\d+\.\d+$", val):
                        data[current_key] = float(val)
                    else:
                        data[current_key] = val.strip("'\"")
        return {"extensions": data} if current_key else {}

    def _save_yaml(self, path: Path) -> None:
        """将当前配置写入 YAML 文件。优先用 PyYAML，否则用内置生成器。"""
        try:
            import yaml
        except ImportError:
            content = self._to_yaml_builtin()
        else:
            data = {k: v for k, v in asdict(self).items()
                    if v is not None and not k.startswith("_")}
            data.pop("extensions", None)  # 扩展配置单独处理
            data["extensions"] = self._build_extensions_yaml()
            content = yaml.dump(data, default_flow_style=False, allow_unicode=True,
                                sort_keys=False)

        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def _to_yaml_builtin(self) -> str:
        """内置 YAML 生成器。"""
        lines = [
            "# MDW 站点配置文件",
            f"# 生成时间：自动",
            "",
            f"host: {self.host}",
            f"port: {self.port}",
            "",
            f"docs_dir: {self.docs_dir}",
            f"static_dir: {self.static_dir}",
        ]
        if self.template_dir:
            lines.append(f"template_dir: {self.template_dir}")
        lines += [
            "",
            f"site_title: {self.site_title}",
            f"brand: {self.brand}",
            f"admin_prefix: {self.admin_prefix}",
            "",
            f"auto_reload: {str(self.auto_reload).lower()}",
            f"debug: {str(self.debug).lower()}",
            "",
        ]
        # 变量
        if self.variables:
            lines.append("variables:")
            for k, v in self.variables.items():
                lines.append(f"  {k}: {v!r}")
            lines.append("")
        # 扩展（简化）
        lines += [
            "extensions:",
            "  front_matter: true",
            "  code_highlight: true",
            "  custom_blocks:",
            "    enabled: true",
            "    markers:",
            "      - :::",
            "      - [[[",
            "    # styles 配置见 app.py CustomBlockExtension",
            "",
        ]
        return "\n".join(lines) + "\n"

    def _build_extensions_yaml(self) -> dict:
        """构建扩展配置的 YAML 友好结构。"""
        return self.extensions

    def _merge_dict(self, data: dict) -> None:
        """将 dict 中的键值合并到实例字段。"""
        # 顶层字段
        for key in ("host", "port", "docs_dir", "static_dir", "template_dir",
                     "site_title", "brand", "admin_prefix", "auto_reload", "debug",
                     "variables", "extensions", "api"):
            if key in data:
                val = data[key]
                if key == "port":
                    val = int(val)
                elif key == "auto_reload":
                    val = bool(val)
                elif key == "debug":
                    val = bool(val)
                elif key == "api" and isinstance(val, dict):
                    # 合并而不是覆盖，保留默认字段
                    merged = dict(_DEFAULTS["api"])
                    merged.update({k: v for k, v in val.items() if v is not None})
                    val = merged
                setattr(self, key, val)
        # config_dir 特殊处理（来自 template_dir 等）
        if "config_dir" in data:
            self.config_dir = str(data["config_dir"])

    def _apply_cli(self, args: argparse.Namespace) -> None:
        """CLI 参数覆盖配置。仅当用户显式传参时才覆盖。"""
        # 字符串/number 类型：默认 None，用户传了才覆盖
        if args.host is not None:
            self.host = args.host
            self._cli_overrides.add("host")
        if args.port is not None:
            self.port = args.port
            self._cli_overrides.add("port")
        if args.docs is not None:
            self.docs_dir = args.docs
            self._cli_overrides.add("docs_dir")
        if args.admin is not None:
            self.admin_prefix = args.admin
            self._cli_overrides.add("admin_prefix")

        # boolean 类型 flag：传了即为 True
        if getattr(args, "debug", False):
            self.debug = True
            self._cli_overrides.add("debug")
        if getattr(args, "no_reload", False):
            self.auto_reload = False
            self._cli_overrides.add("auto_reload")
