from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentbridge.storage.sqlite import ChatThreadStore, ReminderRecord
from agentbridge.telegram.bot import _parse_reminder_args, create_telegram_application


def test_reminder_is_persistent_and_due_once(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "reminders.sqlite3")
    due = "2026-09-21T02:00:00+00:00"
    reminder_id = store.create_reminder(7654321, due, "Проверить запуск LR225")

    assert store.pending_reminders(7654321)[0].id == reminder_id
    assert [item.text for item in store.pending_due_reminders(7654321, due)] == ["Проверить запуск LR225"]
    assert store.mark_reminder_sent(reminder_id)
    assert not store.mark_reminder_sent(reminder_id)
    assert store.pending_due_reminders(7654321, due) == []
    assert ChatThreadStore(tmp_path / "reminders.sqlite3").pending_reminders(7654321) == []


def test_reminder_parser_requires_exact_future_local_time() -> None:
    remind_at_utc, local_label, text = _parse_reminder_args(
        ["2099-09-21", "09:15", "Проверить", "LR225"], "Asia/Novosibirsk",
    )

    assert local_label == "2099-09-21 09:15"
    assert text == "Проверить LR225"
    assert remind_at_utc == "2099-09-21T02:15:00+00:00"


@dataclass
class ReminderBot:
    sent: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str, **kwargs):
        self.sent.append({"chat_id": chat_id, "text": text})
        return SimpleNamespace(message_id=100 + len(self.sent))


@dataclass
class ReminderService:
    reminder: ReminderRecord
    sent: bool = False
    created: list[tuple[str, str]] = field(default_factory=list)

    def pending_due_reminders(self):
        return [] if self.sent else [self.reminder]

    def mark_reminder_sent(self, reminder_id: int):
        self.sent = reminder_id == self.reminder.id
        return self.sent

    def create_reminder(self, remind_at_utc: str, text: str):
        self.created.append((remind_at_utc, text))
        return 7

    def pending_reminders(self):
        return []


@dataclass
class ReminderContext:
    bot: ReminderBot
    args: list[str] = field(default_factory=list)


@dataclass
class ReminderUpdate:
    effective_chat: object


def _handler(application, name: str):
    for handlers in application.handlers.values():
        for handler in handlers:
            if handler.callback.__name__ == name:
                return handler.callback
    raise AssertionError(f"handler not found: {name}")


@pytest.mark.asyncio
async def test_due_reminder_is_sent_to_owner_and_not_repeated() -> None:
    reminder = ReminderRecord(1, 7654321, datetime.now(timezone.utc).isoformat(), "Проверить запуск LR225")
    service = ReminderService(reminder)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        delivery_retry_seconds=0.01,
    )
    bot = ReminderBot()
    application.bot = bot

    await application.post_init(application)
    await asyncio.sleep(0.03)
    await application.post_stop(application)

    assert [item["chat_id"] for item in bot.sent] == [7654321]
    assert "Проверить запуск LR225" in bot.sent[0]["text"]
    assert service.sent


@pytest.mark.asyncio
async def test_remind_command_creates_owner_reminder() -> None:
    reminder = ReminderRecord(7, 7654321, "2099-09-21T02:15:00+00:00", "Проверить LR225")
    service = ReminderService(reminder)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
    )
    bot = ReminderBot()
    command = _handler(application, "remind_command")

    await command(
        ReminderUpdate(SimpleNamespace(id=7654321)),
        ReminderContext(bot, ["2099-09-21", "09:15", "Проверить", "LR225"]),
    )

    assert service.created == [("2099-09-21T02:15:00+00:00", "Проверить LR225")]
    assert bot.sent[0]["chat_id"] == 7654321
