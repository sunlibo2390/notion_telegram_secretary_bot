from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from core.repositories import LogRepository, ProjectRepository, TaskRepository
from database_collect import collector_from_settings
from infra.config import Settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NotionSyncResult:
    success: bool
    message: str
    updated: bool
    duration_seconds: Optional[float] = None


class NotionSyncService:
    def __init__(
        self,
        settings: Settings,
        task_repository: TaskRepository,
        project_repository: ProjectRepository,
        log_repository: LogRepository,
    ):
        self._collector = collector_from_settings(settings, force=False)
        self._task_repo = task_repository
        self._project_repo = project_repository
        self._log_repo = log_repository
        self._lock = threading.Lock()
        self._progress_callback: Optional[Callable[[str], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None] | None) -> None:
        self._progress_callback = callback

    def _emit_progress(self, message: str) -> None:
        if not self._progress_callback:
            return
        try:
            self._progress_callback(message)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Progress callback failed", exc_info=True)

    def sync(
        self,
        actor: str = "manual",
        *,
        force: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ) -> NotionSyncResult:
        if not self._lock.acquire(blocking=False):
            return NotionSyncResult(
                success=False,
                message="已有同步任务正在执行，请稍后再试。",
                updated=False,
                duration_seconds=None,
            )
        start = time.time()
        previous_callback = self._progress_callback
        if progress_callback:
            self._progress_callback = progress_callback
        previous_force = self._collector.config.force_update
        self._collector.config.force_update = force or previous_force
        try:
            self._emit_progress("正在更新 Notion 原始数据...")
            self._collector.collect_once(progress_callback=self._emit_progress)
            self._project_repo.refresh()
            self._emit_progress("项目数据已刷新。")
            self._task_repo.refresh()
            self._emit_progress("任务数据已刷新。")
            self._log_repo.refresh()
            self._emit_progress("日志数据已刷新。")
            duration = time.time() - start
            logger.info("Notion 数据同步完成，actor=%s，耗时 %.2fs", actor, duration)
            return NotionSyncResult(
                success=True,
                message=f"Notion 数据已更新（耗时 {duration:.1f} 秒）",
                updated=True,
                duration_seconds=duration,
            )
        except Exception as error:
            logger.exception("Notion 数据同步失败，actor=%s：%s", actor, error)
            return NotionSyncResult(
                success=False,
                message=f"同步失败：{error}",
                updated=False,
                duration_seconds=None,
            )
        finally:
            self._progress_callback = previous_callback
            self._collector.config.force_update = previous_force
            self._lock.release()

    def start_background_sync(self, interval_seconds: int) -> threading.Thread:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be > 0 for background sync.")

        def _loop() -> None:
            logger.info(
                "Background Notion sync started with interval=%ss", interval_seconds
            )
            while True:
                try:
                    self.sync(actor="background")
                except Exception as error:  # pragma: no cover - defensive logging
                    logger.exception("Background Notion sync failed: %s", error)
                time.sleep(interval_seconds)

        thread = threading.Thread(target=_loop, name="NotionSyncWorker", daemon=True)
        thread.start()
        return thread
