"""Long-polling Telegram adapter for client suggestions and owner learning."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from agentbridge.application import IncomingMessage, MemoryProposal, OnboardingDraftProposal, OnboardingNotice, OwnerQueryResult, QuestionReplyResult
from agentbridge.media import DEFAULT_MEDIA_TTL_SECONDS, MediaRef, delete_media_file, purge_expired_media
from agentbridge.transcribe import TranscriptionError, transcribe_audio_file
from .formatter import (
    format_learning_proposal,
    format_memory_proposal,
    format_onboarding_draft,
    format_onboarding_notice,
    format_owner_message,
    format_rules,
)
from .media import describe_message_media, has_client_content, materialize_media_ref, message_text
from .polling import HeartbeatHTTPXRequest, PollingHeartbeat, PollingWatchdog

_ADMIN_STATUSES = {"administrator", "creator"}
_LEFT_STATUSES = {"left", "kicked"}
_TYPING_REFRESH_SECONDS = 4.0
logger = logging.getLogger(__name__)


class IncomingMessageService(Protocol):
    def handle_messages(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> Awaitable["Suggestion | None"]: ...


@dataclass
class _PendingBatch:
    messages: list[IncomingMessage]
    task: asyncio.Task[None]


async def transcribe_pending_voice_messages(
    message_service,
    bot,
    *,
    media_dir: Path | None,
    api_key: str,
    model: str,
    telegram_chat_id: int,
) -> None:
    """Транскрибирует pending-голосовые чата, записывая текст в SQLite по update_id.

    Ошибка API или пустой аудиосигнал фиксируются плейсхолдером: сообщение не должно
    вечно блокировать батч чата. Локальная копия аудио остаётся до конца эпизода -
    её удалит стандартный _discard_local_media вместе с прочими вложениями.
    """
    finder = getattr(message_service, "pending_voice_messages", None)
    saver = getattr(message_service, "save_voice_transcript", None)
    if finder is None or saver is None or not api_key.strip():
        return
    downloader = getattr(message_service, "pending_media_downloads", None)
    path_saver = getattr(message_service, "set_message_media_path", None)
    for row in finder(telegram_chat_id):
        path: Path | None = None
        if media_dir is not None and downloader is not None and path_saver is not None:
            ref = MediaRef(kind="voice", file_id=row.telegram_file_id, filename="voice.ogg", mime="audio/ogg")
            path = await materialize_media_ref(bot, ref, media_dir, row.chat_id, row.message_id)
            if path is not None:
                path_saver(row.id, str(path))
        if path is None:
            # Скачать не удалось - оставляем на следующую попытку (батч/рестарт).
            logger.warning("event=voice_transcribe_failed reason=no_local_file update_id=%s", row.update_id)
            continue
        try:
            transcript = await transcribe_audio_file(path, api_key=api_key, model=model)
        except TranscriptionError as exc:
            logger.error("event=voice_transcribe_failed reason=%s update_id=%s", exc, row.update_id)
            transcript = ""
        except Exception:
            logger.exception("event=voice_transcribe_failed reason=unexpected update_id=%s", row.update_id)
            transcript = ""
        if transcript.strip():
            saver(row.update_id, transcript)
        else:
            # Пустой аудиофайл, шум или сбой API: плейсхолдер вместо вечного ожидания.
            saver(row.update_id, "не удалось распознать речь")


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


def _onboarding_keyboard(onboarding_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Да, сохранить", callback_data=f"onboard:yes:{onboarding_id}"),
        InlineKeyboardButton("Нет, уточнить", callback_data=f"onboard:no:{onboarding_id}"),
    ]])


def _mentions_bot(message, bot) -> bool:
    text = getattr(message, "text", None) or ""
    if _mentions_bot_from_text(text, bot):
        return True
    bot_id = getattr(bot, "id", None)
    for entity in getattr(message, "entities", None) or []:
        mentioned = getattr(entity, "user", None)
        if mentioned is not None and bot_id is not None and getattr(mentioned, "id", None) == bot_id:
            return True
    return False


def _mentions_bot_from_text(text: str | None, bot) -> bool:
    """Находит текстовый тег или обращение в начале голосового транскрипта."""
    if not text:
        return False
    names = {"agent"}
    username = getattr(bot, "username", None)
    if username:
        names.add(str(username).casefold())
    lowered = text.casefold().strip()
    if any(f"@{name}" in lowered for name in names):
        return True
    spoken_names = {"агент", "рик"}
    if username:
        spoken_names.add(str(username).casefold().replace("_", " "))
    for name in spoken_names:
        if not lowered.startswith(name):
            continue
        tail = lowered[len(name):]
        if not tail or not tail[0].isalnum():
            return True
    return False


def _plain_owner_text(text: str | None, bot) -> str:
    """Снимает ведущий @тег бота, чтобы «Общий контекст:» работал и с тегом, и без."""
    stripped = (text or "").strip()
    names = ["agent"]
    username = getattr(bot, "username", None)
    if username:
        names.append(str(username))
    lowered = stripped.casefold()
    for name in names:
        marker = f"@{str(name).casefold()}"
        if lowered.startswith(marker):
            return stripped[len(marker):].strip()
    return stripped


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
    media_dir: Path | None = None,
    media_ttl_seconds: int = DEFAULT_MEDIA_TTL_SECONDS,
    openai_api_key: str = "",
    transcription_model: str = "gpt-4o-mini-transcribe",
    polling_hard_timeout_seconds: float = 30.0,
    polling_watchdog_seconds: float = 15.0,
    polling_stall_seconds: float = 90.0,
    polling_restart_timeout_seconds: float = 30.0,
    polling_bootstrap_retries: int = 5,
) -> Application:
    if not token.strip():
        raise ValueError("Telegram bot token must not be empty.")
    if batch_seconds < 0:
        raise ValueError("Message batch interval must not be negative.")
    if delivery_retry_seconds <= 0:
        raise ValueError("Delivery retry interval must be positive.")
    if polling_stall_seconds <= polling_hard_timeout_seconds:
        raise ValueError("Telegram polling stall threshold must exceed the hard polling timeout.")
    heartbeat = PollingHeartbeat()
    watchdog = PollingWatchdog(
        heartbeat=heartbeat,
        check_interval_seconds=polling_watchdog_seconds,
        stall_seconds=polling_stall_seconds,
        restart_timeout_seconds=polling_restart_timeout_seconds,
        allowed_updates=("message", "callback_query", "my_chat_member"),
        bootstrap_retries=polling_bootstrap_retries,
    )
    pending_batches: dict[int, _PendingBatch] = {}
    delivery_lock = asyncio.Lock()
    voice_tasks: dict[int, set[asyncio.Task[None]]] = {}
    owner_voice_retry_targets: dict[int, int] = {}
    retry_task: asyncio.Task[None] | None = None
    recovery_task: asyncio.Task[None] | None = None
    watchdog_task: asyncio.Task[None] | None = None
    live_enabled = catchup_idle_seconds <= 0
    last_ingest_at = time.monotonic()

    async def _send(bot, *, chat_id: int, text: str, reply_markup=None):
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await bot.send_message(**kwargs)

    async def _send_typing(bot, chat_id: int) -> None:
        send_action = getattr(bot, "send_chat_action", None)
        if send_action is None:
            return
        await _telegram_try("typing", send_action(chat_id=chat_id, action="typing"))

    @asynccontextmanager
    async def _typing(bot, chat_id: int):
        # Не ждём sendChatAction: через прокси он может зависнуть, а Codex должен стартовать сразу.
        async def _loop() -> None:
            while True:
                await _send_typing(bot, chat_id)
                await asyncio.sleep(_TYPING_REFRESH_SECONDS)

        task = asyncio.create_task(_loop(), name="agentbridge-typing")
        try:
            await asyncio.sleep(0)
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _deliver_owner_query(bot, result) -> None:
        if result is None:
            return
        text = result.text if isinstance(result, OwnerQueryResult) else str(result)
        prompt_id = result.prompt_id if isinstance(result, OwnerQueryResult) else None
        delivery_id = result.delivery_id if isinstance(result, OwnerQueryResult) else None
        if not text:
            return
        save = getattr(message_service, "save_pending_owner_query_delivery", None)
        if delivery_id is None and save is not None:
            delivery_id = save(text, prompt_id)
        try:
            sent = await _send(bot, chat_id=owner_chat_id, text=text)
        except NetworkError:
            logger.warning("event=owner_query_delivery_pending reason=telegram_network_error")
            return
        except Exception:
            logger.exception("event=owner_query_delivery_pending reason=delivery_error")
            return
        message_id = getattr(sent, "message_id", None)
        record = getattr(message_service, "record_owner_query_delivery", None)
        if delivery_id is not None and record is not None and message_id is not None:
            record(delivery_id, message_id)
        attach = getattr(message_service, "attach_owner_query_prompt", None)
        if prompt_id is not None and attach is not None and message_id is not None:
            attach(prompt_id, message_id)

    async def _try_continue_owner_query(
        bot,
        update: Update,
        reply_id: int | None,
        owner_text: str | None,
    ) -> bool:
        handler = getattr(message_service, "continue_owner_query", None)
        message = update.effective_message
        if handler is None or reply_id is None or message is None or not owner_text:
            return False
        async with _typing(bot, owner_chat_id):
            result = await handler(reply_id, owner_text, update.update_id)
            if result is None:
                return False
            await _deliver_owner_query(bot, result)
            return True

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
        if media_dir is not None:
            purge_expired_media(media_dir, media_ttl_seconds)
        pending = getattr(message_service, "pending_suggestions", None)
        if pending is not None:
            try:
                suggestions = pending(owner_chat_id)
            except Exception:
                logger.exception("event=owner_delivery_scan_failed")
                suggestions = []
            for suggestion in suggestions:
                await _deliver_suggestion(bot, suggestion)
        pending_queries = getattr(message_service, "pending_owner_query_deliveries", None)
        if pending_queries is not None:
            try:
                query_results = pending_queries()
            except Exception:
                logger.exception("event=owner_query_delivery_scan_failed")
                query_results = []
            for query_result in query_results:
                await _deliver_owner_query(bot, query_result)
        notices = getattr(message_service, "pending_onboarding_notices", None)
        if notices is None:
            return
        try:
            pending_notices = notices()
        except Exception:
            logger.exception("event=onboarding_notice_scan_failed")
            return
        for notice in pending_notices:
            await _deliver_onboarding_notice(bot, notice)

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
        if pending_ids is not None:
            for chat_id in pending_ids():
                await _transcribe_pending_voice(chat_id, bot)
                await _hydrate_chat_media(bot, chat_id)
        for _ in range(5):
            suggestions = await catchup()
            for suggestion in suggestions:
                await _deliver_suggestion(bot, suggestion)
            leftover = pending_ids() if pending_ids is not None else []
            if not leftover:
                return
            await asyncio.sleep(0.05)

    async def _wait_until_polling_ready(application: Application) -> None:
        # post_init runs before start_polling; wait until Telegram can actually deliver backlog.
        deadline = time.monotonic() + catchup_max_seconds
        while time.monotonic() < deadline:
            updater = getattr(application, "updater", None)
            updater_running = bool(updater is not None and getattr(updater, "running", False))
            if updater_running or getattr(application, "running", False):
                nested = time.monotonic() + 5
                while time.monotonic() < nested and not getattr(application, "running", False):
                    if updater is not None and getattr(updater, "running", False) and getattr(application, "running", False):
                        break
                    await asyncio.sleep(0.05)
                return
            await asyncio.sleep(0.05)

    async def _startup_recovery(application: Application) -> None:
        nonlocal live_enabled
        try:
            await _wait_until_polling_ready(application)
            await _wait_for_ingest_idle()
            await _run_catchup(application.bot)
        finally:
            live_enabled = True
        leftover = getattr(message_service, "pending_client_chat_ids", None)
        if leftover is None:
            return
        for chat_id in leftover():
            await _analyze_chat(chat_id, [], application.bot)

    async def _post_init(application: Application) -> None:
        nonlocal retry_task, recovery_task, watchdog_task, last_ingest_at, live_enabled
        last_ingest_at = time.monotonic()
        if catchup_idle_seconds > 0:
            live_enabled = False
            recovery_task = asyncio.create_task(_startup_recovery(application), name="agentbridge-startup-recovery")
        else:
            live_enabled = True
        retry_task = asyncio.create_task(_delivery_retry_loop(application), name="agentbridge-delivery-retry")
        watchdog_task = asyncio.create_task(watchdog.run(application), name="agentbridge-polling-watchdog")

    async def _post_stop(application: Application) -> None:
        nonlocal retry_task, recovery_task, watchdog_task
        for task in (recovery_task, retry_task, watchdog_task):
            if task is None:
                continue
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        recovery_task = None
        retry_task = None
        watchdog_task = None

    def _ingest(update: Update, chat_id: int, is_owner_chat: bool) -> IncomingMessage:
        nonlocal last_ingest_at
        message = update.effective_message
        sender = update.effective_user
        reply = getattr(message, "reply_to_message", None) if message is not None else None
        media = describe_message_media(message)
        item = IncomingMessage(
            sender_name=getattr(sender, "full_name", "") if sender else "",
            text=message_text(message),
            update_id=update.update_id,
            message_id=getattr(message, "message_id", None),
            sender_id=getattr(sender, "id", None),
            telegram_date=_telegram_date(message),
            reply_to_message_id=getattr(reply, "message_id", None),
            media_kind=media.kind if media is not None else "",
            telegram_file_id=media.file_id if media is not None else "",
            media_mime=media.mime if media is not None else "",
            media_filename=media.filename if media is not None else "",
            media_group_id=media.media_group_id if media is not None else "",
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
                media_kind=item.media_kind,
                telegram_file_id=item.telegram_file_id,
                media_mime=item.media_mime,
                media_filename=item.media_filename,
                media_group_id=item.media_group_id,
            )
            last_ingest_at = time.monotonic()
        return item

    async def _hydrate_chat_media(bot, chat_id: int) -> None:
        pending = getattr(message_service, "pending_media_downloads", None)
        saver = getattr(message_service, "set_message_media_path", None)
        if pending is None or saver is None or media_dir is None:
            return
        for row in pending(chat_id):
            ref = MediaRef(
                kind=row.media_kind or "document",
                file_id=row.telegram_file_id,
                filename=row.media_filename,
                mime=row.media_mime,
                media_group_id=row.media_group_id,
            )
            path = await materialize_media_ref(bot, ref, media_dir, row.chat_id, row.message_id)
            if path is not None:
                saver(row.id, str(path))

    def _track_voice_task(chat_id: int, task: asyncio.Task[None]) -> None:
        voice_tasks.setdefault(chat_id, set()).add(task)
        task.add_done_callback(lambda done: voice_tasks.get(chat_id, set()).discard(done))

    async def _transcribe_pending_voice(chat_id: int, bot) -> None:
        await transcribe_pending_voice_messages(
            message_service, bot,
            media_dir=media_dir, api_key=openai_api_key, model=transcription_model,
            telegram_chat_id=chat_id,
        )

    async def _send_owner_voice_error(bot, text: str, reply_target_id: int | None) -> None:
        sent = await _send(bot, chat_id=owner_chat_id, text=text)
        error_message_id = getattr(sent, "message_id", None)
        if reply_target_id is None or error_message_id is None:
            return
        owner_voice_retry_targets[int(error_message_id)] = reply_target_id
        while len(owner_voice_retry_targets) > 100:
            owner_voice_retry_targets.pop(next(iter(owner_voice_retry_targets)))

    async def _transcribe_owner_voice(message, bot, reply_target_id: int | None) -> str:
        """Скачивает голосовое владельца и возвращает распознанный текст ('' при сбое)."""
        voice = getattr(message, "voice", None)
        file_id = str(getattr(voice, "file_id", "") or "")
        if not openai_api_key.strip():
            await _send_owner_voice_error(
                bot,
                "Голосовые не разобрать: не задан OPENAI_API_KEY. Напишите текстом.",
                reply_target_id,
            )
            return ""
        if media_dir is None or not file_id:
            return ""
        ref = MediaRef(kind="voice", file_id=file_id, filename="voice.ogg", mime="audio/ogg")
        path = await materialize_media_ref(
            bot, ref, media_dir, owner_chat_id, int(getattr(message, "message_id", 0) or 0),
        )
        if path is None:
            logger.warning("event=owner_voice_failed reason=no_local_file")
            await _send_owner_voice_error(
                bot,
                "Не смог скачать голосовое. Повторите запись ответом на это сообщение или напишите текстом.",
                reply_target_id,
            )
            return ""
        try:
            transcript = await transcribe_audio_file(path, api_key=openai_api_key, model=transcription_model)
        except Exception:
            logger.exception("event=owner_voice_failed reason=transcription_error")
            transcript = ""
        finally:
            delete_media_file(path)
        if not transcript.strip():
            logger.warning("event=owner_voice_failed reason=empty_transcript")
            await _send_owner_voice_error(
                bot,
                "Речи в записи не услышал. Повторите запись ответом на это сообщение или напишите текстом.",
                reply_target_id,
            )
            return ""
        logger.info("event=owner_voice_transcribed chars=%d", len(transcript))
        return transcript

    async def _wait_voice_tasks(chat_id: int) -> None:
        tasks = [task for task in voice_tasks.get(chat_id, set()) if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _voice_worker(chat_id: int, bot, update_id: int) -> None:
        """Фоновая транскрибация одного голосового; результат пишется в SQLite."""
        try:
            await _transcribe_pending_voice(chat_id, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event=voice_worker_failed chat_id=%s update_id=%s", chat_id, update_id)

    async def _analyze_chat(chat_id: int, messages: list[IncomingMessage], bot) -> None:
        processor = getattr(message_service, "process_pending_chat", None)
        try:
            await _wait_voice_tasks(chat_id)
            await _transcribe_pending_voice(chat_id, bot)
            await _hydrate_chat_media(bot, chat_id)
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

    async def _deliver_onboarding_notice(bot, notice: OnboardingNotice) -> None:
        if not notice.needs_delivery:
            return
        sent = await _send(bot, chat_id=owner_chat_id, text=format_onboarding_notice(notice))
        message_id = getattr(sent, "message_id", None)
        recorder = getattr(message_service, "record_onboarding_notice", None)
        if recorder is not None and message_id is not None:
            recorder(notice.onboarding_id, message_id)

    async def _deliver_onboarding_draft(bot, proposal: OnboardingDraftProposal) -> None:
        sent = await _send(
            bot,
            chat_id=owner_chat_id,
            text=format_onboarding_draft(proposal),
            reply_markup=_onboarding_keyboard(proposal.onboarding_id),
        )
        message_id = getattr(sent, "message_id", None)
        recorder = getattr(message_service, "record_onboarding_draft_message", None)
        if recorder is not None and message_id is not None:
            recorder(proposal.onboarding_id, message_id)

    def _member_status(member) -> str:
        return str(getattr(member, "status", "") or "").strip().casefold()

    async def _bot_is_group_admin(bot, chat_id: int) -> bool:
        bot_id = getattr(bot, "id", None)
        getter = getattr(bot, "get_chat_member", None)
        if bot_id is None or getter is None:
            return False
        try:
            member = await getter(chat_id, bot_id)
        except Exception:
            logger.warning("event=telegram_admin_check_failed chat_id=%s", chat_id)
            return False
        return _member_status(member) in _ADMIN_STATUSES

    async def _discover_unknown_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> OnboardingNotice | None:
        starter = getattr(message_service, "begin_unconfigured_chat", None)
        if starter is None:
            return None
        chat = update.effective_chat
        if chat is None:
            return None
        if not await _bot_is_group_admin(context.bot, chat.id):
            return None
        title = getattr(chat, "title", None) or getattr(chat, "full_name", "") or str(chat.id)
        sender = update.effective_user
        return starter(
            telegram_chat_id=chat.id,
            chat_title=str(title),
            added_by_name=getattr(sender, "full_name", "") if sender else "",
            added_by_id=getattr(sender, "id", None) if sender else None,
        )

    async def my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        event = getattr(update, "my_chat_member", None)
        if event is None:
            return
        already = getattr(message_service, "is_update_processed", None)
        if already is not None and already(update.update_id):
            return
        chat = getattr(event, "chat", None) or update.effective_chat
        if chat is None or chat.id == owner_chat_id:
            return
        status = _member_status(getattr(event, "new_chat_member", None))
        if status in _LEFT_STATUSES:
            cancel = getattr(message_service, "cancel_unconfigured_chat", None)
            if cancel is not None:
                cancel(chat.id)
            marker = getattr(message_service, "mark_update_processed", None)
            if marker is not None:
                marker(update.update_id)
            return
        if status not in _ADMIN_STATUSES:
            return
        starter = getattr(message_service, "begin_unconfigured_chat", None)
        if starter is None:
            return
        title = getattr(chat, "title", None) or getattr(chat, "full_name", "") or str(chat.id)
        added = getattr(event, "from_user", None)
        notice = starter(
            telegram_chat_id=chat.id,
            chat_title=str(title),
            added_by_name=getattr(added, "full_name", "") if added else "",
            added_by_id=getattr(added, "id", None) if added else None,
            update_id=update.update_id,
        )
        if notice is not None and notice.needs_delivery:
            await _deliver_onboarding_notice(context.bot, notice)

    async def owner_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        message = update.effective_message
        chat = update.effective_chat
        sender = update.effective_user
        if message is None or chat is None or chat.id != owner_chat_id:
            return False
        is_voice = getattr(message, "voice", None) is not None and not message.text
        if not message.text and not is_voice:
            return False
        reply = getattr(message, "reply_to_message", None)
        raw_reply_id = getattr(reply, "message_id", None)
        reply_id = owner_voice_retry_targets.pop(raw_reply_id, raw_reply_id)
        # python-telegram-bot делает Message неизменяемым, поэтому транскрипт
        # живёт в локальной переменной, а не в message.text.
        owner_text = message.text
        if is_voice:
            async with _typing(context.bot, owner_chat_id):
                transcript = await _transcribe_owner_voice(message, context.bot, reply_id)
            if not transcript:
                return True
            owner_text = transcript
            mentions = _mentions_bot_from_text(owner_text, context.bot)
        else:
            mentions = _mentions_bot(message, context.bot)
        replied_sender = getattr(reply, "from_user", None) if reply is not None else None
        bot_id = getattr(context.bot, "id", None)
        replied_to_this_bot = bool(
            replied_sender is not None
            and getattr(replied_sender, "is_bot", False)
            and (bot_id is None or getattr(replied_sender, "id", None) == bot_id)
        )
        if replied_to_this_bot and reply_id is not None:
            onboard_brief = getattr(message_service, "handle_onboarding_brief", None)
            if onboard_brief is not None:
                async with _typing(context.bot, owner_chat_id):
                    draft = await onboard_brief(
                        owner_chat_id, reply_id,
                        getattr(sender, "full_name", "") or "Неизвестный владелец",
                        owner_text, update.update_id,
                    )
                    if draft is not None:
                        await _deliver_onboarding_draft(context.bot, draft)
                        return True
        command_text = _plain_owner_text(owner_text, context.bot)
        is_global_command = getattr(message_service, "is_global_memory_command", lambda _: False)
        if is_global_command(command_text):
            # Префикс сам вызывает бота: тег не обязателен, клиентский чат не нужен.
            context_handler = getattr(message_service, "handle_owner_context", None)
            proposal = await context_handler(
                owner_chat_id, reply_id if replied_to_this_bot else None, getattr(sender, "id", 0),
                getattr(sender, "full_name", "") or "Неизвестный владелец", command_text, update.update_id,
            ) if context_handler is not None else None
            if proposal is None:
                await _send(
                    context.bot, chat_id=owner_chat_id,
                    text="Напишите факт сразу после «Общий контекст:».",
                )
                return True
            await _send_memory_proposal(context.bot, proposal)
            return True
        if mentions:
            question_handler = getattr(message_service, "handle_owner_question_reply", None)
            if replied_to_this_bot and question_handler is not None and reply_id is not None:
                async with _typing(context.bot, owner_chat_id):
                    result = await question_handler(
                        owner_chat_id, reply_id, getattr(sender, "id", 0),
                        getattr(sender, "full_name", "") or "Неизвестный владелец", owner_text, update.update_id,
                    )
                    if result is not None:
                        await _handle_question_result(context.bot, result)
                        return True
            if replied_to_this_bot and await _try_continue_owner_query(
                context.bot, update, reply_id, owner_text,
            ):
                return True
            query = getattr(message_service, "handle_owner_query", None)
            if query is None:
                return True
            logger.info("event=owner_query_received owner_message_id=%s update_id=%s", getattr(message, "message_id", None), update.update_id)
            async with _typing(context.bot, owner_chat_id):
                answer = await query(owner_text, reply_to_message_id=reply_id, update_id=update.update_id)
                await _deliver_owner_query(context.bot, answer)
            return True
        if reply_id is None:
            logger.info(
                "event=owner_message_ignored reason=no_mention_or_reply owner_message_id=%s update_id=%s username=%s",
                getattr(message, "message_id", None), update.update_id, getattr(context.bot, "username", None),
            )
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
            async with _typing(context.bot, owner_chat_id):
                result = await question_handler(
                    owner_chat_id, reply_id, getattr(sender, "id", 0),
                    getattr(sender, "full_name", "") or "Неизвестный владелец", owner_text, update.update_id,
                )
                if result is not None:
                    await _handle_question_result(context.bot, result)
                    return True
        if await _try_continue_owner_query(context.bot, update, reply_id, owner_text):
            return True
        context_handler = getattr(message_service, "handle_owner_context", None)
        is_context_command = getattr(message_service, "is_memory_context_command", lambda _: False)
        if is_context_command(owner_text):
            proposal = await context_handler(
                owner_chat_id, reply_id, getattr(sender, "id", 0),
                getattr(sender, "full_name", "") or "Неизвестный владелец", owner_text, update.update_id,
            ) if context_handler is not None else None
            if proposal is None:
                await _send(
                    context.bot, chat_id=owner_chat_id,
                    text="Не удалось подготовить контекст. Для проектного контекста чат должен быть привязан к проекту; ответьте на актуальную рекомендацию бота.",
                )
                return True
            await _send_memory_proposal(context.bot, proposal)
            return True
        async with _typing(context.bot, owner_chat_id):
            proposal = await message_service.clarify_feedback(reply_id, owner_text, update.update_id)
            if proposal is None:
                proposal = await message_service.handle_owner_feedback(
                    owner_chat_id, reply_id, getattr(sender, "id", 0), getattr(sender, "full_name", "") or "Неизвестный владелец", owner_text, update.update_id
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
        if message is None or chat is None or not has_client_content(message):
            return
        sender = update.effective_user
        if sender is not None and sender.is_bot:
            logger.info("event=telegram_message_ignored reason=bot chat_id=%s", chat.id)
            return
        already = getattr(message_service, "is_update_processed", None)
        if already is not None and already(update.update_id):
            logger.info("event=telegram_update_ignored reason=already_processed update_id=%s", update.update_id)
            return
        if chat.id == owner_chat_id:
            _ingest(update, chat.id, True)
            if await owner_reply(update, context):
                marker = getattr(message_service, "mark_update_processed", None)
                if marker is not None:
                    marker(update.update_id)
            return
        monitored = getattr(message_service, "is_monitored_chat", None)
        if monitored is not None and not monitored(chat.id):
            notice = await _discover_unknown_chat(update, context)
            if notice is None:
                logger.info("event=telegram_message_ignored reason=unknown_chat chat_id=%s", chat.id)
                return
            _ingest(update, chat.id, False)
            if notice.needs_delivery:
                await _deliver_onboarding_notice(context.bot, notice)
            return
        item = _ingest(update, chat.id, False)
        if (item.media_kind or "").casefold() == "voice":
            # Транскрибация идёт параллельно окну батча; send_batch дождётся её.
            task = asyncio.create_task(
                _voice_worker(chat.id, context.bot, item.update_id), name="agentbridge-voice-transcribe",
            )
            _track_voice_task(chat.id, task)
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
        if kind == "onboard":
            await _telegram_try("clear_confirmation_buttons", query.edit_message_reply_markup(reply_markup=None))
            if action == "no":
                rejected = getattr(message_service, "reject_onboarding", lambda _draft: None)(draft_id)
                prompt = await _send(
                    context.bot,
                    chat_id=owner_chat_id,
                    text="Что поправить в описании клиента? Ответьте на это сообщение.",
                )
                marker = getattr(message_service, "mark_onboarding_clarification", None)
                if rejected is not None and marker is not None and prompt is not None and getattr(prompt, "message_id", None) is not None:
                    marker(draft_id, prompt.message_id)
                return
            confirmer = getattr(message_service, "confirm_onboarding", None)
            chat = confirmer(draft_id) if confirmer is not None else None
            if chat is None:
                await _send(context.bot, chat_id=owner_chat_id, text="Этот черновик уже обработан или больше недоступен.")
                return
            await _send(context.bot, chat_id=owner_chat_id, text=f"Чат «{chat.name}» сохранён. Разбираю накопленные сообщения.")
            processor = getattr(message_service, "process_pending_chat", None)
            if processor is None:
                return
            try:
                await _hydrate_chat_media(context.bot, chat.telegram_chat_id)
                suggestion = await processor(chat.telegram_chat_id, mode="catchup")
            except Exception:
                logger.exception("event=onboarding_catchup_failed chat_id=%s", chat.telegram_chat_id)
                await _send(context.bot, chat_id=owner_chat_id, text=f"Чат сохранён, но разбор накопленных сообщений не удался. Чат ID: {chat.telegram_chat_id}.")
                return
            if suggestion is not None:
                await _deliver_suggestion(context.bot, suggestion)
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

    polling_request = HeartbeatHTTPXRequest(heartbeat, polling_hard_timeout_seconds)
    application = (
        Application.builder()
        .token(token.strip())
        .get_updates_request(polling_request)
        .post_init(_post_init)
        .post_stop(_post_stop)
        .build()
    )
    application.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.VOICE | filters.Document.ALL) & ~filters.COMMAND,
        queue_message,
    ))
    application.add_handler(CallbackQueryHandler(learning_callback, pattern=r"^(learn|memory|onboard):(yes|no):\d+$"))
    application.add_handler(ChatMemberHandler(my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("undo", undo_command))
    return application
