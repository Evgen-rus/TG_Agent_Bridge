from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest, NetworkError
from telegram import BotCommandScopeChat, BotCommandScopeDefault

from agentbridge.application import AgentBridgeApplication, Suggestion
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application, register_owner_command_menu
from agentbridge.telegram.formatter import format_owner_message, split_owner_message


@dataclass
class FakeService:
    result: Suggestion | None
    calls: list[dict[str, object]] = field(default_factory=list)
    batch_sizes: list[int] = field(default_factory=list)

    async def handle_message(
        self,
        telegram_chat_id: int,
        sender_name: str,
        message: str,
        update_id: int | None = None,
    ) -> Suggestion | None:
        self.calls.append(
            {
                "telegram_chat_id": telegram_chat_id,
                "sender_name": sender_name,
                "message": message,
                "update_id": update_id,
            }
        )
        return self.result

    async def handle_messages(self, telegram_chat_id, messages):
        self.batch_sizes.append(len(messages))
        self.calls.append(
            {
                "telegram_chat_id": telegram_chat_id,
                "sender_name": messages[0].sender_name,
                "message": messages[0].text,
                "update_id": messages[0].update_id,
            }
        )
        return self.result


@dataclass
class FakeBot:
    sent: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str) -> None:
        self.sent.append({"chat_id": chat_id, "text": text})


@dataclass
class RetryingBot:
    failures_remaining: int = 1
    sent: list[dict[str, object]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise NetworkError("temporary Telegram outage")
        self.sent.append({"chat_id": chat_id, "text": text})
        return type("SentMessage", (), {"message_id": 9000 + len(self.sent)})()


@dataclass
class PersistentDeliveryService:
    suggestion: Suggestion
    pending: bool = True

    async def handle_messages(self, telegram_chat_id, messages):
        return self.suggestion

    def pending_suggestions(self, owner_chat_id):
        return [self.suggestion] if self.pending else []

    def is_owner_delivery_linked(self, recommendation_id, owner_chat_id):
        return not self.pending

    def record_owner_delivery(self, recommendation_id, owner_chat_id, owner_message_id):
        self.pending = False


@dataclass
class FakeContext:
    bot: FakeBot


@dataclass
class FakeChat:
    id: int
    title: str = "Acme Support"
    full_name: str = "Acme Support"


@dataclass
class FakeUser:
    full_name: str = "Alice"
    is_bot: bool = False


@dataclass
class FakeMessage:
    text: str


@dataclass
class FakeUpdate:
    effective_message: FakeMessage
    effective_chat: FakeChat
    effective_user: FakeUser
    update_id: int = 123


@dataclass
class PendingChatService:
    suggestion: Suggestion
    modes: list[str] = field(default_factory=list)

    async def process_pending_chat(self, telegram_chat_id, *, mode="live"):
        self.modes.append(mode)
        return self.suggestion

    async def handle_messages(self, telegram_chat_id, messages):
        raise AssertionError("Live processing should use the durable pending-chat path")


def _message_callback(application):
    return application.handlers[0][0].callback


@pytest.mark.asyncio
async def test_suggestion_is_sent_only_to_the_owner() -> None:
    source_chat_id = -100123456
    owner_chat_id = 7654321
    service = FakeService(
        Suggestion(
            chat_name="Acme Support",
            sender_name="Alice",
            original_message="Can I get the docs?",
            situation="Alice needs the documentation.",
            suggested_reply="Yes, I will send the link.",
        )
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=owner_chat_id, message_service=service,
        batch_seconds=0,
    )
    bot = FakeBot()

    await _message_callback(application)(
        FakeUpdate(FakeMessage("Can I get the docs?"), FakeChat(source_chat_id), FakeUser()),
        FakeContext(bot),
    )
    await asyncio.sleep(0.01)

    assert service.calls == [
        {
            "telegram_chat_id": source_chat_id,
            "sender_name": "Alice",
            "message": "Can I get the docs?",
            "update_id": 123,
        }
    ]
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == owner_chat_id
    assert all(item["chat_id"] != source_chat_id for item in bot.sent)


@pytest.mark.asyncio
async def test_ignored_chat_sends_no_message_to_any_chat() -> None:
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=FakeService(None),
        batch_seconds=0,
    )
    await asyncio.sleep(0.01)
    bot = FakeBot()

    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(-100404), FakeUser()), FakeContext(bot)
    )

    assert bot.sent == []


