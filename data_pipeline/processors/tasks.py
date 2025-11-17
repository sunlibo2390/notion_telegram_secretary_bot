from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List

from data_pipeline.notion_api import NotionAPI
from data_pipeline.processors.base import read_payload, write_payload
from data_pipeline.transformers import blocks_to_markdown

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TasksProcessor:
    source_path: Path
    output_path: Path
    projects_index_path: Path
    notion_api: NotionAPI
    exclude_statuses: tuple[str, ...] = ("Done", "Dormant")

    def run(self) -> None:
        raw = read_payload(self.source_path)
        projects = read_payload(self.projects_index_path)
        processed: Dict[str, Dict] = {}
        results = raw.get("results", [])
        total = len(results)
        for index, item in enumerate(results, start=1):
            task_id = item.get("id")
            if not task_id:
                continue
            try:
                payload = self._build_payload(item, projects)
            except Exception as exc:  # pragma: no cover - defensive log
                logger.warning("Skip task %s due to %s", task_id, exc)
                continue
            if payload["status"] in self.exclude_statuses:
                continue
            processed[task_id] = payload
            self._log_progress(index, total, label="任务")
        self._attach_subtask_names(processed)
        write_payload(self.output_path, processed)
        logger.info(
            "Processed %s active tasks -> %s",
            len(processed),
            self.output_path,
        )

    def _build_payload(self, item: Dict, projects: Dict[str, Dict]) -> Dict:
        props = item.get("properties") or {}
        name_prop = props.get("Name", {})
        title = name_prop.get("title") or []
        name = title[0]["plain_text"] if title else "Untitled"
        priority_select = props.get("Priority", {}).get("select")
        priority = priority_select["name"] if priority_select else "No Priority"
        status_name = (
            props.get("Status", {}).get("status", {}).get("name", "Unknown")
        )
        relations = props.get("Projects", {}).get("relation", [])
        project_id = relations[0]["id"] if relations else None
        due = props.get("Due Date", {}).get("date")
        page_url = item.get("url") or f"https://www.notion.so/{item['id'].replace('-', '')}"
        subtasks_relation = props.get("Subtasks", {}).get("relation", [])
        subtask_ids = [rel.get("id") for rel in subtasks_relation if rel.get("id")]
        md_text = self._fetch_page_markdown(item["id"])
        project_info = projects.get(project_id, {})
        return {
            "name": name,
            "priority": priority,
            "status": status_name,
            "content": md_text,
            "project_id": project_id,
            "project_name": project_info.get("name", "Unknown Project"),
            "due_date": (due or {}).get("start"),
            "page_url": page_url,
            "subtasks_id": subtask_ids,
        }

    def _fetch_page_markdown(self, page_id: str) -> str:
        payload = self.notion_api.fetch_block_children(page_id)
        return blocks_to_markdown(payload.get("results", []))

    def _attach_subtask_names(self, processed: Dict[str, Dict]) -> None:
        for task_id, payload in processed.items():
            names: List[str] = []
            for subtask_id in payload.get("subtasks_id", []):
                subtask_info = processed.get(subtask_id)
                if subtask_info:
                    names.append(subtask_info["name"])
            payload["subtask_names"] = names
            payload.pop("subtasks_id", None)

    def _log_progress(self, current: int, total: int, label: str) -> None:
        if total == 0:
            return
        step = max(1, total // 10)
        if current % step == 0 or current == total:
            logger.info("%s处理进度：%d/%d", label, current, total)
