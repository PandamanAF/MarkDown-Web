"""
热重载 —— 监视文档目录的文件变更并触发重建。

采用轻量级轮询方式（跨平台兼容，Windows 上无需 inotify 依赖）。
在独立线程中运行；通过回调通知主服务器重新加载。
"""

from __future__ import annotations
import os
import time
import threading
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("mdw.hotreload")


@dataclass
class FileSnapshot:
    """记录单个文件的修改时间（mtime）。"""

    path: Path
    mtime: float

    def changed(self) -> bool:
        """检查文件是否已修改。"""
        try:
            return self.path.stat().st_mtime != self.mtime
        except OSError:
            return True  # 文件丢失视为已变更


WatcherCallback = Callable[[list[str]], None]  # 回调类型：路径列表 → None


class HotReloader:
    """基于轮询的文件监视器，文件变更时调用 *on_change*。

    用法：::

        watcher = HotReloader("docs/")
        watcher.start(on_change=lambda paths: print("变更:", paths))
        # ... 之后 ...
        watcher.stop()
    """

    def __init__(
        self,
        watch_dir: str | Path,
        interval: float = 1.0,
        extensions: tuple[str, ...] = (".mdw", ".md", ".markdown"),
    ):
        """
        参数：
            watch_dir: 要监视的目录
            interval: 轮询间隔（秒）
            extensions: 关注的文档扩展名
        """
        self._watch_dir = Path(watch_dir).resolve()
        self._interval = interval
        self._extensions = extensions
        self._snapshots: dict[Path, FileSnapshot] = {}   # 文件 → 快照
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()                  # 运行标志
        self._callback: Optional[WatcherCallback] = None

    # -- 公开 API ----------------------------------------------------------

    def start(self, on_change: Optional[WatcherCallback] = None) -> None:
        """在后台守护线程中开始轮询。"""
        if self._thread and self._thread.is_alive():
            logger.warning("热重载已在运行")
            return
        if on_change:
            self._callback = on_change

        self._refresh_snapshots()
        self._running.set()
        self._thread = threading.Thread(
            target=self._loop, name="mdw-hotreload", daemon=True
        )
        self._thread.start()
        logger.info("热重载已启动（轮询间隔 %.1f 秒）", self._interval)

    def stop(self) -> None:
        """通知监视线程停止。"""
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("热重载已停止")

    def refresh(self) -> None:
        """强制刷新快照（手动重建后调用）。"""
        self._refresh_snapshots()

    # -- 内部方法 ---------------------------------------------------------

    def _loop(self) -> None:
        """轮询主循环。"""
        while self._running.is_set():
            time.sleep(self._interval)
            try:
                changed = self._poll()
                if changed and self._callback:
                    self._callback(changed)
            except Exception:
                logger.exception("热重载轮询出错 —— 继续运行")

    def _poll(self) -> list[str]:
        """
        检查所有文件是否发生变化。
        返回变更的文件路径列表，同时更新快照。
        """
        changed: list[str] = []
        current: dict[Path, FileSnapshot] = {}

        for file_path in self._walk():
            try:
                stat = file_path.stat()
                new_snap = FileSnapshot(path=file_path, mtime=stat.st_mtime)
                current[file_path] = new_snap

                old = self._snapshots.get(file_path)
                # mtime 差异大于 0.001 秒视为已变更
                if old is None or abs(old.mtime - new_snap.mtime) > 0.001:
                    changed.append(str(file_path))

            except OSError:
                # 文件消失 —— 报告为变更
                if file_path in self._snapshots:
                    changed.append(str(file_path))

        # 检测被删除的文件
        for path in list(self._snapshots):
            if path not in current:
                changed.append(str(path))

        self._snapshots = current
        return changed

    def _walk(self) -> list[Path]:
        """递归列出被监视目录下的所有文档文件。"""
        files: list[Path] = []
        base = self._watch_dir
        if not base.is_dir():
            return files
        for entry in base.rglob("*"):
            if entry.is_file() and entry.suffix.lower() in self._extensions:
                files.append(entry)
        return files

    def _refresh_snapshots(self) -> None:
        """重新扫描所有文件，刷新快照字典。"""
        self._snapshots = {}
        for fp in self._walk():
            try:
                self._snapshots[fp] = FileSnapshot(path=fp, mtime=fp.stat().st_mtime)
            except OSError:
                pass
