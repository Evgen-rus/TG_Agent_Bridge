"""Long-polling Telegram adapter for client suggestions and owner learning."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
import logging
import time
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from agentbridge.application import IncomingMessage, MemoryProposal, QuestionReplyResult
from .formatter import format_learning_proposal, format_memory_proposal, format_owner_message, format_rules

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


def _memory_confirmation_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, сохранить", callback_data=f"memory:yes:{draft_id}"),
        InlineKeyboardButton("Нет, отменить", callback_data=f"memory:no:{draft_id}"),
    ]])


async def _telegram_try(operation: str, coro: Awaitable[object]) -> None:
    """UX-only Telegram calls: a timeout must not abort local work like saving a rule."""
    try:
        await coro
    except NetworkError:
        logger.warning("event=telegram_transient_error operation=%s", operation)


def _mentions_bot(message, bot) -> bool:
    text = getattr(message, "text", None) or ""
    names = {"agent"}
    username = getattr(bot, "username", None)
    if username:
        names.add(str(username).casefold())
    lowered = text.casefold()
    if any(f"@{name}" in lowered for name in names):
        return True
    bot_id = getattr(bot, "id", None)
    for entity in getattr(message, "entities", None) or []:
        mentioned = getattr(entity, "user", None)
        if mentioned is not None and bot_id is not None and getattr(mentioned, "id", None) == bot_id:
            return True
    return False


def _telegram_date(message) -> str:
    date = getattr(message, "date", None)
    if date is None:
        return ""
    iso = getattr(date, "isoformat", None)
    return iso() if callable(iso) else str(date)


def create_telegram_application(
    *,
    token: str,
    owner_chat_id: int,
    message_service: IncomingMessageService,
    batch_seconds: float = 20.0,
    delivery_retry_seconds: float = 30.0,
    catchup_idle_seconds: float = 0.0,
    catchup_max_seconds: float = 30.0,
) -> Application:
    if not token.strip():
        raise ValueError("Telegram bot token must not be empty.")
    if batch_seconds < 0:
        raise ValueError("Message batch interval must not be negative.")
    if delivery_retry_seconds <= 0:
        raise ValueError("Delivery retry interval must be positive.")
    pending_batches: dict[int, _PendingBatch] = {}
    delivery_lock = asyncio.Lock()
    retry_task: asyncio.Task[None] | None = None
    live_enabled = catchup_idle_seconds <= 0
    last_ingest_at = time.monotonic()

    async def _send(bot, *, chat_id: int, text: str, reply_markup=None):
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await bot.send_message(**kwargs)

    async def _deliver_suggestion(bot, suggestion) -> bool:
        async with delivery_lock:
            if suggestion.recommendation_id:
                is_linked = getattr(message_service, "is_owner_delivery_linked", None)
                if is_linked is not None and is_linked(suggestion.recommendation_id, owner_chat_id):
                    logger.info("event=owner_delivery_already_linked recommendation_id=%s", suggestion.recommendation_id)
                    return True
            try:
                sent = await _send(bot, chat_id=owner_chat_id, text=format_owner_message(suggestion))
                message_id = getattr(sent, "message_id", None)
                if message_id is None:
                    logger.warning("event=owner_delivery_pending reason=no_message_id recommendation_id=%s", suggestion.recommendation_id)
                    return False
                if suggestion.recommendation_id:
                    message_service.record_owner_delivery(recommendation_id=suggestion.recommendation_id, owner_chat_id=owner_chat_id, owner_message_id=message_id)
                logger.info("event=owner_suggestion_sent recommendation_id=%s owner_message_id=%s", suggestion.recommendation_id, message_id)
                return True
            except NetworkError:
                logger.warning("event=owner_delivery_pending reason=telegram_network_error recommendation_id=%s", suggestion.recommendation_id)
                return False
            except Exception:
                logger.exception("event=owner_delivery_pending reason=delivery_error recommendation_id=%s", suggestion.recommendation_id)
                return False

    async def _retry_pending_deliveries(bot) -> None:
        pending = getattr(message_service, "pending_suggestions", None)
        if pending is None:
            return
        try:
            suggestions = pending(owner_chat_id)
        except Exception:
            logger.exception("event=owner_delivery_scan_failed")
            return
        for suggestion in suggestions:
            await _deliver_suggestion(bot, suggestion)

    async def _delivery_retry_loop(application: Application) -> None:
        while True:
            await _retry_pending_deliveries(application.bot)
            await asyncio.sleep(delivery_retry_seconds)

    async def _wait_for_ingest_idle() -> None:
        if catchup_idle_seconds <= 0:
            return
        deadline = time.monotonic() + max(catchup_max_seconds, catchup_idle_seconds)
        while time.monotonic() < deadline:
            if time.monotonic() - last_ingest_at >= catchup_idle_seconds:
                return
            await asyncio.sleep(0.1)

    async def _run_catchup(bot) -> None:
        catchup = getattr(message_service, "catch_up", None)
        if catchup is None:
            return
        pending_ids = getattr(message_service, "pending_client_chat_ids", None)
        for _ in range(5):
            suggestions = await catchup()
            for suggestion in suggestions:
                await _deliver_suggestion(bot, suggestion)
            leftover = pending_ids() if pending_ids is not None else []
            if not leftover:
                return
            await asyncio.sleep(0.05)

    async def _post_init(application: Application) -> None:
        nonlocal retry_task, live_enabled, last_ingest_at
        last_ingest_at = time.monotonic()
        await _wait_for_ingest_idle()
        await _run_catchup(application.bot)
        live_enabled = True
        retry_task = asyncio.create_task(_delivery_retry_loop(application), name="agentbridge-delivery-retry")

    async def _post_stop(application: Application) -> None:
        nonlocal retry_task
        if retry_task is None:
            return
        retry_task.cancel()
        await asyncio.gather(retry_task, return_exceptions=True)
        retry_task = None

    def _ingest(update: Update, chat_id: int, is_owner_chat: bool) -> IncomingMessage:
        nonlocal last_ingest_at
        message = update.effective_message
        sender = update.effective_user
        reply = getattr(message, "reply_to_message", None) if message is not None else None
        item = IncomingMessage(
            sender_name=getattr(sender, "full_name", "") if sender else "",
            text=getattr(message, "text", "") if message is not None else "",
            update_id=update.update_id,
            message_id=getattr(message, "message_id", None),
            sender_id=getattr(sender, "id", None),
            telegram_date=_telegram_date(message),
            reply_to_message_id=getattr(reply, "message_id", None),
        )
        ingest = getattr(message_service, "ingest_telegram_message", None)
        if ingest is not None and message is not None:
            ingest(
                update_id=update.update_id,
                chat_id=chat_id,
                message_id=item.message_id if item.message_id is not None else update.update_id,
                sender_id=item.sender_id,
                sender_name=item.sender_name,
                telegram_date=item.telegram_date or "",
                text=item.text,
                reply_to_message_id=item.reply_to_message_id,
                is_owner_chat=is_owner_chat,
            )
            last_ingest_at = time.monotonic()
        return item

    async def _analyze_chat(chat_id: int, messages: list[IncomingMessage], bot) -> None:
        processor = getattr(message_service, "process_pending_chat", None)
        try:
            if processor is not None:
                recommendation = await processor(chat_id, mode="live")
            else:
                recommendation = await message_service.handle_messages(chat_id, messages)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event=client_batch_failed chat_id=%s", chat_id)
            await _send(bot, chat_id=owner_chat_id, text=f"AgentBridge не смог подготовить рекомендацию. Чат ID: {chat_id}. Проверьте журнал приложения.")
            return
        if recommendation is not None:
            await _deliver_suggestion(bot, recommendation)

    async def send_batch(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await asyncio.sleep(batch_seconds)
            batch = pending_batches.pop(chat_id, None)
            if batch is None:
                return
            await _analyze_chat(chat_id, batch.messages, context.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            pending_batches.pop(chat_id, None)
            logger.exception("event=client_batch_failed chat_id=%s", chat_id)
            await _send(context.bot, chat_id=owner_chat_id, text=f"AgentBridge не смог подготовить рекомендацию. Чат ID: {chat_id}. Проверьте журнал приложения.")

    async def _send_memory_proposal(bot, proposal: MemoryProposal) -> None:
        await _send(bot, chat_id=owner_chat_id, text=format_memory_proposal(proposal), reply_markup=_memory_confirmation_keyboard(proposal.draft_id))

    async def owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        sender = update.effective_user
        if message is None or chat is None or chat.id != owner_chat_id or not message.text:
            return False
        reply = getattr(message, "reply_to_message", None)
        reply_id = getattr(reply, "message_id", None)
        mentions = _mentions_bot(message, context.bot)
        replied_sender = getattr(reply, "from_user", None) if reply is not None else None
        bot_id = getattr(context.bot, "id", None)
        replied_to_this_bot = bool(
            replied_sender is not None
            and getattr(replied_sender, "is_bot", False)
            and (bot_id is None or getattr(replied_sender, "id", None) == bot_id)
        )
        if mentions:
            question_handler = getattr(message_service, "handle_owner_question_reply", None)
            if replied_to_this_bot and question_handler is not None and reply_id is not None:
                result = await question_handler(
                    owner_chat_id, reply_id, getattr(sender, "id", 0),
                    getattr(sender, "full_name", "") or "Неизвестный владелец", message.text, update.update_id,
                )
                if result is not None:
                    await _handle_question_result(context.bot, result)
                    return True
            query = getattr(message_service, "handle_owner_query", None)
            if query is None:
                return True
            logger.info("event=owner_query_received owner_message_id=%s update_id=%s", getattr(message, "message_id", None), update.update_id)
            answer = await query(message.text, reply_to_message_id=reply_id, update_id=update.update_id)
            if answer:
                await _send(context.bot, chat_id=owner_chat_id, text=answer)
            return True
        if reply_id is None:
            return True
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
        question_handler = getattr(message_service, "handle_owner_question_reply", None)
        if question_handler is not None:
            result = await question_handler(
                owner_chat_id, reply_id, getattr(sender, "id", 0),
                getattr(sender, "full_name", "") or "Неизвестный владелец", message.text, update.update_id,
            )
            if result is not None:
                await _handle_question_result(context.bot, result)
                return True
        context_handler = getattr(message_service, "handle_owner_context", None)
        is_context_command = getattr(message_service, "is_memory_context_command", lambda _: False)
        if is_context_command(message.text):
            proposal = await context_handler(
                owner_chat_id, reply_id, getattr(sender, "id", 0),
                getattr(sender, "full_name", "") or "Неизвестный владелец", message.text, update.update_id,
            ) if context_handler is not None else None
            if proposal is None:
                await _send(
                    context.bot, chat_id=owner_chat_id,
                    text="Не удалось подготовить контекст. Для проектного контекста чат должен быть привязан к проекту; ответьте на актуальную рекомендацию бота.",
                )
                return True
            await _send_memory_proposal(context.bot, proposal)
            return True
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

    async def _handle_question_result(bot, result: QuestionReplyResult) -> None:
        if result.memory_proposal is not None:
            await _send_memory_proposal(bot, result.memory_proposal)
        if result.suggestion is not None:
            await _deliver_suggestion(bot, result.suggestion)

    async def queue_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        chat = update.effective_chat
        if message is None or chat is None or not message.text:
            return
        sender = update.effective_user
        if sender is not None and sender.is_bot:
            logger.info("event=telegram_message_ignored reason=bot chat_id=%s", chat.id)
            return
        item = _ingest(update, chat.id, chat.id == owner_chat_id)
        if await owner_reply(update, context):
            return
        if not live_enabled:
            logger.info("event=client_message_deferred_catchup chat_id=%s update_id=%s", chat.id, update.update_id)
            return
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
        await _telegram_try("answer_callback", query.answer())
        try:
            kind, action, raw_id = (query.data or "").split(":", 2)
            draft_id = int(raw_id)
        except (ValueError, AttributeError):
            return
        if kind == "memory":
            await _telegram_try("clear_confirmation_buttons", query.edit_message_reply_markup(reply_markup=None))
            if action == "no":
                rejected = message_service.reject_memory(draft_id)
                await _send(context.bot, chat_id=owner_chat_id, text="Сохранение контекста отменено." if rejected else "Этот контекст уже обработан или больше недоступен.")
                return
            proposal = message_service.confirm_memory(draft_id)
            await _send(context.bot, chat_id=owner_chat_id, text="Контекст сохранён." if proposal is not None else "Этот контекст уже обработан или больше недоступен.")
            return
        if action == "no":
            prompt = await _send(context.bot, chat_id=owner_chat_id, text="Что я понял неправильно? Ответьте на это сообщение уточнением.")
            if prompt is not None and getattr(prompt, "message_id", None) is not None:
                message_service.mark_awaiting_clarification(draft_id, prompt.message_id)
            await _telegram_try("clear_confirmation_buttons", query.edit_message_reply_markup(reply_markup=None))
            return
        result = await message_service.confirm_learning(draft_id)
        await _telegram_try("clear_confirmation_buttons", query.edit_message_reply_markup(reply_markup=None))
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

    application = Application.builder().token(token.strip()).post_init(_post_init).post_stop(_post_stop).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, queue_message))
    application.add_handler(CallbackQueryHandler(learning_callback, pattern=r"^(learn|memory):(yes|no):\d+$"))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("undo", undo_command))
    return application
