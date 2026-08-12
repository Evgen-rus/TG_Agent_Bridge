from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    owner_chat_id: int
    chats_dir: Path
    database_path: Path
    log_dir: Path
    log_retention_days: int = 7
    codex_model: str = "gpt-5.6-sol"
    codex_reasoning_effort: str = "medium"
    message_batch_seconds: float = 20.0

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or Path.cwd()).resolve()
        load_dotenv(root / ".env")

        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        owner = os.getenv("OWNER_CHAT_ID", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required in .env")
        if not owner:
            raise ValueError("OWNER_CHAT_ID is required in .env")
        try:
            owner_chat_id = int(owner)
        except ValueError as exc:
            raise ValueError("OWNER_CHAT_ID must be an integer") from exc

        return cls(
            telegram_bot_token=token,
            owner_chat_id=owner_chat_id,
            chats_dir=root / os.getenv("CHATS_DIR", "chats"),
            database_path=root / os.getenv("DATABASE_PATH", "runtime/agentbridge.sqlite3"),
            log_dir=root / os.getenv("LOG_DIR", "runtime/logs"),
            log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "7")),
            codex_model=os.getenv("CODEX_MODEL", "gpt-5.6-sol").strip(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "medium").strip(),
            message_batch_seconds=float(os.getenv("MESSAGE_BATCH_SECONDS", "20")),
        )