@pytest.mark.asyncio
async def test_provider_failure_notifies_owner_without_exposing_the_error() -> None:
    secret = "TOKEN_FOR_TESTS_ONLY"

    class FailingService:
        async def handle_message(
            self,
            telegram_chat_id: int,
            sender_name: str,
            message: str,
            update_id: int | None = None,
        ) -> Suggestion | None:
            raise RuntimeError(f"Codex authorization failed: {secret}")

        async def handle_messages(self, telegram_chat_id, messages):
            raise RuntimeError(f"Codex authorization failed: {secret}")

    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=FailingService(),
        batch_seconds=0,
    )
    bot = FakeBot()

    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(-100123456), FakeUser()),
        FakeContext(bot),
    )
    await asyncio.sleep(0.01)

    assert [item["chat_id"] for item in bot.sent] == [7654321]
    assert secret not in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_message_from_another_bot_is_ignored() -> None:
    service = FakeService(None)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0,
    )
    bot = FakeBot()

    await _message_callback(application)(
        FakeUpdate(
            FakeMessage("automated message"),
            FakeChat(-100123456),
            FakeUser(full_name="Another Bot", is_bot=True),
        ),
        FakeContext(bot),
    )

    assert service.calls == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_messages_in_same_chat_are_sent_as_one_batch() -> None:
    service = FakeService(
        Suggestion("Acme", "Alice, Bob", "combined", "situation", "reply")
    )
    application = create_telegram_application(
        token="test-token",
        owner_chat_id=7654321,
        message_service=service,
        batch_seconds=0.02,
    )
    bot = FakeBot()
    callback = _message_callback(application)

    await callback(
        FakeUpdate(FakeMessage("first"), FakeChat(-1001), FakeUser("Alice"), 201),
        FakeContext(bot),
    )
    await callback(
        FakeUpdate(FakeMessage("second"), FakeChat(-1001), FakeUser("Bob"), 202),
        FakeContext(bot),
    )
    await asyncio.sleep(0.04)

    assert len(service.calls) == 1
    assert service.batch_sizes == [2]
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_failed_owner_delivery_is_retried_by_delivery_loop() -> None:
    owner_chat_id = 7654321
    suggestion = Suggestion(
        chat_name="Acme",
        sender_name="Alice",
        original_message="Hello",
        situation="Greeting",
        suggested_reply="Hi",
        recommendation_id=12,
    )
    service = PersistentDeliveryService(suggestion)
    application = create_telegram_application(
        token="test-token",
        owner_chat_id=owner_chat_id,
        message_service=service,
        batch_seconds=0,
        delivery_retry_seconds=0.01,
    )
    bot = RetryingBot()
    application.bot = bot

    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(-100123456), FakeUser()),
        FakeContext(bot),
    )
    await asyncio.sleep(0.01)
    assert bot.sent == []
    assert service.pending is True

    await application.post_init(application)
    await asyncio.sleep(0.02)
    await application.post_stop(application)

    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == owner_chat_id
    assert service.pending is False


class LimitedBot(RetryingBot):
    def __init__(self, fail_at=0):
        super().__init__(failures_remaining=0)
        self.attempts = 0
        self.fail_at = fail_at

    async def send_message(self, *, chat_id, text):
        self.attempts += 1
        if len(text.encode("utf-16-le")) // 2 > 4096:
            raise BadRequest("Message is too long")
        if self.attempts == self.fail_at:
            raise NetworkError("temporary failure between parts")
        return await super().send_message(chat_id=chat_id, text=text)


