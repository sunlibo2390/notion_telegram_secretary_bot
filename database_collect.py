import argparse

from data_pipeline.collectors.notion import NotionCollector, NotionCollectorConfig
from data_pipeline.pipeline import build_default_processors
from infra.config import Settings, load_settings


def collector_from_settings(settings: Settings, force: bool = False) -> NotionCollector:
    if not settings.notion.database_ids:
        raise RuntimeError("No Notion database IDs configured.")
    config = NotionCollectorConfig(
        api_key=settings.notion.api_key,
        api_version=settings.notion.api_version,
        database_ids=settings.notion.database_ids,
        data_dir=settings.paths.data_dir,
        duration_threshold_minutes=30,
        sync_interval_seconds=settings.notion.sync_interval,
        force_update=force or settings.notion.force_update,
    )
    processors = build_default_processors(config)
    return NotionCollector(config=config, processors=processors)


def build_collector(force: bool) -> NotionCollector:
    settings = load_settings(force_update=force, require_telegram=False)
    return collector_from_settings(settings, force=force)


def main():
    parser = argparse.ArgumentParser(description="Notion Database Collector")
    parser.add_argument(
        "--force", action="store_true", help="Force update regardless of timestamp"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep running instead of collecting once",
    )
    args = parser.parse_args()
    collector = build_collector(force=args.force)
    if args.loop:
        collector.run_forever()
    else:
        collector.collect_once()


if __name__ == "__main__":
    main()
