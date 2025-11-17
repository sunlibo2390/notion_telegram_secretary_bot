from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import List

from apps.telegram_bot.clients import TelegramBotClient, WeComWebhookClient
from apps.telegram_bot.handlers import CommandRouter
from apps.telegram_bot.history import HistoryStore
from apps.telegram_bot.proactivity import ProactivityService
from apps.telegram_bot.rest import RestScheduleService
from apps.telegram_bot.session_monitor import TaskSessionMonitor
from apps.telegram_bot.tracker import TaskTracker
from apps.telegram_bot.user_state import UserStateService
from core.llm.agent import LLMAgent
from core.llm.context_builder import AgentContextBuilder
from core.llm.openai_client import OpenAIChatClient
from core.llm.run_logger import AgentRunLogger
from core.llm.tools import build_default_tools
from core.repositories import LogRepository, ProjectRepository, TaskRepository
from core.services import LogbookService, StatusGuard, TaskSummaryService
from infra.config import load_settings
from infra.notion_sync import NotionSyncService

logger = logging.getLogger(__name__)


@dataclass
class BotRuntime:
    client: TelegramBotClient
    history: HistoryStore
    router: CommandRouter
    poll_timeout: int = 25
    background_threads: List[threading.Thread] = field(default_factory=list)

    def run_forever(self):
        logger.info("Starting Telegram bot long-polling loop")
        offset = self.history.last_update_id()
        while True:
            try:
                updates = self.client.get_updates(
                    offset=offset + 1 if offset is not None else None,
                    timeout=self.poll_timeout,
                )
                if not updates:
                    time.sleep(5)
                    continue
                for update in updates:
                    self.router.handle(update)
                    offset = update.get("update_id", offset)
                    self.history.record_update_checkpoint(offset)
            except Exception as error:
                logger.exception("Bot polling error: %s", error)
                time.sleep(5)


def build_runtime() -> BotRuntime:
    settings = load_settings()
    history = HistoryStore(settings.paths.history_dir)
    user_state = UserStateService(settings.paths.history_dir / "user_state.json")
    user_state.reset_all()
    rest_service = RestScheduleService(settings.paths.history_dir / "rest_windows.json")
    task_repo = TaskRepository()
    project_repo = ProjectRepository()
    log_repo = LogRepository()
    notion_sync = NotionSyncService(
        settings=settings,
        task_repository=task_repo,
        project_repository=project_repo,
        log_repository=log_repo,
    )
    notion_sync.set_progress_callback(
        lambda message: logger.info("[NotionSync] %s", message)
    )
    task_service = TaskSummaryService(task_repo, project_repo, log_repo)
    logbook_service = LogbookService(log_repo, task_repo)
    status_guard = StatusGuard(task_repo)
    wecom_client = (
        WeComWebhookClient(settings.wecom.webhook_url)
        if settings.wecom
        else None
    )
    client = TelegramBotClient(
        token=settings.telegram.token,
        history_store=history,
        request_timeout=settings.telegram.poll_timeout + 5,
        wecom_client=wecom_client,
    )
    profile_path = Path(__file__).resolve().parents[2] / "docs" / "user_profile_doc.md"
    # print(profile_path)
    context_builder = AgentContextBuilder(history, profile_path)
    run_logger = AgentRunLogger(settings.paths.history_dir / "agent_runs")
    tracker = TaskTracker(
        client,
        interval_seconds=settings.tracker_interval,
        follow_up_seconds=settings.tracker_follow_up,
        rest_service=rest_service,
        user_state=user_state,
    )
    session_monitor = TaskSessionMonitor(
        client,
        rest_service,
        tracker=tracker,
        task_repository=task_repo,
    )
    proactivity = ProactivityService(
        state_service=user_state,
        rest_service=rest_service,
        state_check_seconds=settings.proactivity.state_check_seconds,
        state_stale_seconds=settings.proactivity.state_stale_seconds,
        state_prompt_cooldown_seconds=settings.proactivity.state_prompt_cooldown_seconds,
        follow_up_seconds=settings.proactivity.question_follow_up_seconds,
        state_unknown_retry_seconds=settings.proactivity.state_unknown_retry_seconds,
        tracker=tracker,
    )
    llm_agent = None
    tools = build_default_tools(
        task_service,
        logbook_service,
        status_guard,
        tracker=tracker,
        task_repository=task_repo,
        log_repository=log_repo,
        history_store=history,
        user_state_service=user_state,
        rest_service=rest_service,
        session_monitor=session_monitor,
        notion_sync_service=notion_sync,
    )
    if settings.llm and settings.llm.enabled:
        llm_client = OpenAIChatClient(
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
            model=settings.llm.model,
            provider=settings.llm.provider,
        )
        llm_agent = LLMAgent(
            context_builder=context_builder,
            task_service=task_service,
            logbook_service=logbook_service,
            status_guard=status_guard,
            tools=tools,
            llm_client=llm_client,
            temperature=settings.llm.temperature,
            run_logger=run_logger,
        )
    else:
        llm_agent = LLMAgent(
            context_builder=context_builder,
            task_service=task_service,
            logbook_service=logbook_service,
            status_guard=status_guard,
            tools=tools,
            llm_client=None,
            run_logger=run_logger,
        )

    if llm_agent is None:
        raise RuntimeError(
            "LLM Agent 未配置。请在 config/settings.toml -> [llm] 中设置有效的 api_key / model。"
        )

    background_threads: List[threading.Thread] = []
    if settings.notion.sync_interval > 0:
        thread = notion_sync.start_background_sync(settings.notion.sync_interval)
        background_threads.append(thread)

    router = CommandRouter(
        client=client,
        history_store=history,
        agent=llm_agent,
        task_repo=task_repo,
        log_repo=log_repo,
        tracker=tracker,
        proactivity=proactivity,
        user_state=user_state,
        rest_service=rest_service,
        session_monitor=session_monitor,
        notion_sync=notion_sync,
    )
    return BotRuntime(
        client=client,
        history=history,
        router=router,
        poll_timeout=settings.telegram.poll_timeout,
        background_threads=background_threads,
    )


def main():
    logging.basicConfig(level=logging.INFO)
    runtime = build_runtime()
    runtime.run_forever()


if __name__ == "__main__":
    main()
