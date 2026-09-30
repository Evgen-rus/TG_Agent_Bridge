from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import NetworkError, TimedOut

from agentbridge.agents.base import AgentReply, FeedbackAnalysis, OwnerQueryAnswer
from agentbridge.application import AgentBridgeApplication, MemoryProposal, OwnerQueryResult, QuestionReplyResult, Suggestion
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application
from agentbridge.telegram.formatter import split_owner_message


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
async def test_generated_photo_is_sent_only_to_owner_and_removed_after_delivery(tmp_path) -> None:
    generated_dir = tmp_path / "runtime" / "media" / "owner_generated"
    generated_dir.mkdir(parents=True)
    image_path = generated_dir / "owner-task-1.jpg"
    image_bytes = b"\xff\xd8mock-jpeg\xff\xd9"
    image_path.write_bytes(image_bytes)

    class PhotoService(OwnerAssistantService):
        async def handle_owner_query(self, text, reply_to_message_id=None, update_id=None):
            return OwnerQueryResult("Тестовое изображение Рика", media_path=str(image_path))

    class PhotoBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.photos: list[dict] = []

        async def send_photo(self, **kwargs):
            self.photos.append({**kwargs, "photo_bytes": kwargs["photo"].read()})
            return FakeSentMessage(2020)

    service = PhotoService()
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "runtime" / "media",
    )
    bot = PhotoBot()

    await _callback(application)(
        FakeUpdate(FakeMessage("@agent Нарисуй Рика"), FakeChat(7654321), FakeUser()), FakeContext(bot),
    )

    assert len(bot.photos) == 1
    assert bot.photos[0]["chat_id"] == 7654321
    assert bot.photos[0]["caption"] == "Тестовое изображение Рика"
    assert bot.photos[0]["photo_bytes"] == image_bytes
    assert bot.sent == []
    assert service.client_calls == []
    assert not image_path.exists()


@pytest.mark.asyncio
async def test_codex_file_citation_and_local_path_never_reach_telegram() -> None:
    class CitationService(OwnerAssistantService):
        async def handle_owner_query(self, text, reply_to_message_id=None, update_id=None):
            return OwnerQueryResult(
                'Счёт проверен :codex-file-citation{path="D:\\Project\\runtime\\media\\invoice.pdf" purpose="source"}. '
                'Копия: "D:\\My Project\\runtime\\media\\invoice.pdf"',
            )
    service = CitationService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _callback(application)(
        FakeUpdate(FakeMessage("@agent проверь счёт"), FakeChat(7654321), FakeUser()), FakeContext(bot),
    )
    sent = bot.sent[0]["text"]
    assert "Счёт проверен" in sent
    assert ":codex-file-citation" not in sent
    assert "D:\\" not in sent
    assert "My Project" not in sent
    assert "[локальный файл]" in sent


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
    assert any("Предлагаю запомнить:" in item["text"] for item in bot.sent)


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


class MultipartBot(FakeBot):
    def __init__(self, fail_at=0):
        super().__init__()
        self.fail_at = fail_at
        self.attempts = 0

    async def send_message(self, **kwargs):
        assert len(kwargs["text"].encode("utf-16-le")) // 2 <= 4096
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise TimedOut("temporary timeout")
        return await super().send_message(**kwargs)


async def _deliver_pending(application, bot):
    application.bot = bot
    await application.post_init(application)
    try:
        await asyncio.sleep(0.04)
    finally:
        await application.post_stop(application)


