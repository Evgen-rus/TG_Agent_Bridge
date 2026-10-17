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


def load_sales_knowledge(root: Path):
    directory = root / 'knowledge' / 'sales_buddy'
    return '\n\n'.join((directory / name).read_text(encoding='utf-8')
                         for name in ('core.md', 'manager_tactics.md'))


def main():
    parser = argparse.ArgumentParser(description='Sales Buddy (Сали), separate from Rick')
    parser.add_argument('--env-file', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    settings = SalesSettings.load(root, args.env_file)
    from dotenv import dotenv_values
    values = dotenv_values(args.env_file or root / '.env')
    os.environ.setdefault('SALLY_WIKI_ENABLED', str(values.get('SALLY_WIKI_ENABLED') or 'false'))
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
    knowledge = load_sales_knowledge(root)
    store = SalesStore(settings.runtime / 'sales.sqlite3')
    provider = SalesCodexProvider(model=settings.model, reasoning_effort=settings.effort,
                                 cwd=cwd, codex_home=settings.codex_home, knowledge=knowledge, store=store)
    service = SalesService(store, provider,
                           system_chat_id=settings.chat_id, system_topic_id=settings.system_topic_id)
    from agentbridge.learning.context import load_store
    service.learning = load_store(store.path)
    provider.learning = service.learning
    app = create_sales_application(settings, service)
    from agentbridge.web.runtime import attach_web
    attach_web(app, root, 'sally', service, settings)
    app.run_polling(drop_pending_updates=False, allowed_updates=['message', 'edited_message', 'callback_query'])
    if app.bot_data.get('sales_polling_fatal'):
        raise RuntimeError('Sales polling recovery failed')


if __name__ == '__main__':
    main()