async def _run_delivery_scan(service, bot):
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
    )
    application.bot = bot
    await application.post_init(application)
    try:
        await asyncio.sleep(0.04)
    finally:
        await application.post_stop(application)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [2, 3])
async def test_multipart_recommendation_resumes_after_restart_without_repeating_parts(
    tmp_path, chat_registry, fail_at,
) -> None:
    path = tmp_path / "deliveries.sqlite3"
    store = ChatThreadStore(path)
    recommendation_id = store.create_recommendation(
        telegram_chat_id=-100123456, chat_name="Acme", sender_name="Alice",
        original_message="Большое сообщение 🚀\n" * 500,
        situation="Не хватает данных", suggested_reply="", owner_chat_id=7654321,
        action="ask_owner", owner_question="Какой срок?",
    )
    store.create_owner_question(-100123456, "Какой срок?", recommendation_id)
    service = AgentBridgeApplication(chat_registry, store, SimpleNamespace(), 7654321)
    original = format_owner_message(service.pending_suggestions(7654321)[0])
    expected = split_owner_message(original)
    bot = LimitedBot(fail_at=fail_at)

    await _run_delivery_scan(service, bot)
    assert len(bot.sent) == fail_at - 1
    assert store.get_recommendation(recommendation_id).owner_message_id is None
    assert [row.id for row in store.pending_recommendations(7654321)] == [recommendation_id]

    restarted = ChatThreadStore(path)
    service = AgentBridgeApplication(chat_registry, restarted, SimpleNamespace(), 7654321)
    await _run_delivery_scan(service, bot)
    assert [item["text"] for item in bot.sent] == expected
    assert all(item["chat_id"] == 7654321 for item in bot.sent)
    assert restarted.pending_recommendations(7654321) == []
    final_id = 9000 + len(expected)
    for message_id in range(9001, final_id + 1):
        canonical_id = service.resolve_owner_reply(7654321, message_id)
        assert restarted.get_recommendation_by_owner_message(7654321, canonical_id).id == recommendation_id
        assert restarted.get_owner_question_by_message(canonical_id).recommendation_id == recommendation_id
    await _run_delivery_scan(service, bot)
    assert len(bot.sent) == len(expected)


@pytest.mark.asyncio
async def test_all_parts_sent_before_crash_are_not_resent_when_final_link_is_missing(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "deliveries.sqlite3")
    recommendation_id = store.create_recommendation(
        telegram_chat_id=-100123456, chat_name="Acme", sender_name="Alice",
        original_message="я" * 8000, situation="Ситуация", suggested_reply="Ответ", owner_chat_id=7654321,
    )
    service = AgentBridgeApplication(chat_registry, store, SimpleNamespace(), 7654321)
    parts = split_owner_message(format_owner_message(service.pending_suggestions(7654321)[0]))
    key = f"recommendation:{recommendation_id}"
    store.prepare_owner_delivery_parts(7654321, key, parts)
    for index in range(len(parts)):
        store.record_owner_delivery_part(7654321, key, index, 8000 + index)
    bot = LimitedBot()
    await _run_delivery_scan(service, bot)
    assert bot.sent == []
    assert store.get_recommendation(recommendation_id).owner_message_id == 8000 + len(parts) - 1


