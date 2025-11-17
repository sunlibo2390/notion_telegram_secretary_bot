from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional

from data_pipeline.notion_api import NotionAPI
from data_pipeline.storage import paths

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NotionCollectorConfig:
    api_key: str
    api_version: str
    database_ids: Dict[str, str]
    data_dir: Path
    duration_threshold_minutes: int = 30
    sync_interval_seconds: int = 1800
    force_update: bool = False


@dataclass
class NotionCollector:
    config: NotionCollectorConfig
    processors: Iterable[Callable[[], None]] = field(default_factory=list)
    update_marker_filename: str = "last_updated.txt"
    _api_client: NotionAPI = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._api_client = NotionAPI(
            api_key=self.config.api_key,
            api_version=self.config.api_version,
        )

    def _update_marker_path(self) -> Path:
        return self.config.data_dir / self.update_marker_filename

    def _read_last_updated(self) -> datetime | None:
        marker = self._update_marker_path()
        if not marker.exists():
            return None
        value = marker.read_text(encoding="utf-8").strip()
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _write_last_updated(self) -> None:
        self._update_marker_path().write_text(
            datetime.now().isoformat(), encoding="utf-8"
        )

    def update_needed(self) -> bool:
        if self.config.force_update:
            return True
        last_updated = self._read_last_updated()
        if not last_updated:
            return True
        delta = datetime.now() - last_updated
        return delta >= timedelta(minutes=self.config.duration_threshold_minutes)

    def fetch_database(self, database_id: str) -> Dict:
        logger.info("请求 Notion 数据库：%s", database_id)
        payload = self._api_client.query_database(database_id)
        logger.info("Notion 数据库 %s 请求成功", database_id)
        return payload

    def _persist_raw_payload(self, key: str, data: Dict) -> None:
        raw_path = paths.raw_json_path(key)
        with raw_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=4)

    def collect_once(self, progress_callback: Optional[Callable[[str], None]] = None) -> None:
        if not self.update_needed():
            logger.info("Skip Notion collection: data already fresh.")
            return
        total_databases = len(self.config.database_ids)
        if total_databases == 0:
            logger.warning("No Notion databases configured for collection.")
            return
        for key, database_id in self.config.database_ids.items():
            if progress_callback:
                progress_callback(
                    f"拉取 Notion 数据库 {key}（{database_id}）中..."
                )
            payload = self.fetch_database(database_id)
            self._persist_raw_payload(key, payload)
        for processor in self.processors:
            name = getattr(processor, "__name__", processor.__class__.__name__)
            if progress_callback:
                progress_callback(f"开始运行处理器 {name}...")
            processor()
        self._write_last_updated()

    def run_forever(self) -> None:
        interval = self.config.sync_interval_seconds
        while True:
            try:
                self.collect_once()
            except Exception as error:
                print(f"[collector] error: {error}")
            time.sleep(interval)
