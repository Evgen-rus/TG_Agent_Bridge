from __future__ import annotations

import logging

from agentbridge.logging import configure_logging


def test_configure_logging_creates_daily_file_with_seven_day_window(tmp_path) -> None:
    configure_logging(tmp_path, retention_days=7)
    logging.getLogger("agentbridge.test").info("event=test chat_id=-1001")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_path = tmp_path / "agentbridge.log"
    assert log_path.is_file()
    assert "event=test chat_id=-1001" in log_path.read_text(encoding="utf-8")
    rotating = [handler for handler in logging.getLogger().handlers if hasattr(handler, "backupCount")]
    assert rotating[0].backupCount == 6
