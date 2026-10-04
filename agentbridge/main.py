from __future__ import annotations

import logging
import os
from pathlib import Path
import sys

from .agents.codex import CodexProvider
from .application import AgentBridgeApplication
from .chats.loader import ChatRegistry
from .image_generation import OpenAIImageGenerator
from .logging import OperationalEventHandler, configure_logging
from .settings import Settings
from .speech import OpenRouterSpeechProvider, SpeechProviderRegistry
from .storage.sqlite import (
    CODEX_RECOVERED_NOTICE,
    ChatThreadStore,
    codex_limit_notice,
    codex_limit_reset_local,
)
from .telegram.bot import create_telegram_application


def main() -> None:
    root = Path.cwd()
    settings = Settings.from_env(root)
    configure_logging(settings.log_dir, settings.log_retention_days)
    logging.info("event=process_starting component=application")
    registry = ChatRegistry.load(settings.chats_dir)
    store = ChatThreadStore(settings.database_path)
    logging.getLogger().addHandler(OperationalEventHandler(store.record_operational_event))
    # SQLite — источник истины о лимите, поэтому новый процесс начинает с того
    # состояния, которое пережило рестарт, а не с «лимита никогда не было».
    limit_active = store.codex_usage_limit_active()

    def note_owner_codex_limit(reset_hint: str) -> None:
        """Отразить отказ по лимиту в SQLite и сообщить владельцу ровно один раз.

        Провайдер сюда приходит с фактом отказа, а решение «это тот же лимит или
        новый» принимает storage по сохранённой метке. Локальное состояние
        провайдера в решение не входит намеренно: клиентский и owner-провайдеры
        живут в разных потоках и могут помнить разное, поэтому опираться на их
        память было бы источником рассинхрона.

        Метки жизненного цикла здесь не трогаются: сброс метки восстановления
        при новом лимите storage делает сам, в той же транзакции.
        """
        # Codex печатает время сброса без зоны, поэтому пересчёт опирается на
        # CODEX_SESSION_TIMEZONE и подписывается как предположение.
        local_hint = codex_limit_reset_local(
            reset_hint, settings.owner_timezone,
            session_timezone_name=settings.codex_session_timezone,
        )
        if store.claim_codex_usage_limit(
            reset_hint, codex_limit_notice(local_hint),
            source_timezone_name=settings.codex_session_timezone, local_hint=local_hint,
        ):
            logging.warning("event=codex_usage_limit_notice_queued component=application reset_hint=%s", local_hint or "UNKNOWN")
        else:
            logging.warning("event=codex_usage_limit_refreshed component=application reset_hint=%s", local_hint or "UNKNOWN")

    def note_owner_codex_recovered(turn_started_at: str | None = None) -> None:
        """Снять лимит и сообщить о восстановлении, если лимит ещё активен.

        Снова решает storage: второй провайдер, который помнит старый лимит и
        успевает позже, не пришлёт дубликат и не тронет уже созданное новое
        состояние лимита. `turn_started_at` — момент старта turn: успех,
        начавшийся до нынешнего лимита, восстановлением считаться не может."""
        # Дешёвая предварительная проверка: успешных turn бывает много, а лимит
        # активен редко, поэтому не берём блокировку на запись ради каждого
        # запроса. Решение всё равно принимает storage внутри транзакции, так
        # что предварительная проверка на ответ не влияет.
        if not store.codex_usage_limit_active():
            return
        if store.claim_codex_usage_recovered(CODEX_RECOVERED_NOTICE, turn_started_at=turn_started_at):
            logging.info("event=codex_usage_limit_recovered component=application")
        else:
            logging.info("event=codex_usage_limit_recovery_noop component=application reason=limit_already_cleared")

    provider = CodexProvider(
        model=settings.codex_model,
        reasoning_effort=settings.codex_reasoning_effort,
        cwd=root,
        sepia_enabled=settings.sepia_enabled,
        on_usage_limit=note_owner_codex_limit,
        on_usage_recovered=note_owner_codex_recovered,
        usage_limit_active=limit_active,
    )
    owner_provider = CodexProvider(
        model=settings.owner_codex_model,
        reasoning_effort=settings.owner_codex_reasoning_effort,
        cwd=root,
        sepia_enabled=False,
        on_usage_limit=note_owner_codex_limit,
        on_usage_recovered=note_owner_codex_recovered,
        usage_limit_active=limit_active,
    )
    image_generator = OpenAIImageGenerator(
        settings.openai_api_key,
        model=settings.image_generation_model,
        size=settings.image_generation_size,
        quality=settings.image_generation_quality,
    )
    speech_provider_registry = None
    if settings.owner_voice_enabled and settings.openrouter_api_key:
        speech_provider_registry = SpeechProviderRegistry((
            OpenRouterSpeechProvider(
                settings.openrouter_api_key,
                model=settings.openrouter_tts_model,
                voice=settings.openrouter_tts_voice,
                timeout_seconds=settings.openrouter_tts_timeout_seconds,
            ),
        ))
    from .leadrecord import LeadRecordClient
    leadrecord_client = LeadRecordClient(settings.leadrecord_ssh_target, settings.leadrecord_ssh_identity) if settings.leadrecord_ssh_target else None
    service = AgentBridgeApplication(
        registry, store, provider, settings.owner_chat_id, settings.catchup_episode_size, settings.chats_dir,
        knowledge_dir=root / "knowledge", owner_provider=owner_provider,
        owner_timezone=settings.owner_timezone, image_generator=image_generator,
        leadrecord_client=leadrecord_client,
        generated_media_dir=settings.media_dir / "owner_generated",
        speech_provider_registry=speech_provider_registry,
        owner_voice_provider_order=settings.owner_voice_provider_order,
        owner_voice_max_cost_usd=settings.owner_voice_max_cost_usd,
    )
    telegram_application = create_telegram_application(
        token=settings.telegram_bot_token,
        owner_chat_id=settings.owner_chat_id,
        message_service=service,
        batch_seconds=settings.message_batch_seconds,
        delivery_retry_seconds=settings.delivery_retry_seconds,
        catchup_idle_seconds=settings.catchup_idle_seconds,
        owner_timezone=settings.owner_timezone,
        media_dir=settings.media_dir,
        media_ttl_seconds=settings.media_ttl_seconds,
        openai_api_key=settings.openai_api_key,
        transcription_model=settings.transcription_model,
        polling_hard_timeout_seconds=settings.telegram_poll_hard_timeout_seconds,
        polling_watchdog_seconds=settings.telegram_poll_watchdog_seconds,
        polling_stall_seconds=settings.telegram_poll_stall_seconds,
        polling_restart_timeout_seconds=settings.telegram_poll_restart_timeout_seconds,
        polling_bootstrap_retries=settings.telegram_bootstrap_retries,
        restart_project_root=root,
        restart_python_executable=Path(sys.executable),
        daily_report_enabled=settings.daily_report_enabled,
        daily_report_time=settings.daily_report_time,
        daily_report_timezone=settings.daily_report_timezone,
    )
    previous_unclean = store.start_run()
    if previous_unclean:
        store.queue_operational_notice(
            f"crash:{previous_unclean}",
            f"Я снова на связи. Предыдущий запуск от {previous_unclean} завершился без clean shutdown. "
            "Причину смотрите в журнале и systemd; точный сигнал пока неизвестен.",
        )
        logging.warning("event=previous_run_unclean component=application run_started_at=%s", previous_unclean)
        store.record_operational_event("previous_run_unclean", "WARNING")
    # Лимит мог быть обнаружен до того, как время сброса начало сохраняться.
    # Тогда ключ есть, а причины нет — и уведомление выходит без времени.
    if limit_active and not store.codex_usage_limit_reason():
        logging.warning("event=codex_usage_limit_reset_unknown component=application")
    # Метка лимита пережила рестарт: по ней видно, ждём ли ещё сброса или уже
    # пора проверять модель на ближайшем запросе владельца.
    if limit_active:
        retry = store.codex_usage_limit_retry()
        logging.info(
            "event=codex_usage_limit_restored_state component=application "
            "reset_hint=%s recovery_probe_allowed=%s reason=%s wait_seconds=%d",
            store.codex_usage_limit_reason() or "UNKNOWN",
            str(retry.allowed).lower(), retry.reason, retry.wait_seconds,
        )
    # Метка self-restart в статусе prepared означает, что прошлый процесс
    # принял решение, но не успел дописать результат: systemctl останавливает и
    # его тоже. Раз мы здесь, значит рестарт не состоялся и метку пора закрыть,
    # иначе она навсегда останется невидимой для acknowledge-цикла.
    abandoned = store.fail_abandoned_self_restarts(os.getpid())
    if abandoned:
        logging.warning("event=self_restart_abandoned component=application count=%d", abandoned)
    try:
        telegram_application.run_polling(
            allowed_updates=["message", "callback_query", "my_chat_member"],
            drop_pending_updates=False,
            bootstrap_retries=settings.telegram_bootstrap_retries,
        )
        fatal_polling_reason = telegram_application.bot_data.get("agentbridge_polling_fatal")
        if fatal_polling_reason:
            raise RuntimeError(f"Telegram polling watchdog stopped AgentBridge: {fatal_polling_reason}")
    except Exception:
        event = "process_failed" if telegram_application.bot_data.get("agentbridge_bootstrapped") else "startup_failed"
        logging.exception("event=%s component=application result=failed", event)
        raise
    else:
        logging.info("event=process_stopping component=application result=clean")
        store.stop_run()
        logging.info("event=process_stopped component=application result=clean")


if __name__ == "__main__":
    main()
