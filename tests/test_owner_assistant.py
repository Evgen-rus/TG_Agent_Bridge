from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.application import MemoryProposal, QuestionReplyResult, Suggestion
from agentbridge.telegram.bot import create_telegram_application


@dataclass
class FakeSentMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    id: int = 777
    username: str = "agentbridge"

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSentMessage(1000 + len(self.sent))


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
