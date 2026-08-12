from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AgentReply:
    thread_id: str
    situation: str
    suggested_reply: str


class AgentProvider(Protocol):
    async def suggest(
        self,
        *,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        thread_id: str | None,
    ) -> AgentReply: ...

