from __future__ import annotations

import logging
from pathlib import Path

from .agents.codex import CodexProvider
from .application import AgentBridgeApplication
from .chats.loader import ChatRegistry
from .logging import configure_logging
from .settings import Settings
from .storage.sqlite import ChatThreadStore
from .telegram.bot import create_telegram_application


def main() -> None:
    root = Path.cwd()
    settings = Settings.from_env(root)
    configure_logging(settings.log_dir, settings.log_retention_days)
    registry = ChatRegistry.load(settings.chats_dir)
    store = ChatThreadStore(settings.database_path)
    provider = CodexProvider(
        model=settings.codex_model,
        reasoning_effort=settings.codex_reasoning_effort,
        cwd=root,
    )
    service = AgentBridgeApplication(
        registry, store, provider, settings.owner_chat_id, settings.catchup_episode_size, settings.chats_dir,
    )
    telegram_application = create_telegram_application(
        token=settings.telegram_bot_token,
        owner_chat_id=settings.owner_chat_id,
        message_service=service,
        batch_seconds=settings.message_batch_seconds,
        delivery_retry_seconds=settings.delivery_retry_seconds,
        catchup_idle_seconds=settings.catchup_idle_seconds,
    )
    logging.info("AgentBridge started with %d monitored chat(s)", len(registry))
    telegram_application.run_polling(
        allowed_updates=["message", "callback_query", "my_chat_member"],
        drop_pending_updates=False,
        bootstrap_retries=settings.telegram_bootstrap_retries,
    )


if __name__ == "__main__":
    main()
