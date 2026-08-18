from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


@dataclass(frozen=True)
class ChatOnboardingDraft:
    name: str
    wiki: str
    directory_slug: str = ""


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
