from __future__ import annotations

from typing import Callable, List

from data_pipeline.collectors.notion import NotionCollectorConfig
from data_pipeline.notion_api import NotionAPI
from data_pipeline.processors import LogsProcessor, ProjectsProcessor, TasksProcessor
from data_pipeline.storage import paths


def build_default_processors(config: NotionCollectorConfig) -> List[Callable[[], None]]:
    """
    Wire together the canonical processors so that the collector can execute
    them after downloading the latest Notion databases.
    """

    notion_api = NotionAPI(
        api_key=config.api_key,
        api_version=config.api_version,
    )
    projects_processor = ProjectsProcessor(
        source_path=paths.raw_json_path("projects"),
        output_path=paths.processed_json_path("processed_projects"),
    )
    tasks_processor = TasksProcessor(
        source_path=paths.raw_json_path("tasks"),
        output_path=paths.processed_json_path("processed_tasks"),
        projects_index_path=paths.processed_json_path("processed_projects"),
        notion_api=notion_api,
    )
    logs_processor = LogsProcessor(
        source_path=paths.raw_json_path("logs"),
        output_path=paths.processed_json_path("processed_logs"),
        tasks_index_path=paths.processed_json_path("processed_tasks"),
        notion_api=notion_api,
    )
    return [projects_processor.run, tasks_processor.run, logs_processor.run]
