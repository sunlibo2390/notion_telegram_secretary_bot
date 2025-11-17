from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NotionAPI:
    """
    Thin wrapper around the Notion HTTP API with retry/backoff logic so that
    collectors and processors can share the same networking code.
    """

    api_key: str
    api_version: str = "2022-06-28"
    timeout: int = 30
    max_retries: int = 5
    backoff_seconds: int = 10
    base_url: str = "https://api.notion.com/v1"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Notion-Version": self.api_version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_payload: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_payload,
                    params=params,
                    timeout=self.timeout,
                )
                if response.status_code == 200:
                    return response.json()
                last_error = RuntimeError(
                    f"Notion API {method} {path} failed with "
                    f"{response.status_code}: {response.text}"
                )
                logger.warning(
                    "Notion API request failed (attempt %s/%s): %s",
                    attempt,
                    self.max_retries,
                    last_error,
                )
            except requests.RequestException as exc:  # pragma: no cover - network
                last_error = exc
                logger.warning(
                    "Notion API request error (attempt %s/%s): %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
            time.sleep(self.backoff_seconds)
        raise last_error or RuntimeError(f"Notion API {method} {path} failed.")

    def query_database(
        self, database_id: str, payload: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        logger.info("Querying Notion database %s", database_id)
        return self._request(
            "POST",
            f"/databases/{database_id}/query",
            json_payload=payload,
        )

    def fetch_block_children(
        self, block_id: str, *, page_size: int = 100
    ) -> Dict[str, Any]:
        logger.debug("Fetching block children for %s", block_id)
        return self._request(
            "GET",
            f"/blocks/{block_id}/children",
            params={"page_size": page_size},
        )

    def fetch_page(self, page_id: str) -> Dict[str, Any]:
        logger.debug("Fetching page %s", page_id)
        return self._request("GET", f"/pages/{page_id}")
