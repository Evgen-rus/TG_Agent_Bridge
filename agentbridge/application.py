from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .agents.base import AgentProvider
from .chats.loader import ChatRegistry
from .storage.sqlite import ChatThreadStore


@dataclass(frozen=True)
class Suggestion:
    chat_name: str
    sender_name: str
    original_message: str
    situation: str
    suggested_reply: str


@dataclass(frozen=True)
class IncomingMessage:
    sender_name: str
    text: str
    update_id: int | None = None


class AgentBridgeApplication:
    def __init__(
        self,
        registry: ChatRegistry,
        store: ChatThreadStore,
        provider: AgentProvider,
    ):
        self.registry = registry
        self.store = store
        self.provider = provider
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def handle_message(
        self,
        telegram_chat_id: int,
        sender_name: str,
        message: str,
        update_id: int | None = None,
    ) -> Suggestion | None:
        return await self.handle_messages(
            telegram_chat_id,
            [IncomingMessage(sender_name, message, update_id)],
        )

    async def handle_messages(
        self,
        telegram_chat_id: int,
        messages: list[IncomingMessage],
    ) -> Suggestion | None:
        chat = self.registry.get(telegram_chat_id)
        if chat is None:
            return None

        lock = self._chat_locks.setdefault(telegram_chat_id, asyncio.Lock())
        async with lock:
            pending = [
                item
                for item in messages
                if item.text.strip()
                and (
                    item.update_id is None
                    or not self.store.is_update_processed(item.update_id)
                )
            ]
            if not pending:
                return None

            sender_names = list(
                dict.fromkeys(
                    item.sender_name.strip() or "Неизвестный отправитель"
                    for item in pending
                )
            )
            sender_name = (
                sender_names[0]
                if len(sender_names) == 1
                else ", ".join(sender_names)
            )
            combined_message = (
                pending[0].text.strip()
                if len(pending) == 1
                else "\n".join(
                    f"{item.sender_name.strip() or 'Неизвестный отправитель'}: "
                    f"{item.text.strip()}"
                    for item in pending
                )
            )
            thread_id = self.store.get_thread_id(telegram_chat_id)
            reply = await self.provider.suggest(
                message=combined_message,
                sender_name=sender_name,
                chat_name=chat.name,
                wiki=chat.wiki,
                thread_id=thread_id,
            )
            self.store.save_thread(
                telegram_chat_id=chat.telegram_chat_id,
                logical_name=chat.name,
                codex_thread_id=reply.thread_id,
                agent_provider=chat.agent_provider,
            )
            for item in pending:
                if item.update_id is not None:
                    self.store.mark_update_processed(item.update_id)
        return Suggestion(
            chat_name=chat.name,
            sender_name=sender_name,
            original_message=combined_message,
            situation=reply.situation,
            suggested_reply=reply.suggested_reply,
        )
