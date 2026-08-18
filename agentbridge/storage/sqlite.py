from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3

DEFAULT_CHAT_STATE = {
    "participants": [],
    "summary": "",
    "stage": "",
    "facts": [],
    "decisions": [],
    "agreements": [],
    "commitments": [],
    "waiting_from_client": [],
    "waiting_from_us": [],
    "open_questions": [],
    "risks": [],
    "unknowns": [],
    "next_step": "",
    "updated_at": "",
}

MEMORY_KINDS = (
    "fact",
    "decision",
    "commitment",
    "preference",
    "open_question",
    "rule",
    "assumption",
    "experience",
)


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
    action: str = "reply"
    observation: str = ""
    unknowns: str = ""
    owner_question: str = ""


@dataclass(frozen=True)
class StoredMessage:
    id: int
    update_id: int
    chat_id: int
    message_id: int
    sender_id: int | None
    sender_name: str
    telegram_date: str
    text: str
    reply_to_message_id: int | None
    role: str
    processing_status: str


@dataclass(frozen=True)
class MemoryEntry:
    content: str
    scope: str
    kind: str


@dataclass(frozen=True)
class OwnerQuestion:
    id: int
    telegram_chat_id: int
    recommendation_id: int | None
    question: str
    owner_message_id: int | None
    status: str


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


