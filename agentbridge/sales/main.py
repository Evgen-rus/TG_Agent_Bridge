from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from agentbridge.logging import configure_logging
from .bot import create_sales_application
from .provider import SalesCodexProvider
from .service import SalesService
from .settings import SalesSettings
from .storage import SalesStore


def main():
    parser = argparse.ArgumentParser(description='Sales Buddy (Сали), separate from Rick')
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    settings = SalesSettings.load(root, args.env_file)
    if os.name == 'posix':
        os.umask(0o077)
    settings.runtime.mkdir(parents=True, exist_ok=True)
    settings.codex_home.mkdir(parents=True, exist_ok=True)
    cwd = settings.runtime / 'turns'
    cwd.mkdir(exist_ok=True)
    configure_logging(settings.runtime / 'logs')
    # Shared SDK runner can log transport error text; sales diagnostics contain only types.
    logging.getLogger('agentbridge.agents.codex').disabled = True
    logging.getLogger('openai_codex').setLevel(logging.CRITICAL)
    knowledge = (root / 'knowledge' / 'sales_buddy' / 'core.md').read_text(encoding='utf-8')
    store = SalesStore(settings.runtime / 'sales.sqlite3')
    provider = SalesCodexProvider(model=settings.model, reasoning_effort=settings.effort,
                                 cwd=cwd, codex_home=settings.codex_home, knowledge=knowledge, store=store)
    service = SalesService(store, provider,
                           system_chat_id=settings.chat_id, system_topic_id=settings.system_topic_id)
    app = create_sales_application(settings, service)
    app.run_polling(drop_pending_updates=False, allowed_updates=['message', 'callback_query'])
    if app.bot_data.get('sales_polling_fatal'):
        raise RuntimeError('Sales polling recovery failed')


if __name__ == '__main__':
    main()