@pytest.mark.asyncio
async def test_pending_generated_photo_is_retained_while_telegram_delivery_fails(tmp_path) -> None:
    import os
    import time

    media_dir = tmp_path / "runtime" / "media"
    generated_dir = media_dir / "owner_generated"
    generated_dir.mkdir(parents=True)
    image_path = generated_dir / "owner-task-9.jpg"
    image_path.write_bytes(b"\xff\xd8mock-jpeg\xff\xd9")
    os.utime(image_path, (time.time() - 3600, time.time() - 3600))

    class Store:
        def retained_document_paths(self):
            return set()

    class Service:
        store = Store()

        def pending_owner_query_deliveries(self):
            return [OwnerQueryResult("Изображение Рика", delivery_id=9, media_path=str(image_path))]

        def record_owner_query_delivery(self, delivery_id, owner_message_id):
            raise AssertionError("A failed photo must remain pending")

    class OfflinePhotoBot(FakeBot):
        async def send_photo(self, **kwargs):
            raise NetworkError("offline")

    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=Service(),
        media_dir=media_dir, media_ttl_seconds=10, delivery_retry_seconds=30,
    )

    await _deliver_pending(application, OfflinePhotoBot())

    assert image_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_part", [0, 1, -1], ids=["first", "middle", "last"])
async def test_long_query_resumes_delivery_and_reply_to_any_part_continues_thread(
    tmp_path, chat_registry, reply_part,
) -> None:
    path = tmp_path / "query.sqlite3"
    answer = "Большой ответ владельцу 🚀\n" * 450
    provider = SimpleNamespace(answer_owner_query=AsyncMock(side_effect=[
        OwnerQueryAnswer("owner-thread", answer), OwnerQueryAnswer("owner-thread", "Продолжаем"),
    ]))
    service = AgentBridgeApplication(chat_registry, ChatThreadStore(path), provider, 7654321)
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service)
    bot = MultipartBot(fail_at=2)
    await _callback(application)(
        FakeUpdate(FakeMessage("Рик, как дела у Acme?"), FakeChat(7654321), FakeUser(), 601), FakeContext(bot),
    )
    assert len(bot.sent) == 1
    assert len(service.pending_owner_query_deliveries()) == 1

    service = AgentBridgeApplication(chat_registry, ChatThreadStore(path), provider, 7654321)
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service)
    await _deliver_pending(application, bot)
    expected = split_owner_message(answer)
    assert [item["text"] for item in bot.sent] == expected
    assert service.pending_owner_query_deliveries() == []
    assert provider.answer_owner_query.await_count == 1
    part_ids = list(range(1001, 1001 + len(expected)))
    await _callback(application)(
        FakeUpdate(
            FakeMessage("А какой следующий шаг?", FakeReply(part_ids[reply_part], FakeUser(777, "Рик", True))),
            FakeChat(7654321), FakeUser(), 602,
        ), FakeContext(bot),
    )
    assert bot.sent[-1]["text"] == "Продолжаем"
    assert provider.answer_owner_query.await_count == 2
    assert provider.answer_owner_query.call_args.kwargs["thread_id"] == "owner-thread"
    assert all(item["chat_id"] == 7654321 for item in bot.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["reply", "ask_owner"])
async def test_reply_to_first_recommendation_part_preserves_feedback_and_question_paths(
    tmp_path, chat_registry, action,
) -> None:
    store = ChatThreadStore(tmp_path / "reply.sqlite3")
    recommendation_id = store.create_recommendation(
        telegram_chat_id=-100123456, chat_name="Acme Support", sender_name="Alice",
        original_message="Контекст\n" * 1100, situation="Нужен срок", suggested_reply="Ответ",
        owner_chat_id=7654321, action=action, owner_question="Какой срок?",
    )
    if action == "ask_owner":
        store.create_owner_question(-100123456, "Какой срок?", recommendation_id)
    provider = SimpleNamespace(
        suggest=AsyncMock(return_value=AgentReply("client-thread", "Срок известен", "Завтра")),
        analyze_feedback=AsyncMock(return_value=FeedbackAnalysis(
            "Подробное понимание поправки.\n" * 350, None, None, "client", False, None,
        )),
    )
    service = AgentBridgeApplication(chat_registry, store, provider, 7654321)
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service)
    bot = MultipartBot()
    await _deliver_pending(application, bot)
    recommendation_parts = len(bot.sent)
    assert recommendation_parts > 1
    update = FakeUpdate(
        FakeMessage("Срок завтра", FakeReply(1001, FakeUser(777, "Рик", True))),
        FakeChat(7654321), FakeUser(), 701,
    )
    await _callback(application)(update, FakeContext(bot))
    if action == "ask_owner":
        assert provider.suggest.await_count == 1
        assert provider.analyze_feedback.await_count == 0
        assert "Срок завтра" in provider.suggest.call_args.kwargs["message"]
    else:
        assert provider.analyze_feedback.await_count == 1
        assert provider.analyze_feedback.call_args.kwargs["original_message"] == "Контекст\n" * 1100
        # A long learning proposal is also split; only its final part has buttons.
        proposal_parts = bot.sent[recommendation_parts:]
        assert len(proposal_parts) > 1
        assert all("reply_markup" not in part for part in proposal_parts[:-1])
        assert proposal_parts[-1]["reply_markup"].inline_keyboard[0][0].callback_data.startswith("learn:yes:")
    before_duplicate = len(bot.sent)
    await _callback(application)(update, FakeContext(bot))
    assert len(bot.sent) == before_duplicate
    assert all(item["chat_id"] == 7654321 for item in bot.sent)


