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
    codex_model: str = "gpt-5.6-luna"
    codex_reasoning_effort: str = "xhigh"
    owner_codex_model: str = "gpt-5.6-sol"
    owner_codex_reasoning_effort: str = "low"
    sepia_enabled: bool = True
    message_batch_seconds: float = 20.0
    delivery_retry_seconds: float = 30.0
    catchup_idle_seconds: float = 2.0
    catchup_episode_size: int = 40
    telegram_bootstrap_retries: int = 5
    telegram_poll_hard_timeout_seconds: float = 30.0
    telegram_poll_watchdog_seconds: float = 15.0
    telegram_poll_stall_seconds: float = 90.0
    telegram_poll_restart_timeout_seconds: float = 30.0
    media_dir: Path = Path("runtime/media")
    media_ttl_seconds: int = 3600
    openai_api_key: str = ""
    transcription_model: str = "gpt-4o-mini-transcribe"

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
            codex_model=os.getenv("CODEX_MODEL", "gpt-5.6-luna").strip(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "xhigh").strip(),
            owner_codex_model=os.getenv("OWNER_CODEX_MODEL", "gpt-5.6-sol").strip(),
            owner_codex_reasoning_effort=os.getenv("OWNER_CODEX_REASONING_EFFORT", "low").strip(),
            sepia_enabled=os.getenv("SEPIA_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            message_batch_seconds=float(os.getenv("MESSAGE_BATCH_SECONDS", "20")),
            delivery_retry_seconds=float(os.getenv("DELIVERY_RETRY_SECONDS", "30")),
            catchup_idle_seconds=float(os.getenv("CATCHUP_IDLE_SECONDS", "2")),
            catchup_episode_size=int(os.getenv("CATCHUP_EPISODE_SIZE", "40")),
            telegram_bootstrap_retries=int(os.getenv("TELEGRAM_BOOTSTRAP_RETRIES", "5")),
            telegram_poll_hard_timeout_seconds=float(os.getenv("TELEGRAM_POLL_HARD_TIMEOUT_SECONDS", "30")),
            telegram_poll_watchdog_seconds=float(os.getenv("TELEGRAM_POLL_WATCHDOG_SECONDS", "15")),
            telegram_poll_stall_seconds=float(os.getenv("TELEGRAM_POLL_STALL_SECONDS", "90")),
            telegram_poll_restart_timeout_seconds=float(os.getenv("TELEGRAM_POLL_RESTART_TIMEOUT_SECONDS", "30")),
            media_dir=root / os.getenv("MEDIA_DIR", "runtime/media"),
            media_ttl_seconds=int(os.getenv("MEDIA_TTL_SECONDS", "3600")),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            transcription_model=os.getenv("TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe").strip(),
        )
