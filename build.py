"""
MDW 打包编译脚本

用法：
    python build.py              # Nuitka 单文件编译（先自动打包内置资源）
    python build.py --clean      # 清理构建产物后重新编译
    python build.py --install    # 编译 + 创建发布包
    python build.py --bundle     # 仅打包内置资源（docs/static/config → mdw/_bundle_assets.py）

产出（dist/ 下）：
    app.exe                      # 单文件可执行程序（内置 docs/static/config，首启自动解压）
"""

import base64
import io
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD_DIR = ROOT / "build_cache"
BUNDLE_MODULE = ROOT / "mdw" / "_bundle_assets.py"

# 打包进二进制的资源目录（docs 教程 / static 主题 / config 默认配置）
BUNDLE_DIRS = ["docs", "static"]


def make_bundle() -> bool:
    """把 docs/、static/、默认 config 打包为内嵌 zip 模块。

    生成 ``mdw/_bundle_assets.py``，包含 base64 编码的 zip 与文件清单。
    首次运行（或 exe 同目录缺失这些目录）时自动解压。
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 资源目录（docs / static）
        for name in BUNDLE_DIRS:
            base = ROOT / name
            if not base.is_dir():
                print(f"  ⚠️ 缺少 {name}/，跳过")
                continue
            for root, dirs, files in os.walk(base):
                # 跳过缓存
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for f in files:
                    full = Path(root) / f
                    arc = str(full.relative_to(ROOT)).replace("\\", "/")
                    z.write(full, arc)
        # 默认配置（不覆盖用户现有 config/）
        try:
            import yaml
            default_cfg = {
                "host": "127.0.0.1",
                "port": 8080,
                "docs_dir": "docs",
                "static_dir": "static",
                "config_dir": "config",
                "site_title": "MDW",
                "brand": "MDW",
                "admin_prefix": "/admin",
                "auto_reload": True,
                "debug": False,
                "api": {"enabled": True, "token": "mdw-admin"},
            }
            z.writestr("config/site.yaml", yaml.dump(
                default_cfg, default_flow_style=False,
                allow_unicode=True, sort_keys=False))
            print("  ✓ 打包默认配置 config/site.yaml")
        except Exception as exc:
            print(f"  ⚠️ 默认配置打包失败（{exc}），首次运行将自动生成")

    data = buf.getvalue()
    b64 = base64.b64encode(data).decode("ascii")
    # base64 每行 76 字符
    lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]

    # 文件清单（供快速判断是否已解压）
    manifest = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            top = info.filename.split("/", 1)[0]
            manifest.setdefault(top, 0)
            manifest[top] += 1

    with open(BUNDLE_MODULE, "w", encoding="utf-8") as f:
        f.write('"""内置资源包（构建期自动生成，勿手改）。"""\n')
        f.write(f"# 来源: build.py make_bundle()，{BUNDLE_DIRS} + config\n")
        f.write(f"BUNDLE_TOP_DIRS = {sorted(manifest)}\n")
        f.write(f"BUNDLE_MANIFEST = {manifest}\n")
        f.write("BUNDLE_ZIP_B64 = \"\"\"\n")
        f.write("\n".join(lines))
        f.write("\n\"\"\"\n")

    size_kb = len(data) / 1024
    print(f"  ✓ 内置资源包: {BUNDLE_MODULE.name} "
          f"({size_kb:.1f} KB，{sum(manifest.values())} 个文件)")
    return True


def clean():
    """清理旧的构建产物。"""
    for d in (DIST, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d)
            print(f"  清理: {d}")
    for p in ROOT.glob("*.build"):
        if p.is_dir():
            shutil.rmtree(p)
    for p in ROOT.glob("*.dist"):
        if p.is_dir():
            shutil.rmtree(p)


def compile_onefile():
    """使用 Nuitka 编译单文件 exe。"""
    # Nuitka 命令
    cmd = [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--onefile",
        "--windows-console-mode=attach",
        "--output-dir", str(DIST),
        f"--include-data-dir={ROOT / 'mdw' / 'templates'}=mdw/templates",
        f"--include-data-dir={ROOT / 'static'}=static",
        "--enable-plugin=anti-bloat",
        "--assume-yes-for-downloads",
        str(ROOT / "app.py"),
    ]

    # 如果 aiohttp 存在则包含
    try:
        import aiohttp  # noqa
        cmd.insert(-1, "--enable-plugin=pylint-warnings")
    except ImportError:
        pass

    print("  编译中...（约 1-3 分钟）")
    result = subprocess.run(cmd, cwd=str(ROOT), capture_output=False)
    if result.returncode != 0:
        print("❌ 编译失败")
        return False
    return True


def make_distribution():
    """创建发布包：复制 docs/ 示例 + 生成默认配置。"""
    release_dir = DIST / "MDW-release"
    if release_dir.exists():
        shutil.rmtree(release_dir)
    release_dir.mkdir(parents=True)

    exe = DIST / "app.exe"
    if not exe.exists():
        print("❌ app.exe 未找到，请先编译")
        return

    shutil.copy(exe, release_dir / "app.exe")

    # 复制示例 docs
    docs_src = ROOT / "docs"
    if docs_src.is_dir():
        shutil.copytree(docs_src, release_dir / "docs", dirs_exist_ok=True)

    # 预先创建 config/site.yaml
    import importlib.util
    spec = importlib.util.spec_from_file_location("mdw.config", ROOT / "mdw" / "config.py")
    cfg_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cfg_mod)

    cfg = cfg_mod.Config.from_defaults()
    config_dir = release_dir / "config"
    config_dir.mkdir(parents=True)
    import yaml
    with open(config_dir / "site.yaml", "w", encoding="utf-8") as f:
        yaml.dump({
            "host": "127.0.0.1",
            "port": 8080,
            "docs_dir": "docs",
            "static_dir": "static",
            "site_title": "MDW",
            "brand": "MDW",
            "admin_prefix": "/admin",
            "auto_reload": True,
            "debug": False,
        }, f, default_flow_style=False, allow_unicode=True)

    print(f"\n✅ 发布包已创建: {release_dir}")
    print(f"   {release_dir}/app.exe")
    print(f"   {release_dir}/docs/")
    print(f"   {release_dir}/config/site.yaml")


def main():
    args = set(sys.argv[1:])

    if "--clean" in args:
        clean()

    if "--bundle" in args:
        make_bundle()
        return

    # 编译前先打包内置资源（docs/static/config 首启自动解压）
    make_bundle()

    if "--install" in args:
        if compile_onefile():
            make_distribution()
    else:
        if compile_onefile():
            exe = DIST / "app.exe"
            if exe.exists():
                size_mb = exe.stat().st_size / (1024 * 1024)
                print(f"\n✅ 编译完成: {exe} ({size_mb:.1f} MB)")
                print(f"   用法: {exe}")
                print(f"   exe 已内置 docs/static/config，首次运行自动解压到同目录")


if __name__ == "__main__":
    main()
