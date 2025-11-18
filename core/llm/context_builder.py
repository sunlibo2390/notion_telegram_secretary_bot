from __future__ import annotations

from pathlib import Path
from typing import List, Protocol

from core.utils.timezone import beijing_now


class HistoryProvider(Protocol):
    def get_history(self, chat_id: int, limit: int = 20):
        ...


class AgentContextBuilder:
    def __init__(
        self,
        history_provider: HistoryProvider,
        user_profile_path: Path,
        history_limit: int = 8,
    ):
        self._history = history_provider
        self._history_limit = history_limit
        self._profile_text = self._load_profile(user_profile_path)

    @staticmethod
    def _load_profile(path: Path) -> str:
        if not path.exists():
            return "用户画像缺失。"
        return path.read_text(encoding="utf-8")

    def build_messages(self, chat_id: int, user_text: str) -> List[dict]:
        history_entries = self._history.get_history(chat_id, limit=self._history_limit)
        beijing_time = beijing_now()
        system_prompt = (
            "你是一名 AI 秘书，负责在 Telegram 中高效督促用户，所有回复必须使用 Markdown。\n"
            "用户画像如下：\n"
            f"{self._profile_text}\n"
            f"当前北京时间：{beijing_time:%Y-%m-%d %H:%M}\n"
            "务必遵守：\n"
            "- 所有时间/计时器都基于真实客观时间（以当前北京时间为准），禁止主观估计“已经过去多久”。\n"
            "- 风格选择：\n"
            "  * 问时间/状态：最多 2 句，每句 ≤15 字，禁止问句，直接陈述并命令。\n"
            "  * 任务规划/追踪：列事实/风险/下一步，可附具体时间点。\n"
            "- 涉及任务汇总与进展报告或日程安排时，至少包含如下信息及分析，以条目形式结构化输出，禁止以表格形式输出：\n"
            "  1. 带超链接的任务名称\n"
            "  2. 截止时间\n"
            "  3. 状态\n"
            "  4. 优先级\n"
            "  5. 进展\n"
            "  6. 相关日志（如有）\n"
            "  7. 风险与建议（如有）\n"
            "- 禁止使用###等标题语法。标题以emoji开头，如“### 📋 今日关键任务”请改为“📋 今日关键任务”。\n"
            "- 进行日程安排时，需要综合考虑时间、用户作息节奏、优先级和依赖关系，确保任务合理安排。明确具体执行时间段，确保日程规划清晰。\n"
            "- 基于事实，不给无意义安慰；若不了解任务，先追问。\n"
            "- 指令必须明确，必要时给出精确到分钟的截止时间。\n"
            "- 跟踪提醒的间隔可以自定义为任意 ≥5 分钟的时长。\n"
            "- 当用户用“小时/分钟/秒”等描述提醒间隔时，请主动换算为分钟（例如 8 小时=480 分钟）后执行，禁止让用户再次输入。\n"
        )

        messages: List[dict] = [{"role": "system", "content": system_prompt}]
        for entry in history_entries:
            role = "assistant" if entry.direction == "bot" else "user"
            messages.append({"role": role, "content": entry.text})
        messages.append({"role": "user", "content": user_text})
        return messages
