from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any

from data_pipeline.processors.base import read_payload, write_payload

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProjectsProcessor:
    source_path: Path
    output_path: Path
    exclude_statuses: tuple[str, ...] = ("Done",)

    def run(self) -> None:
        raw = read_payload(self.source_path)
        processed: Dict[str, Dict[str, Any]] = {}
        for item in raw.get("results", []):
            project_id = item.get("id")
            payload = self._build_payload(item)
            if not project_id or not payload:
                continue
            if payload["status"] in self.exclude_statuses:
                continue
            processed[project_id] = payload
        write_payload(self.output_path, processed)
        logger.info(
            "Processed %s active projects -> %s",
            len(processed),
            self.output_path,
        )

    def _build_payload(self, item: Dict[str, Any]) -> Dict[str, str] | None:
        props = item.get("properties") or {}
        name_prop = props.get("Name", {})
        title = name_prop.get("title") or []
        name = title[0]["plain_text"] if title else "Untitled"
        status_prop = props.get("Status", {})
        status = status_prop.get("status", {}).get("name", "Unknown")
        return {"name": name, "status": status}
