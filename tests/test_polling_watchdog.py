from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest
from telegram.error import TimedOut
from telegram.request import HTTPXRequest

from agentbridge.telegram.polling import HeartbeatHTTPXRequest, PollingHeartbeat, PollingWatchdog


@dataclass
class FakeUpdater:
    heartbeat: PollingHeartbeat
    fail_start: bool = False
    running: bool = True
    stop_calls: int = 0
    start_calls: list[dict[str, object]] = field(default_factory=list)

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False

    async def start_polling(self, **kwargs) -> None:
        self.start_calls.append(kwargs)
        if self.fail_start:
            raise RuntimeError("polling restart failed")
        self.running = True
        self.heartbeat.touch()


@dataclass
class FakeApplication:
    updater: FakeUpdater
    running: bool = True
    bot_data: dict[str, object] = field(default_factory=dict)
    stop_calls: int = 0

    def stop_running(self) -> None:
        self.stop_calls += 1


def _watchdog(heartbeat: PollingHeartbeat) -> PollingWatchdog:
    return PollingWatchdog(
        heartbeat=heartbeat,
        check_interval_seconds=0.005,
        stall_seconds=0.02,
        restart_timeout_seconds=0.1,
        allowed_updates=("message", "callback_query", "my_chat_member"),
        bootstrap_retries=5,
    )


@pytest.mark.asyncio
async def test_hard_polling_deadline_updates_heartbeat(monkeypatch) -> None:
    heartbeat = PollingHeartbeat()
    request = HeartbeatHTTPXRequest(heartbeat, hard_timeout_seconds=0.01)

    async def hang_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(HTTPXRequest, "do_request", hang_forever)

    with pytest.raises(TimedOut, match="hard polling deadline"):
        await request.do_request("https://example.invalid", "POST")

    assert heartbeat.attempts == 1
    assert heartbeat.completions == 1


@pytest.mark.asyncio
async def test_watchdog_restarts_stalled_polling_without_dropping_backlog() -> None:
    now = [0.0]
    heartbeat = PollingHeartbeat(clock=lambda: now[0])
    updater = FakeUpdater(heartbeat)
    application = FakeApplication(updater)
    task = asyncio.create_task(_watchdog(heartbeat).run(application))
    await asyncio.sleep(0.01)

    now[0] = 1.0
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert updater.stop_calls == 1
    assert updater.start_calls == [{
        "allowed_updates": ("message", "callback_query", "my_chat_member"),
        "drop_pending_updates": False,
        "bootstrap_retries": 5,
    }]
    assert application.stop_calls == 0


@pytest.mark.asyncio
async def test_recent_empty_poll_progress_does_not_trigger_restart() -> None:
    now = [0.0]
    heartbeat = PollingHeartbeat(clock=lambda: now[0])
    updater = FakeUpdater(heartbeat)
    application = FakeApplication(updater)
    task = asyncio.create_task(_watchdog(heartbeat).run(application))
    await asyncio.sleep(0.01)

    for value in (0.005, 0.01, 0.015):
        now[0] = value
        heartbeat.mark_finished()
        await asyncio.sleep(0.006)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert updater.stop_calls == 0
    assert updater.start_calls == []


@pytest.mark.asyncio
async def test_failed_polling_restart_requests_fatal_process_shutdown() -> None:
    heartbeat = PollingHeartbeat()
    updater = FakeUpdater(heartbeat, fail_start=True)
    application = FakeApplication(updater)

    recovered = await _watchdog(heartbeat)._restart(application)

    assert recovered is False
    assert application.bot_data["agentbridge_polling_fatal"] == "restart_failed"
    assert application.stop_calls == 1
