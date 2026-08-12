from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.application import LearningProposal
from agentbridge.telegram.bot import create_telegram_application


@dataclass
class FakeSentMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    id: int = 777

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
class FakeLearningService:
    feedback_calls: list[dict] = field(default_factory=list)

    async def handle_messages(self, telegram_chat_id, messages):
        raise AssertionError("Owner messages must not enter the client pipeline")

    async def clarify_feedback(self, prompt_message_id, feedback, update_id=None):
        return None

    async def handle_owner_feedback(self, owner_chat_id, reply_to_message_id, author_user_id, author_name, feedback, update_id=None):
        self.feedback_calls.append({
            "owner_chat_id": owner_chat_id,
            "reply_to_message_id": reply_to_message_id,
            "author_user_id": author_user_id,
            "feedback": feedback,
            "update_id": update_id,
        })
        return LearningProposal(7, "Acme", "Write warmer", "Use a warm tone", "client", True)


def _text_callback(application):
    return application.handlers[0][0].callback


@pytest.mark.asyncio
async def test_plain_owner_conversation_is_ignored() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _text_callback(application)(FakeUpdate(FakeMessage("Hello colleagues"), FakeChat(7654321), FakeUser()), FakeContext(bot))
    assert service.feedback_calls == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_reply_to_bot_recommendation_creates_confirmable_proposal() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _text_callback(application)(
        FakeUpdate(FakeMessage("Make it warmer", FakeReply(9001, FakeUser(777, "AgentBridge", True))), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert service.feedback_calls == [{
        "owner_chat_id": 7654321,
        "reply_to_message_id": 9001,
        "author_user_id": 42,
        "feedback": "Make it warmer",
        "update_id": 123,
    }]
    assert len(bot.sent) == 1
    assert bot.sent[0]["chat_id"] == 7654321
    assert "Я понял так" in bot.sent[0]["text"]
    buttons = bot.sent[0]["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in buttons] == ["learn:yes:7", "learn:no:7"]


@pytest.mark.asyncio
async def test_reply_to_old_unlinked_bot_message_explains_why_it_cannot_learn() -> None:
    service = FakeLearningService()

    async def missing(*args, **kwargs):
        return None

    service.handle_owner_feedback = missing
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _text_callback(application)(
        FakeUpdate(FakeMessage("Remember this", FakeReply(8000, FakeUser(777, "AgentBridge", True))), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert len(bot.sent) == 1
    assert "отправлена до обновления" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_reply_to_human_in_owner_group_stays_ordinary_conversation() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _text_callback(application)(
        FakeUpdate(FakeMessage("Agreed", FakeReply(7000, FakeUser(11, "Colleague", False))), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert service.feedback_calls == []
    assert bot.sent == []
