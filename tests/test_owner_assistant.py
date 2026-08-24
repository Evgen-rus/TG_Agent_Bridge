from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from telegram.error import TimedOut

from agentbridge.application import MemoryProposal, OwnerQueryResult, QuestionReplyResult, Suggestion
from agentbridge.telegram.bot import create_telegram_application


@dataclass
class FakeSentMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    id: int = 777
    username: str = "agentbridge"

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSentMessage(1000 + len(self.sent))

    async def send_chat_action(self, **kwargs):
        self.actions.append(kwargs)


@dataclass
class FakeContext:
    bot: FakeBot


@dataclass
class FakeReply:
    message_id: int
    from_user: "FakeUser | None" = None


@dataclass
class FakeMessage:
    text: str
    reply_to_message: FakeReply | None = None
    message_id: int = 50


@dataclass
class FakeChat:
    id: int


@dataclass
class FakeUser:
    id: int = 42
    full_name: str = "Owner"
    is_bot: bool = False


@dataclass
class FakeUpdate:
    effective_message: FakeMessage
    effective_chat: FakeChat
    effective_user: FakeUser
    update_id: int = 123


@dataclass
class OwnerAssistantService:
    query_calls: list[dict] = field(default_factory=list)
    question_calls: list[dict] = field(default_factory=list)
    client_calls: list = field(default_factory=list)

    async def handle_messages(self, telegram_chat_id, messages):
        self.client_calls.append((telegram_chat_id, messages))
        raise AssertionError("Owner messages must not enter the client pipeline")

    async def handle_owner_query(self, text, reply_to_message_id=None, update_id=None):
        self.query_calls.append({"text": text, "reply_to_message_id": reply_to_message_id, "update_id": update_id})
        return "Сейчас ждём расчёт от нас."

    async def handle_owner_question_reply(self, owner_chat_id, reply_to_message_id, author_user_id, author_name, answer, update_id=None):
        if "@" in answer:
            return None
        self.question_calls.append({"reply_to_message_id": reply_to_message_id, "answer": answer})
        return QuestionReplyResult(
            Suggestion("Acme", "Alice", "Need a quote", "Need a fact", "Here it is", 9, -100123456),
            MemoryProposal(3, "Acme", answer, "chat"),
        )

    async def clarify_feedback(self, *args, **kwargs):
        return None

    async def handle_owner_feedback(self, *args, **kwargs):
        raise AssertionError("Knowledge-gap replies must not go through ordinary learning")


@dataclass
class ClarifyingAssistantService:
    query_calls: list[dict] = field(default_factory=list)
    continued: list[dict] = field(default_factory=list)
    attached: list[tuple[int, int]] = field(default_factory=list)
    question_calls: list[dict] = field(default_factory=list)
    client_calls: list = field(default_factory=list)

    async def handle_messages(self, telegram_chat_id, messages):
        self.client_calls.append((telegram_chat_id, messages))
        raise AssertionError("Owner messages must not enter the client pipeline")

    async def handle_owner_query(self, text, reply_to_message_id=None, update_id=None):
        self.query_calls.append({"text": text, "reply_to_message_id": reply_to_message_id, "update_id": update_id})
        return OwnerQueryResult("Уточните, о каком чате речь. Сейчас подключены: [LR225] ОптоБель.", 11)

    def attach_owner_query_prompt(self, prompt_id: int, owner_message_id: int) -> None:
        self.attached.append((prompt_id, owner_message_id))

    async def continue_owner_query(self, owner_message_id: int, text: str, update_id=None):
        self.continued.append({"reply_to_message_id": owner_message_id, "text": text})
        return "Для ОптоБель: звоните по методике дозвона."

    async def handle_owner_question_reply(self, *args, **kwargs):
        return None

    async def clarify_feedback(self, *args, **kwargs):
        return None

    async def handle_owner_feedback(self, *args, **kwargs):
        raise AssertionError("Chat clarification replies must not go through ordinary learning")


def _callback(application):
    return application.handlers[0][0].callback


