from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentReply:
    thread_id: str
    situation: str
    suggested_reply: str
    should_notify: bool = True


@dataclass(frozen=True)
class FeedbackAnalysis:
    understanding: str
    proposed_rule: str | None
    conflict_key: str | None
    scope: str
    regenerate_current: bool
    revision_instruction: str | None


class AgentProvider(Protocol):
    async def suggest(
        self,
        *,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        rules: list[str],
        thread_id: str | None,
    ) -> AgentReply: ...

    async def analyze_feedback(
        self,
        *,
        feedback: str,
        chat_name: str,
        original_message: str,
        situation: str,
        suggested_reply: str,
        rules: list[str],
    ) -> FeedbackAnalysis: ...

    async def revise(
        self,
        *,
        feedback: str,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        rules: list[str],
        thread_id: str,
    ) -> AgentReply: ...
