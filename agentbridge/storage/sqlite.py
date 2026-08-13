from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class RecommendationRecord:
    id: int
    telegram_chat_id: int
    chat_name: str
    sender_name: str
    original_message: str
    situation: str
    suggested_reply: str
    owner_chat_id: int | None
    owner_message_id: int | None


@dataclass(frozen=True)
class LearningDraft:
    id: int
    recommendation_id: int
    author_user_id: int
    author_name: str
    feedback: str
    understanding: str
    proposed_rule: str | None
    conflict_key: str | None
    scope: str
    regenerate_current: bool
    revision_instruction: str | None
    status: str


@dataclass(frozen=True)
class RuleRecord:
    id: int
    telegram_chat_id: int | None
    chat_name: str
    rule_text: str
    scope: str
    author_name: str
    created_at: str


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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_threads (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    codex_thread_id TEXT,
                    agent_provider TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_updates (
                    telegram_update_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    original_message TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    suggested_reply TEXT NOT NULL,
                    owner_chat_id INTEGER,
                    owner_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_chat_id, owner_message_id)
                );
                CREATE TABLE IF NOT EXISTS learning_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id INTEGER NOT NULL,
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    understanding TEXT NOT NULL,
                    proposed_rule TEXT,
                    conflict_key TEXT,
                    scope TEXT NOT NULL CHECK(scope IN ('client', 'global')),
                    regenerate_current INTEGER NOT NULL,
                    revision_instruction TEXT,
                    status TEXT NOT NULL,
                    clarification_prompt_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE TABLE IF NOT EXISTS learning_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    chat_name TEXT NOT NULL,
                    rule_text TEXT NOT NULL,
                    conflict_key TEXT,
                    scope TEXT NOT NULL CHECK(scope IN ('client', 'global')),
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    source_draft_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    FOREIGN KEY(source_draft_id) REFERENCES learning_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_recommendation_owner_message
                    ON recommendations(owner_chat_id, owner_message_id);
                CREATE INDEX IF NOT EXISTS idx_active_rules
                    ON learning_rules(status, telegram_chat_id);
                """
            )

    def get_thread_id(self, telegram_chat_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT codex_thread_id FROM chat_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else row["codex_thread_id"]

    def save_thread(self, telegram_chat_id: int, logical_name: str, codex_thread_id: str, agent_provider: str = "codex") -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_threads (telegram_chat_id, logical_name, codex_thread_id, agent_provider, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    logical_name=excluded.logical_name, codex_thread_id=excluded.codex_thread_id,
                    agent_provider=excluded.agent_provider, updated_at=excluded.updated_at
                """,
                (telegram_chat_id, logical_name, codex_thread_id, agent_provider, now, now),
            )

    def is_update_processed(self, telegram_update_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM processed_updates WHERE telegram_update_id = ?", (telegram_update_id,)).fetchone()
        return row is not None

    def mark_update_processed(self, telegram_update_id: int) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO processed_updates VALUES (?, ?)", (telegram_update_id, _now()))

    def create_recommendation(
        self,
        telegram_chat_id: int,
        chat_name: str,
        sender_name: str,
        original_message: str,
        situation: str,
        suggested_reply: str,
        owner_chat_id: int | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO recommendations
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, _now()),
            )
            return int(cursor.lastrowid)

    def pending_recommendations(self, owner_chat_id: int) -> list[RecommendationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM recommendations
                WHERE owner_chat_id=? AND owner_message_id IS NULL
                ORDER BY id""",
                (owner_chat_id,),
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def attach_owner_message(self, recommendation_id: int, owner_chat_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE recommendations SET owner_chat_id=?, owner_message_id=? WHERE id=?",
                (owner_chat_id, owner_message_id, recommendation_id),
            )

    def assign_unowned_pending_recommendations(self, owner_chat_id: int) -> None:
        """Recover rows created before durable owner delivery was introduced."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE recommendations SET owner_chat_id=?
                WHERE owner_chat_id IS NULL AND owner_message_id IS NULL""",
                (owner_chat_id,),
            )

    def get_recommendation_by_owner_message(self, owner_chat_id: int, owner_message_id: int) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recommendations WHERE owner_chat_id=? AND owner_message_id=?",
                (owner_chat_id, owner_message_id),
            ).fetchone()
        return None if row is None else self._recommendation(row)

    def get_recommendation(self, recommendation_id: int) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recommendations WHERE id=?", (recommendation_id,)).fetchone()
        return None if row is None else self._recommendation(row)

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> RecommendationRecord:
        return RecommendationRecord(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"], chat_name=row["chat_name"],
            sender_name=row["sender_name"], original_message=row["original_message"], situation=row["situation"],
            suggested_reply=row["suggested_reply"], owner_chat_id=row["owner_chat_id"],
            owner_message_id=row["owner_message_id"],
        )

    def create_learning_draft(self, recommendation_id: int, author_user_id: int, author_name: str, feedback: str, analysis) -> LearningDraft:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO learning_drafts
                (recommendation_id, author_user_id, author_name, feedback, understanding, proposed_rule,
                 conflict_key, scope, regenerate_current, revision_instruction, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (recommendation_id, author_user_id, author_name, feedback, analysis.understanding,
                 analysis.proposed_rule, analysis.conflict_key, analysis.scope, int(analysis.regenerate_current),
                 analysis.revision_instruction, now, now),
            )
            draft_id = int(cursor.lastrowid)
        return self.get_learning_draft(draft_id)  # type: ignore[return-value]

    def get_learning_draft(self, draft_id: int) -> LearningDraft | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM learning_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            return None
        return LearningDraft(
            id=row["id"], recommendation_id=row["recommendation_id"], author_user_id=row["author_user_id"],
            author_name=row["author_name"], feedback=row["feedback"], understanding=row["understanding"],
            proposed_rule=row["proposed_rule"], conflict_key=row["conflict_key"], scope=row["scope"],
            regenerate_current=bool(row["regenerate_current"]), revision_instruction=row["revision_instruction"],
            status=row["status"],
        )

    def replace_learning_draft_analysis(self, draft_id: int, feedback: str, analysis) -> LearningDraft:
        with self._connect() as connection:
            connection.execute(
                """UPDATE learning_drafts SET feedback=?, understanding=?, proposed_rule=?, conflict_key=?,
                scope=?, regenerate_current=?, revision_instruction=?, status='pending',
                clarification_prompt_message_id=NULL, updated_at=? WHERE id=?""",
                (feedback, analysis.understanding, analysis.proposed_rule, analysis.conflict_key, analysis.scope,
                 int(analysis.regenerate_current), analysis.revision_instruction, _now(), draft_id),
            )
        return self.get_learning_draft(draft_id)  # type: ignore[return-value]

    def mark_draft_awaiting_clarification(self, draft_id: int, prompt_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE learning_drafts SET status='clarifying', clarification_prompt_message_id=?, updated_at=? WHERE id=?",
                (prompt_message_id, _now(), draft_id),
            )

    def get_draft_by_clarification_prompt(self, prompt_message_id: int) -> LearningDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM learning_drafts WHERE clarification_prompt_message_id=? AND status='clarifying'",
                (prompt_message_id,),
            ).fetchone()
        return None if row is None else self.get_learning_draft(row["id"])

    def confirm_draft(self, draft_id: int) -> bool:
        draft = self.get_learning_draft(draft_id)
        if draft is None or draft.status != "pending":
            return False
        recommendation = self.get_recommendation(draft.recommendation_id)
        if recommendation is None:
            return False
        now = _now()
        with self._connect() as connection:
            claimed = connection.execute(
                "UPDATE learning_drafts SET status='confirming', updated_at=? WHERE id=? AND status='pending'",
                (now, draft_id),
            )
            if claimed.rowcount != 1:
                return False
            if draft.proposed_rule:
                target_chat_id = None if draft.scope == "global" else recommendation.telegram_chat_id
                if draft.conflict_key:
                    connection.execute(
                        """UPDATE learning_rules SET status='superseded', superseded_at=?
                        WHERE status='active' AND scope=? AND conflict_key=?
                        AND ((telegram_chat_id IS NULL AND ? IS NULL) OR telegram_chat_id=?)""",
                        (now, draft.scope, draft.conflict_key, target_chat_id, target_chat_id),
                    )
                connection.execute(
                    """INSERT INTO learning_rules
                    (telegram_chat_id, chat_name, rule_text, conflict_key, scope, author_user_id,
                     author_name, source_draft_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (target_chat_id, recommendation.chat_name, draft.proposed_rule, draft.conflict_key,
                     draft.scope, draft.author_user_id, draft.author_name, draft.id, now),
                )
            connection.execute("UPDATE learning_drafts SET status='confirmed', updated_at=? WHERE id=?", (now, draft_id))
        return True

    def active_rule_texts(self, telegram_chat_id: int) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT rule_text FROM learning_rules WHERE status='active'
                AND (scope='global' OR telegram_chat_id=?) ORDER BY id""",
                (telegram_chat_id,),
            ).fetchall()
        return [row["rule_text"] for row in rows]

    def list_active_rules(self) -> list[RuleRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM learning_rules WHERE status='active' ORDER BY id DESC").fetchall()
        return [RuleRecord(row["id"], row["telegram_chat_id"], row["chat_name"], row["rule_text"], row["scope"], row["author_name"], row["created_at"]) for row in rows]

    def undo_latest_rule(self) -> RuleRecord | None:
        rules = self.list_active_rules()
        if not rules:
            return None
        rule = rules[0]
        with self._connect() as connection:
            connection.execute("UPDATE learning_rules SET status='undone', superseded_at=? WHERE id=?", (_now(), rule.id))
            if rule.scope and rule.id:
                current = connection.execute("SELECT conflict_key FROM learning_rules WHERE id=?", (rule.id,)).fetchone()
                conflict_key = current["conflict_key"] if current else None
                if conflict_key:
                    previous = connection.execute(
                        """SELECT id FROM learning_rules
                        WHERE status='superseded' AND scope=? AND conflict_key=?
                        AND ((telegram_chat_id IS NULL AND ? IS NULL) OR telegram_chat_id=?)
                        ORDER BY id DESC LIMIT 1""",
                        (rule.scope, conflict_key, rule.telegram_chat_id, rule.telegram_chat_id),
                    ).fetchone()
                    if previous:
                        connection.execute(
                            "UPDATE learning_rules SET status='active', superseded_at=NULL WHERE id=?",
                            (previous["id"],),
                        )
        return rule