class VoiceBot(FakeBot):
    """Бот, который умеет отправлять голос и умеет его ронять."""

    def __init__(self, *, fail_voice: bool = False) -> None:
        super().__init__()
        self.fail_voice = fail_voice
        self.voices: list[dict] = []

    async def send_voice(self, **kwargs):
        if self.fail_voice:
            raise NetworkError("voice upload failed")
        self.voices.append({**kwargs, "voice_bytes": kwargs["voice"].read()})
        return FakeSentMessage(3000 + len(self.voices))


class _BaseVoiceService(OwnerAssistantService):
    """Owner-сервис, который доставляет текст и умеет синтезировать голос.

    Голос пишется в заранее заданный файл: так тест проверяет доставку и
    удаление MP3, а не работу HTTP-слоя OpenRouter.
    """

    def __init__(self, *, error: Exception | None = None, result=None, audio_name: str = "owner-voice-test.mp3") -> None:
        super().__init__()
        self.spoken_texts: list[str] = []
        self.pending: list[OwnerQueryResult] = []
        self.recorded: list[tuple[int, int]] = []
        self.audio_path: Path | None = None
        self._error = error
        self._result = result or OwnerQueryResult("Ответ владельцу", 11, speak=True)
        self._audio_name = audio_name

    def bind_audio(self, path: Path) -> None:
        self.audio_path = path

    async def handle_owner_query(self, text, reply_to_message_id=None, update_id=None):
        return self._result

    async def prepare_owner_query_voice(self, result):
        self.spoken_texts.append(result.text)
        if self._error is not None:
            raise self._error
        if self.audio_path is None:
            raise AssertionError("bind_audio() was not called")
        self.audio_path.write_bytes(b"ID3-audio")
        return replace(result, media_path=str(self.audio_path), media_kind="audio")

    def save_pending_owner_query_delivery(
        self, text, prompt_id, selection_id=None, general_task_id=None, media_path="", media_kind="",
    ):
        self.pending.append(OwnerQueryResult(
            text, prompt_id, delivery_id=len(self.pending) + 1,
            media_path=media_path, media_kind=media_kind,
        ))
        return len(self.pending)

    def record_owner_query_delivery(self, delivery_id, owner_message_id):
        self.recorded.append((delivery_id, owner_message_id))
        # Доставка закрыта — повторно её брать нельзя.
        self.pending = [item for item in self.pending if item.delivery_id != delivery_id]

    def pending_owner_query_deliveries(self):
        return list(self.pending)


class VoiceService(_BaseVoiceService):
    pass


def _voice_application(service, bot, tmp_path, audio_name: str = "owner-voice-test.mp3"):
    media_dir = tmp_path / "runtime" / "media"
    generated_dir = media_dir / "owner_generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    service.bind_audio(generated_dir / audio_name)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=media_dir, delivery_retry_seconds=30,
    )
    application.bot = bot
    return application, generated_dir


_SENSITIVE_OWNER_RESULT = OwnerQueryResult(
    'Счёт проверен :codex-file-citation{path="D:\\Project\\runtime\\invoice.pdf" purpose="source"}. '
    'Копия: "D:\\My Project\\runtime\\invoice.pdf"',
    11, speak=True,
)


