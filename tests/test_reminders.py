from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentbridge.storage.sqlite import ChatThreadStore, ReminderRecord
from agentbridge.telegram.bot import _format_reminder_message, _parse_reminder_args, create_telegram_application


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
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(message_id=100 + len(self.sent))


@dataclass
class ReminderService:
    reminder: ReminderRecord
    sent: bool = False
    created: list[tuple[str, str]] = field(default_factory=list)
    authors: list[dict] = field(default_factory=list)

    def pending_due_reminders(self):
        return [] if self.sent else [self.reminder]

    def mark_reminder_sent(self, reminder_id: int):
        self.sent = reminder_id == self.reminder.id
        return self.sent

    def create_reminder(self, remind_at_utc: str, text: str, **kwargs):
        self.created.append((remind_at_utc, text))
        self.authors.append(kwargs)
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
    effective_user: object = None


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
    assert service.authors == [{}]
    assert bot.sent[0]["chat_id"] == 7654321


def test_reminder_mentions_follow_the_creator_and_keep_old_format() -> None:
    client = "Риолюкс ЕКБ"
    body = "Уточнить у Ильи <готовность> & запуск"
    with_username = ReminderRecord(
        1, 77, "2099-10-11T07:00:00+00:00", body,
        related_chat_id=-11, related_chat_name=client,
        created_by_user_id=5, created_by_username="rickowner", created_by_name="Евгений",
    )
    without_username = ReminderRecord(
        2, 77, "2099-10-11T07:00:00+00:00", body,
        related_chat_id=-11, related_chat_name=client,
        created_by_user_id=9, created_by_username=None, created_by_name="Евгений",
    )
    other_owner = ReminderRecord(
        3, 77, "2099-10-11T08:00:00+00:00", "Свой текст",
        created_by_user_id=8, created_by_username="owner2", created_by_name="Анна",
    )
    legacy = ReminderRecord(4, 77, "2099-10-11T09:00:00+00:00", "Старое напоминание")

    username_text, username_mode = _format_reminder_message(with_username)
    link_text, link_mode = _format_reminder_message(without_username)
    other_text, other_mode = _format_reminder_message(other_owner)
    legacy_text, legacy_mode = _format_reminder_message(legacy)

    assert username_mode is None and username_text == (
        "🔔 @rickowner, напоминание\n\nРиолюкс ЕКБ\n\nУточнить у Ильи <готовность> & запуск"
    )
    assert link_mode == "HTML"
    assert link_text == (
        '🔔 <a href="tg://user?id=9">Евгений</a>, напоминание\n\n'
        "Риолюкс ЕКБ\n\nУточнить у Ильи &lt;готовность&gt; &amp; запуск"
    )
    assert "@owner2" in other_text and "@rickowner" not in other_text and other_mode is None
    assert legacy_text == "🔔 Напоминание\nСтарое напоминание" and legacy_mode is None


def test_legacy_reminder_row_upgrades_without_author(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    connection = __import__("sqlite3").connect(path)
    connection.execute(
        "CREATE TABLE reminders (id INTEGER PRIMARY KEY, owner_chat_id INTEGER NOT NULL, remind_at_utc TEXT NOT NULL, text TEXT NOT NULL, sent_at TEXT)"
    )
    connection.execute(
        "INSERT INTO reminders (owner_chat_id, remind_at_utc, text) VALUES (77, '2099-10-11T07:00:00+00:00', 'Старое')"
    )
    connection.commit()
    connection.close()

    store = ChatThreadStore(path)
    legacy = store.pending_reminders(77)[0]
    assert legacy.text == "Старое"
    assert legacy.related_chat_id is None and legacy.created_by_user_id is None
    created = store.create_reminder(77, "2099-10-12T07:00:00+00:00", "Новое", created_by_user_id=5, created_by_username="rickowner")
    assert store.pending_reminders(77)[-1].id == created


@pytest.mark.asyncio
async def test_remind_command_stores_author_and_reminders_lists_text() -> None:
    reminder = ReminderRecord(7, 7654321, "2099-09-21T02:15:00+00:00", "Проверить LR225")
    service = ReminderService(reminder)
    service.pending_reminders = lambda: [reminder]
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service)
    bot = ReminderBot()
    user = SimpleNamespace(id=5, username="rickowner", full_name="Евгений", is_bot=False)
    update = ReminderUpdate(SimpleNamespace(id=7654321), user)

    await _handler(application, "remind_command")(update, ReminderContext(bot, ["2099-09-21", "09:15", "Проверить", "LR225"]))
    await _handler(application, "reminders_command")(update, ReminderContext(bot))

    assert service.authors == [{"created_by_user_id": 5, "created_by_username": "rickowner", "created_by_name": "Евгений"}]
    assert "Проверить LR225" in bot.sent[1]["text"]


@pytest.mark.asyncio
async def test_due_reminder_mentions_author_and_client() -> None:
    reminder = ReminderRecord(
        1, 7654321, datetime.now(timezone.utc).isoformat(), "Уточнить у Ильи",
        related_chat_id=-11, related_chat_name="Риолюкс ЕКБ",
        created_by_user_id=5, created_by_username="rickowner", created_by_name="Евгений",
    )
    service = ReminderService(reminder)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service, delivery_retry_seconds=0.01,
    )
    bot = ReminderBot()
    application.bot = bot
    await application.post_init(application)
    await asyncio.sleep(0.03)
    await application.post_stop(application)

    assert bot.sent[0]["chat_id"] == 7654321
    assert bot.sent[0]["text"].startswith("🔔 @rickowner, напоминание")
    assert "Риолюкс ЕКБ" in bot.sent[0]["text"]
    assert "parse_mode" not in bot.sent[0]
