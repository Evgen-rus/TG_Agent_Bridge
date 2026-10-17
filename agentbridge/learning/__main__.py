"""Run product learning once; systemd owns scheduling, the existing bot owns delivery."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv


@contextmanager
def product_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if os.name == 'posix':
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('learning_already_running') from None
        else:
            import msvcrt
            stream.seek(0)
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('learning_already_running') from None
        try:
            yield
        finally:
            if os.name == 'posix':
                fcntl.flock(stream, fcntl.LOCK_UN)
            else:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def main():
    parser = argparse.ArgumentParser(description='Extract reviewable Wiki candidates from saved history')
    parser.add_argument('--product', choices=['rick', 'sally'], required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--force', action='store_true', help='Run manual extraction while scheduling is disabled')
    parser.add_argument('--migrate-only', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    if os.name == 'posix':
        os.umask(0o077)
    load_dotenv(root / '.env')
    if args.product == 'sally':
        from agentbridge.sales.settings import SalesSettings
        env_file = args.env_file or Path(os.getenv('AGENTBRIDGE_WEB_SALES_ENV_FILE', str(root / 'runtime/sales_buddy/.env')))
        load_dotenv(env_file, override=True)
        settings = SalesSettings.load(root, env_file)
        database = settings.runtime / 'sales.sqlite3'
        model, effort, codex_home = settings.model, settings.effort, settings.codex_home
    else:
        if args.env_file:
            load_dotenv(args.env_file, override=True)
        from agentbridge.settings import Settings
        settings = Settings.from_env(root)
        database = settings.database_path
        model, effort = settings.owner_codex_model, settings.owner_codex_reasoning_effort
        codex_home = Path(os.getenv('CODEX_HOME') or Path.home() / '.codex')
    if not database.is_file():
        raise RuntimeError('learning_source_database_missing')
    enabled = os.getenv(args.product.upper() + '_LEARNING_ENABLED', 'false').lower() in {'1', 'true', 'yes', 'on'}
    if not args.migrate_only and not args.force and not enabled:
        print(json.dumps({'status': 'disabled', 'product': args.product}))
        return
    from .store import LearningStore
    from .sources import read_sources
    from .runner import run_learning
    with product_lock(database.parent / (args.product + '-learning.lock')):
        store = LearningStore(database)
        if args.migrate_only:
            print(json.dumps({'status': 'migrated', 'product': args.product}))
            return
        from .provider import LearningProvider
        # Provider transport error strings may contain private source paths.
        logging.getLogger('agentbridge.agents.codex').disabled = True
        logging.getLogger('openai_codex').setLevel(logging.CRITICAL)
        cwd = database.parent / 'learning-turns'
        cwd.mkdir(exist_ok=True)
        snapshot = store.snapshot()
        rules = snapshot.get('rules', []) if isinstance(snapshot, dict) else []
        provider = LearningProvider(model=model, reasoning_effort=effort,
                                    cwd=cwd, codex_home=codex_home, rules=rules)
        result = run_learning(store, provider, lambda cutoff: read_sources(
            database, args.product, cutoff, root / 'runtime/web/access.sqlite3'))
        print(json.dumps({'status': 'ok', 'product': args.product, **result}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