@pytest.mark.asyncio
async def test_voice_is_synthesized_from_sanitized_text_after_text_delivery(tmp_path) -> None:
    """TTS получает ровно тот же очищенный текст, что ушёл в Telegram."""
    service = VoiceService(result=_SENSITIVE_OWNER_RESULT)
    application, _ = _voice_application(service, VoiceBot(), tmp_path)

    await _callback(application)(
        FakeUpdate(FakeMessage("@agent проверь счёт"), FakeChat(7654321), FakeUser()),
        FakeContext(application.bot),
    )

    assert len(service.spoken_texts) == 1
    spoken = service.spoken_texts[0]
    assert ":codex-file-citation" not in spoken
    assert "D:\\" not in spoken and "My Project" not in spoken
    # Синтез идёт после текста: текст уже отправлен, голос — вторым сообщением.
    assert len(application.bot.sent) == 1
    assert spoken == application.bot.sent[0]["text"]
    assert len(application.bot.voices) == 1
    assert application.bot.voices[0]["chat_id"] == 7654321


@pytest.mark.asyncio
async def test_tts_failure_does_not_block_or_fail_text_delivery(tmp_path) -> None:
    """Упавший OpenRouter не превращает текстовый ответ в pending/failed."""
    service = VoiceService(error=RuntimeError("openrouter is down"))
    application, _ = _voice_application(service, VoiceBot(), tmp_path)

    await _callback(application)(
        FakeUpdate(FakeMessage("@agent вопрос"), FakeChat(7654321), FakeUser()),
        FakeContext(application.bot),
    )

    # Текст доставлен и подтверждён: сбой TTS не оставляет незакрытой доставки.
    assert [item["text"] for item in application.bot.sent] == ["Ответ владельцу"]
    assert application.bot.voices == []
    assert service.spoken_texts == ["Ответ владельцу"]
    assert service.pending == []
    assert service.recorded == [(1, 1001)]


@pytest.mark.asyncio
async def test_telegram_audio_failure_keeps_mp3_and_retry_resends_only_audio(tmp_path) -> None:
    """Аудио не доставилось — MP3 сохранён, повтор не дублирует уже отправленный текст."""
    service = VoiceService()
    bot = VoiceBot(fail_voice=True)
    application, _ = _voice_application(service, bot, tmp_path)
    audio_path = service.audio_path

    await _callback(application)(
        FakeUpdate(FakeMessage("@agent вопрос"), FakeChat(7654321), FakeUser(), 811),
        FakeContext(bot),
    )

    assert [item["text"] for item in bot.sent] == ["Ответ владельцу"]
    assert bot.voices == []
    # MP3 переживает неудачу Telegram — иначе голос пришлось бы платно синтезировать заново.
    assert audio_path.exists()
    # Незакрытая доставка содержит только аудио: текст у неё пустой.
    assert [(item.text, item.media_kind) for item in service.pending] == [("", "audio")]
    assert service.recorded[0][0] == 1

    # Повтор: Telegram ожил, текст уходить не должен.
    retry_bot = VoiceBot()
    application.bot = retry_bot
    await _deliver_pending(application, retry_bot)

    assert retry_bot.sent == []
    assert len(retry_bot.voices) == 1
    assert retry_bot.voices[0]["voice_bytes"] == b"ID3-audio"
    assert not audio_path.exists()
    assert service.pending == []


@pytest.mark.asyncio
async def test_successful_audio_delivery_removes_mp3(tmp_path) -> None:
    """После принятого Telegram голоса временный MP3 удаляется."""
    service = VoiceService()
    bot = VoiceBot()
    application, _ = _voice_application(service, bot, tmp_path, audio_name="owner-voice-ok.mp3")
    audio_path = service.audio_path

    await _callback(application)(
        FakeUpdate(FakeMessage("@agent вопрос"), FakeChat(7654321), FakeUser()),
        FakeContext(bot),
    )

    assert len(bot.voices) == 1
    assert not audio_path.exists()
    assert service.pending == []
