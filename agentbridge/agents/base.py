from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from ..owner_query import OwnerQueryIntent, PortfolioChatSummary


class AgentAction:
    REPLY = "reply"
    ASK_OWNER = "ask_owner"
    OBSERVE = "observe"
    NO_ACTION = "no_action"


NOTIFY_ACTIONS = {AgentAction.REPLY, AgentAction.ASK_OWNER, AgentAction.OBSERVE}


@dataclass(frozen=True)
class AgentReply:
    thread_id: str
    situation: str
    suggested_reply: str
    should_notify: bool = True
    action: str = ""
    observation: str = ""
    unknowns: str = ""
    owner_question: str = ""
    candidate_state: dict | None = None
    candidate_memory: list | None = None
    confidence: float | None = None
    needs_critique: bool = False

    def resolved_action(self) -> str:
        if self.action in {
            AgentAction.REPLY,
            AgentAction.ASK_OWNER,
            AgentAction.OBSERVE,
            AgentAction.NO_ACTION,
        }:
            return self.action
        return AgentAction.REPLY if self.should_notify else AgentAction.NO_ACTION

    def notifies_owner(self) -> bool:
        return self.resolved_action() in NOTIFY_ACTIONS


@dataclass(frozen=True)
class FeedbackAnalysis:
    understanding: str
    proposed_rule: str | None
    conflict_key: str | None
    scope: str
    regenerate_current: bool
    revision_instruction: str | None
    candidate_memory: str | None = None
    candidate_memory_scope: str | None = None


@dataclass(frozen=True)
class ChatOnboardingDraft:
    name: str
    wiki: str
    directory_slug: str = ""


@dataclass(frozen=True)
class OwnerQueryAnswer:
    thread_id: str
    answer: str


@dataclass(frozen=True)
class GeneralTaskPlan:
    thread_id: str
    understanding: str
    kind: str
    remind_at_utc: str = ""
    local_label: str = ""
    reminder_text: str = ""


@dataclass(frozen=True)
class MediaAttachment:
    path: str
    kind: str = ""
    mime: str = ""
    filename: str = ""


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
        context_pack: str = "",
        attachments: Sequence[MediaAttachment] = (),
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
        context_pack: str = "",
        attachments: Sequence[MediaAttachment] = (),
    ) -> AgentReply: ...

    async def answer_owner_query(
        self,
        *,
        question: str,
        chat_name: str,
        context_pack: str,
        thread_id: str | None,
        attachments: Sequence[MediaAttachment] = (),
    ) -> OwnerQueryAnswer: ...

    async def plan_general_task(
        self, *, request: str, timezone_name: str, now_local: str, thread_id: str | None,
    ) -> GeneralTaskPlan: ...

    async def run_general_task(self, *, request: str, thread_id: str) -> OwnerQueryAnswer: ...

    async def resolve_owner_query_scope(
        self, *, question: str, known_chats: Sequence[dict[str, str]],
    ) -> OwnerQueryIntent: ...

    async def summarize_portfolio_chat(
        self, *, question: str, chat_name: str, period: str, context_pack: str,
        detail_level: str,
    ) -> PortfolioChatSummary: ...

    async def aggregate_owner_portfolio(
        self, *, question: str, period: str, detail_level: str,
        summaries: Sequence[dict[str, object]],
    ) -> str: ...
