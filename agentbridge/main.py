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
    configure_logging()
    root = Path.cwd()
    settings = Settings.from_env(root)
    registry = ChatRegistry.load(settings.chats_dir)
    store = ChatThreadStore(settings.database_path)
    provider = CodexProvider(
        model=settings.codex_model,
        reasoning_effort=settings.codex_reasoning_effort,
        cwd=root,
    )
    service = AgentBridgeApplication(registry, store, provider)
    telegram_application = create_telegram_application(
        token=settings.telegram_bot_token,
        owner_chat_id=settings.owner_chat_id,
        message_service=service,
        batch_seconds=settings.message_batch_seconds,
    )
    logging.info("AgentBridge started with %d monitored chat(s)", len(registry))
    telegram_application.run_polling(
        allowed_updates=["message"],
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
