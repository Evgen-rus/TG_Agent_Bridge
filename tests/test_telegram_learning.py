from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from telegram.error import TimedOut

from agentbridge.application import LearningProposal, LearningResult, MemoryProposal
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
    confirm_calls: list[int] = field(default_factory=list)
    confirm_result: LearningResult | None = field(default_factory=lambda: LearningResult("Acme", True, None, False))
    context_calls: list[dict] = field(default_factory=list)
    memory_confirm_calls: list[int] = field(default_factory=list)

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

    @staticmethod
    def is_memory_context_command(text: str) -> bool:
        return text.startswith("Контекст:")

    async def handle_owner_context(self, owner_chat_id, reply_to_message_id, author_user_id, author_name, text, update_id=None):
        self.context_calls.append({"reply_to_message_id": reply_to_message_id, "text": text})
        return MemoryProposal(8, "Acme", "Known fact", "chat")

    def confirm_memory(self, draft_id: int):
        self.memory_confirm_calls.append(draft_id)
        return MemoryProposal(draft_id, "Acme", "Known fact", "chat")

    def reject_memory(self, draft_id: int):
        return True

    async def confirm_learning(self, draft_id: int):
        self.confirm_calls.append(draft_id)
        return self.confirm_result


@dataclass
class FakeCallbackMessage:
    chat: FakeChat


@dataclass
class FakeCallbackQuery:
    data: str
    message: FakeCallbackMessage
    answer_error: BaseException | None = None
    edit_error: BaseException | None = None
    answered: bool = False
    markup_cleared: bool = False

    async def answer(self, **kwargs):
        if self.answer_error is not None:
            raise self.answer_error
        self.answered = True

    async def edit_message_reply_markup(self, reply_markup=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.markup_cleared = True


@dataclass
class FakeCallbackUpdate:
    callback_query: FakeCallbackQuery


def _text_callback(application):
    return application.handlers[0][0].callback


def _learning_callback(application):
    return application.handlers[0][1].callback


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
async def test_context_reply_to_bot_recommendation_creates_memory_confirmation() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _text_callback(application)(
        FakeUpdate(FakeMessage("Контекст: Клиент использует свой колл-центр.", FakeReply(9001, FakeUser(777, "AgentBridge", True))), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )
    assert service.context_calls == [{"reply_to_message_id": 9001, "text": "Контекст: Клиент использует свой колл-центр."}]
    buttons = bot.sent[0]["reply_markup"].inline_keyboard[0]
    assert [button.callback_data for button in buttons] == ["memory:yes:8", "memory:no:8"]


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


@dataclass
class DedupingLearningService(FakeLearningService):
    processed: set[int] = field(default_factory=set)

    def is_update_processed(self, update_id: int) -> bool:
        return update_id in self.processed

    def mark_update_processed(self, update_id: int) -> None:
        self.processed.add(update_id)


@pytest.mark.asyncio
async def test_duplicate_owner_update_id_does_not_repeat_learning() -> None:
    service = DedupingLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    update = FakeUpdate(
        FakeMessage("Make it warmer", FakeReply(9001, FakeUser(777, "AgentBridge", True))),
        FakeChat(7654321),
        FakeUser(),
        555,
    )
    await _text_callback(application)(update, FakeContext(bot))
    await _text_callback(application)(update, FakeContext(bot))
    assert len(service.feedback_calls) == 1
    assert service.is_update_processed(555) is True


def _yes_callback_update(*, owner_chat_id: int = 7654321, draft_id: int = 2, answer_error=None, edit_error=None) -> FakeCallbackUpdate:
    query = FakeCallbackQuery(
        data=f"learn:yes:{draft_id}",
        message=FakeCallbackMessage(FakeChat(owner_chat_id)),
        answer_error=answer_error,
        edit_error=edit_error,
    )
    return FakeCallbackUpdate(query)


@pytest.mark.asyncio
async def test_yes_button_confirms_learning() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    update = _yes_callback_update()
    await _learning_callback(application)(update, FakeContext(bot))
    assert service.confirm_calls == [2]
    assert update.callback_query.answered is True
    assert update.callback_query.markup_cleared is True
    assert bot.sent[-1]["text"] == "Правило сохранено."


@pytest.mark.asyncio
async def test_yes_button_still_confirms_when_telegram_ack_times_out() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    update = _yes_callback_update(answer_error=TimedOut("Timed out"))
    await _learning_callback(application)(update, FakeContext(bot))
    assert service.confirm_calls == [2]
    assert bot.sent[-1]["text"] == "Правило сохранено."


@pytest.mark.asyncio
async def test_yes_button_still_confirms_when_clearing_buttons_times_out() -> None:
    service = FakeLearningService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    update = _yes_callback_update(edit_error=TimedOut("Timed out"))
    await _learning_callback(application)(update, FakeContext(bot))
    assert service.confirm_calls == [2]
    assert bot.sent[-1]["text"] == "Правило сохранено."
