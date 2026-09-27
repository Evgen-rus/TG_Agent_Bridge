from __future__ import annotations

import io
import logging

from agentbridge.logging import RedactingFormatter, SecretRedactionFilter, redact_secrets


TOKEN = "8687043294:AAE-RIo5fKUdfQaGThz3aUrqkVffGK3gZEI"


def test_redacts_plain_and_url_embedded_telegram_tokens() -> None:
    result = redact_secrets(f"POST https://api.telegram.org/bot{TOKEN}/getMe")

    assert TOKEN not in result
    assert "bot868704…:AAE-…gZEI/getMe" in result


def test_logging_filter_redacts_token_passed_as_argument() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    logger = logging.getLogger("test.secret-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)

    logger.info("Telegram URL: %s", f"https://api.telegram.org/bot{TOKEN}/getUpdates")

    output = stream.getvalue()
    assert TOKEN not in output
    assert "bot868704…:AAE-…gZEI" in output


def test_formatter_redacts_token_inside_traceback() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(RedactingFormatter("%(levelname)s: %(message)s"))
    logger = logging.getLogger("test.traceback-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)

    try:
        raise RuntimeError(f"failed URL: https://api.telegram.org/bot{TOKEN}/getMe")
    except RuntimeError:
        logger.exception("request failed")

    output = stream.getvalue()
    assert TOKEN not in output
    assert "bot868704…:AAE-…gZEI" in output


def test_codex_failure_reason_field_is_redacted() -> None:
    """reason= в логе падения Codex полезен, но не должен протекать токеном."""
    from agentbridge.agents.codex import _error_reason

    leaky = RuntimeError(
        f"You've hit your usage limit. try again at 11:27 AM. "
        f"context: https://api.telegram.org/bot{TOKEN} key=sk-proj-AAAAAAAAAAAAAAAAAAAAAAA"
    )
    # Причина читается как есть (её видно и в базе), но лог её редактирует.
    assert TOKEN in _error_reason(leaky)

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(SecretRedactionFilter())
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.getLogger("test.codex-reason-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)

    logger.error("event=codex_turn_failed reason=%s", _error_reason(leaky))

    output = stream.getvalue()
    assert TOKEN not in output
    assert "sk-proj-AAAAAAAAAAAAAAAAAAAAAAA" not in output
    # Полезная часть причины остаётся: без неё в логе нечего читать.
    assert "usage limit" in output
