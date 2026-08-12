from __future__ import annotations

import logging
import re
from typing import Any


_TELEGRAM_TOKEN = re.compile(r"(?P<prefix>bot)?(?P<id>\d{6,}):(?P<secret>[A-Za-z0-9_-]{20,})")


def redact_secrets(value: Any) -> Any:
    """Mask Telegram bot tokens while retaining enough text for diagnostics."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        secret = match.group("secret")
        return (
            f"{match.group('prefix') or ''}{match.group('id')[:6]}…:"
            f"{secret[:4]}…{secret[-4:]}"
        )

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


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
