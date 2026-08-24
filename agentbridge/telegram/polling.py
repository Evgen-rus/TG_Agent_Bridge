"""Health monitoring and recovery for Telegram long polling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Callable, Sequence

from telegram.error import TimedOut
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)
_HEALTH_LOG_SECONDS = 300.0


@dataclass
class PollingHeartbeat:
    """Monotonic progress marker updated around every Telegram ``getUpdates`` call."""

    clock: Callable[[], float] = time.monotonic
    last_progress_at: float = field(init=False)
    attempts: int = 0
    completions: int = 0

    def __post_init__(self) -> None:
        self.last_progress_at = self.clock()

    def mark_started(self) -> None:
        self.attempts += 1
        self.last_progress_at = self.clock()

    def mark_finished(self) -> None:
        self.completions += 1
        self.last_progress_at = self.clock()

    def touch(self) -> None:
        self.last_progress_at = self.clock()

    def age(self) -> float:
        return max(0.0, self.clock() - self.last_progress_at)


class HeartbeatHTTPXRequest(HTTPXRequest):
    """Dedicated ``getUpdates`` transport with a hard wall-clock deadline."""

    def __init__(self, heartbeat: PollingHeartbeat, hard_timeout_seconds: float) -> None:
        super().__init__()
        if hard_timeout_seconds <= 0:
            raise ValueError("Telegram polling hard timeout must be positive.")
        self.heartbeat = heartbeat
        self.hard_timeout_seconds = hard_timeout_seconds

    async def do_request(self, *args, **kwargs):
        self.heartbeat.mark_started()
        try:
            async with asyncio.timeout(self.hard_timeout_seconds):
                return await super().do_request(*args, **kwargs)
        except TimeoutError as exc:
            raise TimedOut("Telegram getUpdates exceeded the hard polling deadline.") from exc
        finally:
            self.heartbeat.mark_finished()


@dataclass
class PollingWatchdog:
    heartbeat: PollingHeartbeat
    check_interval_seconds: float
    stall_seconds: float
    restart_timeout_seconds: float
    allowed_updates: Sequence[str]
    bootstrap_retries: int
    fatal_state_key: str = "agentbridge_polling_fatal"

    def __post_init__(self) -> None:
        if self.check_interval_seconds <= 0:
            raise ValueError("Telegram polling watchdog interval must be positive.")
        if self.stall_seconds <= self.check_interval_seconds:
            raise ValueError("Telegram polling stall threshold must exceed the watchdog interval.")
        if self.restart_timeout_seconds <= 0:
            raise ValueError("Telegram polling restart timeout must be positive.")

    async def run(self, application) -> None:
        await self._wait_until_running(application)
        last_health_log_at = self.heartbeat.clock()
        while True:
            await asyncio.sleep(self.check_interval_seconds)
            age = self.heartbeat.age()
            if age < self.stall_seconds:
                now = self.heartbeat.clock()
                if now - last_health_log_at >= _HEALTH_LOG_SECONDS:
                    logger.info(
                        "event=telegram_polling_healthy last_progress_age=%.1f attempts=%d completions=%d",
                        age,
                        self.heartbeat.attempts,
                        self.heartbeat.completions,
                    )
                    last_health_log_at = now
                continue
            logger.critical(
                "event=telegram_polling_stalled last_progress_age=%.1f attempts=%d completions=%d",
                age,
                self.heartbeat.attempts,
                self.heartbeat.completions,
            )
            if not await self._restart(application):
                return

    async def _wait_until_running(self, application) -> None:
        while True:
            updater = getattr(application, "updater", None)
            if getattr(application, "running", False) or bool(updater and updater.running):
                self.heartbeat.touch()
                return
            await asyncio.sleep(min(0.1, self.check_interval_seconds))

    async def _restart(self, application) -> bool:
        updater = getattr(application, "updater", None)
        if updater is None:
            return self._fail(application, "missing_updater")
        try:
            if updater.running:
                async with asyncio.timeout(self.restart_timeout_seconds):
                    await updater.stop()
            async with asyncio.timeout(self.restart_timeout_seconds):
                await updater.start_polling(
                    allowed_updates=self.allowed_updates,
                    drop_pending_updates=False,
                    bootstrap_retries=self.bootstrap_retries,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event=telegram_polling_restart_failed")
            return self._fail(application, "restart_failed")
        self.heartbeat.touch()
        logger.warning("event=telegram_polling_restarted")
        return True

    def _fail(self, application, reason: str) -> bool:
        logger.critical("event=telegram_polling_fatal reason=%s", reason)
        application.bot_data[self.fatal_state_key] = reason
        application.stop_running()
        return False