@pytest.mark.asyncio
async def test_bad_request_is_not_reported_as_network_failure_or_leaked(caplog) -> None:
    secret = "123456789:abcdefghijklmnopqrstuvwxyz0123456789"

    class RejectingBot:
        async def send_message(self, **kwargs):
            raise BadRequest(f"Message is too long https://api.telegram.org/bot{secret}/sendMessage")

    service = PersistentDeliveryService(Suggestion("Acme", "Alice", "hello", "test", "reply", 12))
    await _run_delivery_scan(service, RejectingBot())
    assert service.pending
    assert "reason=telegram_bad_request" in caplog.text
    assert "reason=message_too_long" in caplog.text
    assert "telegram_network_error" not in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_live_pending_chat_path_still_sends_only_to_the_owner() -> None:
    source_chat_id = -100123456
    owner_chat_id = 7654321
    service = PendingChatService(
        Suggestion("Acme", "Alice", "Hello", "Greeting", "Hi", 4, source_chat_id)
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=owner_chat_id, message_service=service, batch_seconds=0,
    )
    bot = FakeBot()
    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(source_chat_id), FakeUser()),
        FakeContext(bot),
    )
    await asyncio.sleep(0.01)
    assert service.modes == ["live"]
    assert [item["chat_id"] for item in bot.sent] == [owner_chat_id]
    assert all(item["chat_id"] != source_chat_id for item in bot.sent)


@dataclass
class CatchupLifecycleService:
    catchups: list[str] = field(default_factory=list)
    lives: list[str] = field(default_factory=list)
    ingested: list[int] = field(default_factory=list)
    catchup_gate: asyncio.Event = field(default_factory=asyncio.Event)

    def ingest_telegram_message(self, **kwargs):
        self.ingested.append(kwargs["update_id"])
        return True

    async def catch_up(self):
        await self.catchup_gate.wait()
        self.catchups.append("run")
        return []

    def pending_client_chat_ids(self):
        return []

    async def process_pending_chat(self, telegram_chat_id, *, mode="live"):
        self.lives.append(mode)
        return None

    async def handle_messages(self, telegram_chat_id, messages):
        self.lives.append("handle")
        return None


@pytest.mark.asyncio
async def test_startup_defers_live_until_after_polling_and_catchup() -> None:
    service = CatchupLifecycleService()
    application = create_telegram_application(
        token="test-token",
        owner_chat_id=7654321,
        message_service=service,
        batch_seconds=0,
        catchup_idle_seconds=0.15,
        delivery_retry_seconds=5,
    )
    bot = FakeBot()
    application.bot = bot
    application._running = True
    callback = _message_callback(application)

    await callback(FakeUpdate(FakeMessage("one"), FakeChat(-1001), FakeUser(), 201), FakeContext(bot))
    await callback(FakeUpdate(FakeMessage("two"), FakeChat(-1001), FakeUser(), 202), FakeContext(bot))
    assert service.ingested == [201, 202]
    assert service.lives == []
    assert service.catchups == []

    started = time.monotonic()
    await application.post_init(application)
    assert time.monotonic() - started < 0.1
    assert service.catchups == []
    assert service.lives == []

    service.catchup_gate.set()
    await asyncio.sleep(0.25)
    assert service.catchups == ["run"]
    assert service.lives == []
    await application.post_stop(application)


@dataclass
class CommandMenuBot:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def set_my_commands(self, commands, scope=None):
        self.calls.append({"commands": list(commands), "scope": scope})


@dataclass
class FailingCommandMenuBot:
    async def set_my_commands(self, commands, scope=None):
        raise NetworkError("temporary Telegram outage")


@pytest.mark.asyncio
async def test_owner_command_menu_is_scoped_to_owner_chat_only() -> None:
    bot = CommandMenuBot()

    await register_owner_command_menu(bot, 7654321)

    assert len(bot.calls) == 2
    default_call, owner_call = bot.calls
    assert default_call["commands"] == []
    assert isinstance(default_call["scope"], BotCommandScopeDefault)
    assert [(item.command, item.description) for item in owner_call["commands"]] == [
        ("rules", "Показать активные правила"),
        ("undo", "Отменить последнее правило"),
    ]
    assert isinstance(owner_call["scope"], BotCommandScopeChat)
    assert owner_call["scope"].chat_id == 7654321


@pytest.mark.asyncio
async def test_owner_command_menu_failure_does_not_raise() -> None:
    await register_owner_command_menu(FailingCommandMenuBot(), 7654321)
    await register_owner_command_menu(FakeBot(), 7654321)
