from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from data_pipeline.notion_api import NotionAPI
from data_pipeline.processors.base import read_payload, write_payload
from data_pipeline.transformers import blocks_to_markdown

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LogsProcessor:
    source_path: Path
    output_path: Path
    tasks_index_path: Path
    notion_api: NotionAPI
    exclude_statuses: tuple[str, ...] = ("Done", "Dormant")

    def run(self) -> None:
        raw = read_payload(self.source_path)
        tasks = read_payload(self.tasks_index_path)
        processed: Dict[str, Dict] = {}
        results = raw.get("results", [])
        total = len(results)
        for index, item in enumerate(results, start=1):
            log_id = item.get("id")
            if not log_id:
                continue
            try:
                payload = self._build_payload(item, tasks)
            except Exception as exc:  # pragma: no cover - defensive log
                logger.warning("Skip log %s due to %s", log_id, exc)
                continue
            if payload["status"] in self.exclude_statuses:
                continue
            processed[log_id] = payload
            self._log_progress(index, total, label="日志")
        write_payload(self.output_path, processed)
        logger.info(
            "Processed %s log entries -> %s",
            len(processed),
            self.output_path,
        )

    def _build_payload(self, item: Dict, tasks: Dict[str, Dict]) -> Dict:
        props = item.get("properties") or {}
        name_prop = props.get("Name", {})
        title = name_prop.get("title") or []
        name = title[0]["plain_text"] if title else "Untitled"
        status_name = (
            props.get("Status", {}).get("status", {}).get("name", "Unknown")
        )
        relation = props.get("Task", {}).get("relation", [])
        task_id = relation[0]["id"] if relation else None
        md_text = self._fetch_page_markdown(item["id"])
        task_name = tasks.get(task_id, {}).get("name", "Unknown Task")
        return {
            "name": name,
            "status": status_name,
            "content": md_text,
            "task_id": task_id,
            "task_name": task_name,
        }

    def _fetch_page_markdown(self, page_id: str) -> str:
        payload = self.notion_api.fetch_block_children(page_id)
        return blocks_to_markdown(payload.get("results", []))

    def _log_progress(self, current: int, total: int, label: str) -> None:
        if total == 0:
            return
        step = max(1, total // 10)
        if current % step == 0 or current == total:
            logger.info("%s处理进度：%d/%d", label, current, total)
