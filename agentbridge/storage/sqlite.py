from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
import sqlite3
from collections.abc import Iterator


class ChatThreadStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_threads (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    codex_thread_id TEXT,
                    agent_provider TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_updates (
                    telegram_update_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )
                """
            )

    def get_thread_id(self, telegram_chat_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT codex_thread_id FROM chat_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else row["codex_thread_id"]

    def save_thread(
        self,
        telegram_chat_id: int,
        logical_name: str,
        codex_thread_id: str,
        agent_provider: str = "codex",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_threads (
                    telegram_chat_id, logical_name, codex_thread_id,
                    agent_provider, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    logical_name = excluded.logical_name,
                    codex_thread_id = excluded.codex_thread_id,
                    agent_provider = excluded.agent_provider,
                    updated_at = excluded.updated_at
                """,
                (
                    telegram_chat_id,
                    logical_name,
                    codex_thread_id,
                    agent_provider,
                    now,
                    now,
                ),
            )

    def is_update_processed(self, telegram_update_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_updates WHERE telegram_update_id = ?",
                (telegram_update_id,),
            ).fetchone()
        return row is not None

    def mark_update_processed(self, telegram_update_id: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO processed_updates (
                    telegram_update_id, processed_at
                ) VALUES (?, ?)
                """,
                (telegram_update_id, now),
            )
