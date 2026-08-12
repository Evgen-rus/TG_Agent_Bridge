"""Long-polling Telegram adapter for client suggestions and owner learning."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import logging
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from agentbridge.application import IncomingMessage
from .formatter import format_learning_proposal, format_owner_message, format_rules

logger = logging.getLogger(__name__)


class IncomingMessageService(Protocol):
    def handle_messages(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> Awaitable["Suggestion | None"]: ...


@dataclass
class _PendingBatch:
    messages: list[IncomingMessage]
    task: asyncio.Task[None]


def _confirmation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, применить", callback_data=f"learn:yes:{draft_id}"),
        InlineKeyboardButton("Нет, уточнить", callback_data=f"learn:no:{draft_id}"),
    ]])


def create_telegram_application(*, token: str, owner_chat_id: int, message_service: IncomingMessageService, batch_seconds: float = 20.0) -> Application:
    if not token.strip():
        raise ValueError("Telegram bot token must not be empty.")
    if batch_seconds < 0:
        raise ValueError("Message batch interval must not be negative.")
    application = Application.builder().token(token.strip()).build()
    pending_batches: dict[int, _PendingBatch] = {}

    async def _send(bot, *, chat_id: int, text: str, reply_markup=None):
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await bot.send_message(**kwargs)

    async def _deliver_suggestion(bot, suggestion) -> None:
        sent = await _send(bot, chat_id=owner_chat_id, text=format_owner_message(suggestion))
        if sent is not None and getattr(sent, "message_id", None) is not None and suggestion.recommendation_id:
            message_service.record_owner_delivery(suggestion.recommendation_id, owner_chat_id, sent.message_id)
        logger.info("event=owner_suggestion_sent recommendation_id=%s owner_message_id=%s", suggestion.recommendation_id, getattr(sent, "message_id", None))

    async def send_batch(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await asyncio.sleep(batch_seconds)
            batch = pending_batches.pop(chat_id, None)
            if batch is None:
                return
            recommendation = await message_service.handle_messages(chat_id, batch.messages)
            if recommendation is not None:
                await _deliver_suggestion(context.bot, recommendation)
        except asyncio.CancelledError:
            raise
        except Exception:
            pending_batches.pop(chat_id, None)
            logger.exception("event=client_batch_failed chat_id=%s", chat_id)
            await _send(context.bot, chat_id=owner_chat_id, text=f"AgentBridge не смог подготовить рекомендацию. Чат ID: {chat_id}. Проверьте журнал приложения.")

    async def owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        sender = update.effective_user
        if message is None or chat is None or chat.id != owner_chat_id or not message.text:
            return False
        reply = getattr(message, "reply_to_message", None)
        reply_id = getattr(reply, "message_id", None)
        if reply_id is None:
            return True
        replied_sender = getattr(reply, "from_user", None)
        bot_id = getattr(context.bot, "id", None)
        replied_to_this_bot = bool(
            replied_sender is not None
            and getattr(replied_sender, "is_bot", False)
            and (bot_id is None or getattr(replied_sender, "id", None) == bot_id)
        )
        if not replied_to_this_bot:
            logger.info(
                "event=owner_reply_ignored reason=not_bot_message owner_message_id=%s reply_to_message_id=%s",
                getattr(message, "message_id", None), reply_id,
            )
            return True
        logger.info(
            "event=owner_feedback_received owner_message_id=%s reply_to_message_id=%s author_id=%s update_id=%s",
            getattr(message, "message_id", None), reply_id, getattr(sender, "id", None), update.update_id,
        )
        proposal = await message_service.clarify_feedback(reply_id, message.text, update.update_id)
        if proposal is None:
            proposal = await message_service.handle_owner_feedback(
                owner_chat_id, reply_id, getattr(sender, "id", 0), getattr(sender, "full_name", "") or "Неизвестный владелец", message.text, update.update_id
            )
        if proposal is None:
            logger.info(
                "event=owner_feedback_ignored reason=unlinked_or_duplicate reply_to_message_id=%s update_id=%s",
                reply_id, update.update_id,
            )
            await _send(
                context.bot,
                chat_id=owner_chat_id,
                text=(
                    "Не могу связать это замечание с рекомендацией. Возможно, она была "
                    "отправлена до обновления AgentBridge. Ответьте на новую рекомендацию бота."
                ),
            )
            return True
        await _send(context.bot, chat_id=owner_chat_id, text=format_learning_proposal(proposal), reply_markup=_confirmation_keyboard(proposal.draft_id))
        return True

    async def queue_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return
        sender = update.effective_user
        if sender is not None and sender.is_bot:
            logger.info("event=telegram_message_ignored reason=bot chat_id=%s", chat.id)
            return
        if await owner_reply(update, context):
            return
        item = IncomingMessage(getattr(sender, "full_name", "") if sender else "", message.text, update.update_id)
        batch = pending_batches.get(chat.id)
        if batch is not None:
            batch.messages.append(item)
            logger.info("event=client_message_batched chat_id=%s batch_size=%s", chat.id, len(batch.messages))
            return
        task = asyncio.create_task(send_batch(chat.id, context))
        pending_batches[chat.id] = _PendingBatch([item], task)
        logger.info("event=client_batch_opened chat_id=%s update_id=%s", chat.id, update.update_id)

    async def learning_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or query.message is None or query.message.chat.id != owner_chat_id:
            return
        await query.answer()
        try:
            _, action, raw_id = (query.data or "").split(":", 2)
            draft_id = int(raw_id)
        except (ValueError, AttributeError):
            return
        if action == "no":
            prompt = await _send(context.bot, chat_id=owner_chat_id, text="Что я понял неправильно? Ответьте на это сообщение уточнением.")
            if prompt is not None and getattr(prompt, "message_id", None) is not None:
                message_service.mark_awaiting_clarification(draft_id, prompt.message_id)
            await query.edit_message_reply_markup(reply_markup=None)
            return
        result = await message_service.confirm_learning(draft_id)
        await query.edit_message_reply_markup(reply_markup=None)
        if result is None:
            await _send(context.bot, chat_id=owner_chat_id, text="Это изменение уже обработано или больше недоступно.")
            return
        await _send(context.bot, chat_id=owner_chat_id, text=("Правило сохранено." if result.rule_saved else "Исправление применено без постоянного правила."))
        if result.revised_suggestion is not None:
            await _deliver_suggestion(context.bot, result.revised_suggestion)
        elif result.current_suppressed:
            await _send(context.bot, chat_id=owner_chat_id, text="После исправления рекомендация для этого сообщения больше не требуется.")

    async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat and update.effective_chat.id == owner_chat_id:
            await _send(context.bot, chat_id=owner_chat_id, text=format_rules(message_service.list_rules()))

    async def undo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_chat or update.effective_chat.id != owner_chat_id:
            return
        rule = message_service.undo_latest_rule()
        text = "Активных правил для отмены нет." if rule is None else f"Последнее правило отменено:\n{rule.rule_text}"
        await _send(context.bot, chat_id=owner_chat_id, text=text)

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queue_message))
    application.add_handler(CallbackQueryHandler(learning_callback, pattern=r"^learn:(yes|no):\d+$"))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("undo", undo_command))
    return application


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agentbridge.application import Suggestion
