"""
MDW —— 文档建站框架启动入口

用法：
    python app.py                       # 默认运行（首次启动自动生成 config/site.yaml）
    python app.py --port 9090           # CLI 参数覆盖配置文件
    python app.py --config mycfg.yaml   # 使用指定配置文件
    python app.py --debug               # 调试模式
"""

import argparse
import logging
import sys

from mdw import MDWSite
from mdw.config import Config
from mdw.extensions import (
    FrontMatterExtension,
    CustomBlockExtension,
    CodeHighlightExtension,
)
from mdw.site import ensure_bundle_extracted


def main():
    """主函数：解压内置资源 → 加载配置 → 创建站点 → 启动服务。"""
    # ── 首次运行：解压内置的 docs/static/config 到 exe 同目录 ──
    ensure_bundle_extracted()

    # ── 解析 CLI 参数 ──────────────────────────────────────────
    parser = argparse.ArgumentParser(description="MDW 文档建站框架")
    parser.add_argument("--host", default=None, help="监听地址（覆盖配置文件）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（覆盖配置文件）")
    parser.add_argument("--docs", default=None, help="文档目录（覆盖配置文件）")
    parser.add_argument("--admin", default=None, help="管理后台前缀（覆盖配置文件）")
    parser.add_argument("--config", default=None, help="指定配置文件路径")
    parser.add_argument("--debug", action="store_true", help="调试模式")
    parser.add_argument("--no-reload", action="store_true", help="禁用热重载")
    cli_args = parser.parse_args()

    # ── 初始化配置 ────────────────────────────────────────────
    # 首次运行自动生成 config/site.yaml 及必须目录
    cfg = Config.load(cli_args=cli_args, config_path=cli_args.config)
    cfg.ensure_dirs()

    # ── 创建站点实例 ──────────────────────────────────────────
    site = MDWSite(
        docs_dir=cfg.docs_dir,
        host=cfg.host,
        port=cfg.port,
        site_title=cfg.site_title,
        admin_prefix=cfg.admin_prefix,
        auto_reload=cfg.auto_reload,
        variables=cfg.variables,
        template_dir=cfg.template_dir,
    )

    # ── 注册扩展（从配置驱动） ─────────────────────────────────
    site.use(FrontMatterExtension())
    site.use(CodeHighlightExtension())

    # 自定义样式块
    block_cfg = {}
    if isinstance(cfg.extensions, dict):
        block_cfg = cfg.extensions.get("custom_blocks", {})
    if isinstance(block_cfg, dict) and block_cfg.get("enabled", True):
        markers = tuple(block_cfg.get("markers", (":::", "[[[")))
        styles = block_cfg.get("styles", {})
        site.use(CustomBlockExtension(markers=markers, styles=styles))

    # ── 注册管理 API ─────────────────────────────────────────
    api_cfg = cfg.api if isinstance(cfg.api, dict) else {}
    if api_cfg.get("enabled", True):
        site.register_api(token=api_cfg.get("token", "mdw-admin"))

    # ── 打印启动信息 ──────────────────────────────────────────
    print(f"""
 ╔═══════════════════════════════════════╗
 ║   MDW — Markdown Website Framework    ║
 ╚═══════════════════════════════════════╝
    › 站点标题 : {cfg.site_title}
    › 文档目录 : {cfg.docs_dir}/
    › 配置文件 : {cfg._loaded_from or '默认'}
    › 访问地址 : http://{cfg.host}:{cfg.port}
    › 管理后台 : http://{cfg.host}:{cfg.port}{cfg.admin_prefix}
    › 热重载   : {"开启" if cfg.auto_reload else "关闭"}
    """)

    try:
        site.run(host=cfg.host, port=cfg.port, debug=cfg.debug)
    except KeyboardInterrupt:
        print("\n👋 正在关闭...")
        site.stop()


if __name__ == "__main__":
    main()
