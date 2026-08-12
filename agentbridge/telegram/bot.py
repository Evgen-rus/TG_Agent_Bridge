"""Long-polling Telegram adapter for the suggest-only AgentBridge MVP."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import logging
from typing import Protocol

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from agentbridge.application import IncomingMessage

from .formatter import format_owner_message

logger = logging.getLogger(__name__)


class IncomingMessageService(Protocol):
    def handle_messages(
        self,
        telegram_chat_id: int,
        messages: list[IncomingMessage],
    ) -> Awaitable["Suggestion | None"]: ...


@dataclass
class _PendingBatch:
    messages: list[IncomingMessage]
    task: asyncio.Task[None]


def create_telegram_application(
    *,
    token: str,
    owner_chat_id: int,
    message_service: IncomingMessageService,
    batch_seconds: float = 20.0,
) -> Application:
    """Build a suggest-only Telegram application with per-chat batching."""
    if not token.strip():
        raise ValueError("Telegram bot token must not be empty.")
    if batch_seconds < 0:
        raise ValueError("Message batch interval must not be negative.")

    application = Application.builder().token(token.strip()).build()
    pending_batches: dict[int, _PendingBatch] = {}

    async def send_batch(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await asyncio.sleep(batch_seconds)
            batch = pending_batches.pop(chat_id, None)
            if batch is None:
                return
            recommendation = await message_service.handle_messages(
                telegram_chat_id=chat_id,
                messages=batch.messages,
            )
            if recommendation is not None:
                await context.bot.send_message(
                    chat_id=owner_chat_id,
                    text=format_owner_message(recommendation),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            pending_batches.pop(chat_id, None)
            logger.exception("Failed to process Telegram message batch from chat %s", chat_id)
            await context.bot.send_message(
                chat_id=owner_chat_id,
                text=(
                    "AgentBridge не смог подготовить рекомендацию. "
                    f"Чат ID: {chat_id}. Проверьте журнал приложения."
                ),
            )

    async def queue_message(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return

        sender = update.effective_user
        if sender is not None and sender.is_bot:
            logger.info("Ignoring message from bot account in chat %s", chat.id)
            return

        item = IncomingMessage(
            sender_name=sender.full_name if sender is not None else "",
            text=message.text,
            update_id=update.update_id,
        )
        batch = pending_batches.get(chat.id)
        if batch is not None:
            batch.messages.append(item)
            return

        task = asyncio.create_task(send_batch(chat.id, context))
        pending_batches[chat.id] = _PendingBatch(messages=[item], task=task)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queue_message))
    return application


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentbridge.application import Suggestion