@pytest.mark.asyncio
async def test_plain_owner_message_without_mention_does_not_call_the_agent() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _callback(application)(FakeUpdate(FakeMessage("Коллеги, кто на связи?"), FakeChat(7654321), FakeUser()), FakeContext(bot))
    assert service.query_calls == []
    assert service.client_calls == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_owner_mention_asks_the_assistant() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _callback(application)(
        FakeUpdate(FakeMessage("@agent что мы сейчас ждём от Acme Support?"), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert service.client_calls == []
    assert service.query_calls[0]["text"] == "@agent что мы сейчас ждём от Acme Support?"
    assert bot.sent[0]["chat_id"] == 7654321
    assert "ждём расчёт" in bot.sent[0]["text"]
    assert bot.actions
    assert bot.actions[0]["chat_id"] == 7654321
    assert bot.actions[0]["action"] == "typing"


@pytest.mark.asyncio
async def test_owner_rick_prefix_asks_the_assistant_without_telegram_tag() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321,
        message_service=service, batch_seconds=0,
    )
    bot = FakeBot()
    await _callback(application)(
        FakeUpdate(FakeMessage("Рик, привет"), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert service.query_calls[0]["text"] == "Рик, привет"
    assert "ждём расчёт" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_reply_to_bot_mention_is_an_owner_query() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _callback(application)(
        FakeUpdate(
            FakeMessage("@agentbridge почему такой ответ?", FakeReply(9001, FakeUser(777, "AgentBridge", True))),
            FakeChat(7654321),
            FakeUser(),
        ),
        FakeContext(bot),
    )
    assert service.query_calls[0]["reply_to_message_id"] == 9001
    assert service.question_calls == []
    assert service.client_calls == []
    assert all(item["chat_id"] == 7654321 for item in bot.sent)


@pytest.mark.asyncio
async def test_reply_to_proactive_question_is_linked_to_the_client() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _callback(application)(
        FakeUpdate(
            FakeMessage("Да, тест ещё действует", FakeReply(9001, FakeUser(777, "AgentBridge", True))),
            FakeChat(7654321),
            FakeUser(),
        ),
        FakeContext(bot),
    )
    assert service.question_calls == [{"reply_to_message_id": 9001, "answer": "Да, тест ещё действует"}]
    assert service.client_calls == []
    assert all(item["chat_id"] == 7654321 for item in bot.sent)
    assert any("Сохранить?" in item["text"] for item in bot.sent)


@pytest.mark.asyncio
async def test_reply_to_which_chat_clarification_continues_the_query() -> None:
    service = ClarifyingAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    context = FakeContext(bot)
    await _callback(application)(
        FakeUpdate(FakeMessage("@agentbridge подскажи как дозваниваться"), FakeChat(7654321), FakeUser(), 201),
        context,
    )
    assert service.attached == [(11, 1001)]
    assert "Уточните, о каком чате речь" in bot.sent[0]["text"]
    await _callback(application)(
        FakeUpdate(
            FakeMessage("[LR225] ОптоБель", FakeReply(1001, FakeUser(777, "AgentBridge", True))),
            FakeChat(7654321),
            FakeUser(),
            202,
        ),
        context,
    )
    assert service.continued == [{"reply_to_message_id": 1001, "text": "[LR225] ОптоБель"}]
    assert service.question_calls == []
    assert "методике дозвона" in bot.sent[-1]["text"]


class TimeoutBot(FakeBot):
    async def send_message(self, **kwargs):
        raise TimedOut("proxy timeout")


@pytest.mark.asyncio
async def test_owner_query_telegram_timeout_does_not_crash_handler() -> None:
    service = OwnerAssistantService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = TimeoutBot()
    await _callback(application)(
        FakeUpdate(FakeMessage("@agentbridge что сейчас с Acme Support?"), FakeChat(7654321), FakeUser(), 301),
        FakeContext(bot),
    )
    assert service.query_calls
    assert bot.sent == []
