from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
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
    daily_report_enabled: bool = True
    daily_report_time: str = "07:30"
    daily_report_timezone: str = "Europe/Moscow"
    codex_model: str = "gpt-6-luna"
    codex_reasoning_effort: str = "xhigh"
    owner_codex_model: str = "gpt-6-luna"
    owner_codex_reasoning_effort: str = "xhigh"
    owner_timezone: str = "Asia/Novosibirsk"
    codex_session_timezone: str = "Europe/Moscow"
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
    image_generation_model: str = "gpt-image-2.5-flare"
    image_generation_size: str = "1024x1024"
    image_generation_quality: str = "low"
    openrouter_api_key: str = ""
    owner_voice_enabled: bool = True
    owner_voice_provider_order: tuple[str, ...] = ("openrouter",)
    owner_voice_max_cost_usd: Decimal = Decimal("0")
    openrouter_tts_model: str = "fish-audio/s2.1-pro-free:free"
    openrouter_tts_voice: str = "933563129e564b19a115bedd57b7406a"
    openrouter_tts_timeout_seconds: float = 120.0

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
        try:
            owner_voice_max_cost_usd = Decimal(os.getenv("OWNER_VOICE_MAX_COST_USD", "0").strip())
        except InvalidOperation as exc:
            raise ValueError("OWNER_VOICE_MAX_COST_USD must be a non-negative number") from exc
        if not owner_voice_max_cost_usd.is_finite() or owner_voice_max_cost_usd < 0:
            raise ValueError("OWNER_VOICE_MAX_COST_USD must be a non-negative number")
        owner_voice_provider_order = tuple(
            item.strip() for item in os.getenv("OWNER_VOICE_PROVIDER_ORDER", "openrouter").split(",")
            if item.strip()
        )

        return cls(
            telegram_bot_token=token,
            owner_chat_id=owner_chat_id,
            chats_dir=root / os.getenv("CHATS_DIR", "chats"),
            database_path=root / os.getenv("DATABASE_PATH", "runtime/agentbridge.sqlite3"),
            log_dir=root / os.getenv("LOG_DIR", "runtime/logs"),
            log_retention_days=int(os.getenv("LOG_RETENTION_DAYS", "7")),
            daily_report_enabled=os.getenv("DAILY_REPORT_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            daily_report_time=os.getenv("DAILY_REPORT_TIME", "07:30").strip(),
            daily_report_timezone=os.getenv("DAILY_REPORT_TIMEZONE", "Europe/Moscow").strip(),
            codex_model=os.getenv("CODEX_MODEL", "gpt-6-luna").strip(),
            codex_reasoning_effort=os.getenv("CODEX_REASONING_EFFORT", "xhigh").strip(),
            owner_codex_model=os.getenv("OWNER_CODEX_MODEL", "gpt-6-luna").strip(),
            owner_codex_reasoning_effort=os.getenv("OWNER_CODEX_REASONING_EFFORT", "xhigh").strip(),
            owner_timezone=os.getenv("OWNER_TIMEZONE", "Asia/Novosibirsk").strip() or "Asia/Novosibirsk",
            codex_session_timezone=os.getenv("CODEX_SESSION_TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
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
            image_generation_model=os.getenv("IMAGE_GENERATION_MODEL", "gpt-image-2.5-flare").strip(),
            image_generation_size=os.getenv("IMAGE_GENERATION_SIZE", "1024x1024").strip(),
            image_generation_quality=os.getenv("IMAGE_GENERATION_QUALITY", "low").strip(),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            owner_voice_enabled=os.getenv("OWNER_VOICE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
            owner_voice_provider_order=owner_voice_provider_order,
            owner_voice_max_cost_usd=owner_voice_max_cost_usd,
            openrouter_tts_model=os.getenv("OPENROUTER_TTS_MODEL", "fish-audio/s2.1-pro-free:free").strip(),
            openrouter_tts_voice=os.getenv(
                "OPENROUTER_TTS_VOICE", "933563129e564b19a115bedd57b7406a",
            ).strip(),
            openrouter_tts_timeout_seconds=float(os.getenv("OPENROUTER_TTS_TIMEOUT_SECONDS", "120")),
        )