@dataclass(frozen=True)
class MemoryDraft:
    id: int
    recommendation_id: int
    author_user_id: int
    author_name: str
    content: str
    scope: str
    project_key: str | None
    status: str
    kind: str = "fact"


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
                CREATE TABLE IF NOT EXISTS internal_context_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_internal_context_chat
                    ON internal_context_messages(telegram_chat_id, id DESC);
                CREATE TABLE IF NOT EXISTS memory_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id INTEGER NOT NULL,
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
                    project_key TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    project_key TEXT,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    source_draft_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_draft_id) REFERENCES memory_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_active_memory
                    ON memory_entries(status, scope, telegram_chat_id, project_key);
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    telegram_date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    role TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_pending
                    ON telegram_messages(chat_id, processing_status, id);
                CREATE TABLE IF NOT EXISTS chat_states (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    recommendation_id INTEGER,
                    question TEXT NOT NULL,
                    owner_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_questions_message
                    ON owner_questions(owner_message_id, status);
                CREATE TABLE IF NOT EXISTS experience_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    chat_name TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'experience',
                    source_draft_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experience_chat
                    ON experience_entries(status, telegram_chat_id, id);
                """
            )
            self._upgrade_schema(connection)
            connection.execute(
                "UPDATE telegram_messages SET processing_status='pending' WHERE processing_status='processing'"
            )

    @staticmethod
    def _upgrade_schema(connection: sqlite3.Connection) -> None:
        columns = {
            "recommendations": (
                ("action", "TEXT NOT NULL DEFAULT 'reply'"),
                ("observation", "TEXT NOT NULL DEFAULT ''"),
                ("unknowns", "TEXT NOT NULL DEFAULT ''"),
                ("owner_question", "TEXT NOT NULL DEFAULT ''"),
            ),
            "memory_drafts": (("kind", "TEXT NOT NULL DEFAULT 'fact'"),),
            "memory_entries": (("kind", "TEXT NOT NULL DEFAULT 'fact'"),),
        }
        for table, specs in columns.items():
            existing = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, definition in specs:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

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
            inbox = connection.execute(
                "SELECT processing_status FROM telegram_messages WHERE update_id=?",
                (telegram_update_id,),
            ).fetchone()
            if inbox is not None:
                return inbox["processing_status"] == "processed"
            row = connection.execute(
                "SELECT 1 FROM processed_updates WHERE telegram_update_id = ?",
                (telegram_update_id,),
            ).fetchone()
        return row is not None

    def mark_update_processed(self, telegram_update_id: int) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO processed_updates VALUES (?, ?)", (telegram_update_id, _now()))

    def record_internal_context(self, telegram_chat_id: int, chat_name: str, sender_name: str, message_text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO internal_context_messages
                (telegram_chat_id, chat_name, sender_name, message_text, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (telegram_chat_id, chat_name, sender_name, message_text, _now()),
            )

    def recent_internal_context(self, telegram_chat_id: int, limit: int = 8) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sender_name, message_text FROM internal_context_messages
                WHERE telegram_chat_id=? ORDER BY id DESC LIMIT ?""",
                (telegram_chat_id, limit),
            ).fetchall()
        return [f"{row['sender_name']}: {row['message_text']}" for row in reversed(rows)]

    def create_recommendation(
        self,
        telegram_chat_id: int,
        chat_name: str,
        sender_name: str,
        original_message: str,
        situation: str,
        suggested_reply: str,
        owner_chat_id: int | None = None,
        action: str = "reply",
        observation: str = "",
        unknowns: str = "",
        owner_question: str = "",
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO recommendations
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, action, observation, unknowns, owner_question, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, action, observation, unknowns, owner_question, _now()),
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
            connection.execute(
                """UPDATE owner_questions SET owner_message_id=?
                WHERE recommendation_id=? AND owner_message_id IS NULL""",
                (owner_message_id, recommendation_id),
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
        keys = set(row.keys())
        return RecommendationRecord(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"], chat_name=row["chat_name"],
            sender_name=row["sender_name"], original_message=row["original_message"], situation=row["situation"],
            suggested_reply=row["suggested_reply"], owner_chat_id=row["owner_chat_id"],
            owner_message_id=row["owner_message_id"],
            action=row["action"] if "action" in keys and row["action"] else "reply",
            observation=row["observation"] if "observation" in keys and row["observation"] else "",
            unknowns=row["unknowns"] if "unknowns" in keys and row["unknowns"] else "",
            owner_question=row["owner_question"] if "owner_question" in keys and row["owner_question"] else "",
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

    def create_memory_draft(
        self, recommendation_id: int, author_user_id: int, author_name: str,
        content: str, scope: str, project_key: str | None, kind: str = "fact",
    ) -> MemoryDraft:
        now = _now()
        kind = kind if kind in MEMORY_KINDS else "fact"
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO memory_drafts
                (recommendation_id, author_user_id, author_name, content, scope, project_key, kind, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (recommendation_id, author_user_id, author_name, content, scope, project_key, kind, now, now),
            )
            draft_id = int(cursor.lastrowid)
        return self.get_memory_draft(draft_id)  # type: ignore[return-value]

    def get_memory_draft(self, draft_id: int) -> MemoryDraft | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            return None
        keys = set(row.keys())
        return MemoryDraft(
            id=row["id"], recommendation_id=row["recommendation_id"], author_user_id=row["author_user_id"],
            author_name=row["author_name"], content=row["content"], scope=row["scope"],
            project_key=row["project_key"], status=row["status"],
            kind=row["kind"] if "kind" in keys and row["kind"] else "fact",
        )

    def confirm_memory_draft(self, draft_id: int) -> MemoryDraft | None:
        draft = self.get_memory_draft(draft_id)
        if draft is None or draft.status != "pending":
            return None
        recommendation = self.get_recommendation(draft.recommendation_id)
        if recommendation is None:
            return None
        with self._connect() as connection:
            claimed = connection.execute(
                "UPDATE memory_drafts SET status='confirming', updated_at=? WHERE id=? AND status='pending'",
                (_now(), draft_id),
            )
            if claimed.rowcount != 1:
                return None
            chat_id = recommendation.telegram_chat_id if draft.scope == "chat" else None
            connection.execute(
                """INSERT INTO memory_entries
                (telegram_chat_id, project_key, content, scope, kind, author_user_id, author_name, source_draft_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (chat_id, draft.project_key, draft.content, draft.scope, draft.kind, draft.author_user_id,
                 draft.author_name, draft.id, _now()),
            )
            connection.execute("UPDATE memory_drafts SET status='confirmed', updated_at=? WHERE id=?", (_now(), draft_id))
        return self.get_memory_draft(draft_id)

    def reject_memory_draft(self, draft_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE memory_drafts SET status='rejected', updated_at=? WHERE id=? AND status='pending'",
                (_now(), draft_id),
            )
        return result.rowcount == 1

    def active_memory_texts(self, telegram_chat_id: int, project_key: str | None) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT content FROM memory_entries WHERE status='active' AND (
                    scope='global' OR (scope='chat' AND telegram_chat_id=?)
                    OR (scope='project' AND project_key=?)
                ) ORDER BY id""",
                (telegram_chat_id, project_key),
            ).fetchall()
        return [row["content"] for row in rows]

    def active_memory_entries(self, telegram_chat_id: int, project_key: str | None) -> list[MemoryEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT content, scope, kind FROM memory_entries WHERE status='active' AND (
                    scope='global' OR (scope='chat' AND telegram_chat_id=?)
                    OR (scope='project' AND project_key=?)
                ) ORDER BY id""",
                (telegram_chat_id, project_key),
            ).fetchall()
        return [
            MemoryEntry(
                content=row["content"],
                scope=row["scope"],
                kind=row["kind"] if "kind" in row.keys() and row["kind"] else "fact",
            )
            for row in rows
        ]

    def ingest_telegram_message(
        self,
        *,
        update_id: int,
        chat_id: int,
        message_id: int,
        sender_id: int | None,
        sender_name: str,
        telegram_date: str,
        text: str,
        reply_to_message_id: int | None,
        role: str,
        processing_status: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO telegram_messages (
                    update_id, chat_id, message_id, sender_id, sender_name, telegram_date,
                    text, reply_to_message_id, role, processing_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    update_id, chat_id, message_id, sender_id, sender_name, telegram_date,
                    text, reply_to_message_id, role, processing_status, _now(),
                ),
            )
            return cursor.rowcount == 1

    def reset_stale_processing(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_messages SET processing_status='pending' WHERE processing_status='processing'"
            )

    def pending_chat_ids(self, known_chat_ids: list[int]) -> list[int]:
        if not known_chat_ids:
            return []
        placeholders = ",".join("?" * len(known_chat_ids))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT chat_id FROM telegram_messages
                WHERE processing_status='pending' AND role IN ('client', 'internal')
                AND chat_id IN ({placeholders}) ORDER BY chat_id""",
                known_chat_ids,
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def pending_messages(self, chat_id: int, limit: int | None = None) -> list[StoredMessage]:
        sql = """SELECT * FROM telegram_messages
            WHERE chat_id=? AND processing_status='pending' AND role IN ('client', 'internal')
            ORDER BY id"""
        params: tuple[object, ...] = (chat_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (chat_id, limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._stored_message(row) for row in rows]

    def claim_messages(self, message_ids: list[int]) -> list[StoredMessage]:
        if not message_ids:
            return []
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE telegram_messages SET processing_status='processing'
                WHERE id IN ({placeholders}) AND processing_status='pending'""",
                message_ids,
            )
            rows = connection.execute(
                f"SELECT * FROM telegram_messages WHERE id IN ({placeholders}) AND processing_status='processing' ORDER BY id",
                message_ids,
            ).fetchall()
        return [self._stored_message(row) for row in rows]

    def release_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE telegram_messages SET processing_status='pending'
                WHERE id IN ({placeholders}) AND processing_status='processing'""",
                message_ids,
            )

    def mark_messages_processed(self, messages: list[StoredMessage]) -> None:
        if not messages:
            return
        placeholders = ",".join("?" * len(messages))
        ids = [item.id for item in messages]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE telegram_messages SET processing_status='processed' WHERE id IN ({placeholders})",
                ids,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO processed_updates VALUES (?, ?)",
                [(item.update_id, _now()) for item in messages],
            )

    def recent_messages(self, chat_id: int, limit: int = 20) -> list[StoredMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM telegram_messages WHERE chat_id=? AND role IN ('client', 'internal')
                ORDER BY id DESC LIMIT ?""",
                (chat_id, limit),
            ).fetchall()
        return [self._stored_message(row) for row in reversed(rows)]

    @staticmethod
    def _stored_message(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            id=row["id"], update_id=row["update_id"], chat_id=row["chat_id"], message_id=row["message_id"],
            sender_id=row["sender_id"], sender_name=row["sender_name"], telegram_date=row["telegram_date"],
            text=row["text"], reply_to_message_id=row["reply_to_message_id"], role=row["role"],
            processing_status=row["processing_status"],
        )

    def get_chat_state(self, telegram_chat_id: int) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM chat_states WHERE telegram_chat_id=?",
                (telegram_chat_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_CHAT_STATE)
        try:
            payload = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return dict(DEFAULT_CHAT_STATE)
        state = dict(DEFAULT_CHAT_STATE)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in state:
                    state[key] = value
        return state

    def save_chat_state(self, telegram_chat_id: int, state: dict) -> None:
        merged = dict(DEFAULT_CHAT_STATE)
        for key, value in state.items():
            if key in merged:
                merged[key] = value
        merged["updated_at"] = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_states (telegram_chat_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (telegram_chat_id, json.dumps(merged, ensure_ascii=False), merged["updated_at"]),
            )

    def create_owner_question(self, telegram_chat_id: int, question: str, recommendation_id: int | None) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_questions
                (telegram_chat_id, recommendation_id, question, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)""",
                (telegram_chat_id, recommendation_id, question, _now()),
            )
            return int(cursor.lastrowid)

    def attach_owner_question_message(self, question_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_questions SET owner_message_id=? WHERE id=?",
                (owner_message_id, question_id),
            )

    def get_owner_question_by_message(self, owner_message_id: int) -> OwnerQuestion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM owner_questions WHERE owner_message_id=? AND status='pending'",
                (owner_message_id,),
            ).fetchone()
        if row is None:
            return None
        return OwnerQuestion(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"],
            recommendation_id=row["recommendation_id"], question=row["question"],
            owner_message_id=row["owner_message_id"], status=row["status"],
        )

    def answer_owner_question(self, question_id: int) -> OwnerQuestion | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_questions SET status='answered' WHERE id=? AND status='pending'",
                (question_id,),
            )
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owner_questions WHERE id=?", (question_id,)).fetchone()
        if row is None:
            return None
        return OwnerQuestion(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"],
            recommendation_id=row["recommendation_id"], question=row["question"],
            owner_message_id=row["owner_message_id"], status=row["status"],
        )

    def record_experience(
        self, *, telegram_chat_id: int | None, chat_name: str, situation: str, lesson: str, source_draft_id: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO experience_entries
                (telegram_chat_id, chat_name, situation, lesson, kind, source_draft_id, status, created_at)
                VALUES (?, ?, ?, ?, 'experience', ?, 'active', ?)""",
                (telegram_chat_id, chat_name, situation, lesson, source_draft_id, _now()),
            )

    def recent_experience(self, telegram_chat_id: int, limit: int = 3) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT situation, lesson FROM experience_entries
                WHERE status='active' AND (telegram_chat_id=? OR telegram_chat_id IS NULL)
                ORDER BY id DESC LIMIT ?""",
                (telegram_chat_id, limit),
            ).fetchall()
        return [f"{row['situation']} → {row['lesson']}" for row in rows]
