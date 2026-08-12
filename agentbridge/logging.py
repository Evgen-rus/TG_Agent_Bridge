from __future__ import annotations

from logging.handlers import TimedRotatingFileHandler
import logging
from pathlib import Path
import re
from typing import Any

_TELEGRAM_TOKEN = re.compile(r"(?P<prefix>bot)?(?P<id>\d{6,}):(?P<secret>[A-Za-z0-9_-]{20,})")


def redact_secrets(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        secret = match.group("secret")
        return f"{match.group('prefix') or ''}{match.group('id')[:6]}…:{secret[:4]}…{secret[-4:]}"

    return _TELEGRAM_TOKEN.sub(replace, value)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: redact_secrets(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_secrets(value) for value in record.args)
        return True


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def configure_logging(log_dir: Path | None = None, retention_days: int = 7) -> None:
    target_dir = (log_dir or Path.cwd() / "runtime" / "logs").resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    redaction = SecretRedactionFilter()
    console = logging.StreamHandler()
    console.addFilter(redaction)
    console.setFormatter(formatter)
    daily = TimedRotatingFileHandler(
        target_dir / "agentbridge.log", when="midnight", interval=1,
        backupCount=max(0, retention_days - 1), encoding="utf-8", utc=False,
    )
    daily.suffix = "%Y-%m-%d"
    daily.addFilter(redaction)
    daily.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, daily], force=True)
    # Long-poll HTTP success lines add no diagnostic value and otherwise obscure
    # the compact event chain used to investigate AgentBridge behavior.
    logging.getLogger("httpx").setLevel(logging.WARNING)
