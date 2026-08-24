from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentbridge.agents.base import AgentReply
from agentbridge.application import AgentBridgeApplication
from agentbridge.media import media_label
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application, transcribe_pending_voice_messages
from agentbridge.telegram.media import describe_message_media, has_client_content


@dataclass
class FakeProvider:
    calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return AgentReply("thread-1", "Situation", "Reply")


@dataclass
class FakeVoice:
    file_id: str = "voice-file-id"
    duration: int = 7
    mime_type: str = "audio/ogg"
    file_size: int = 24_000


@dataclass
class FakeMessage:
    text: str = ""
    caption: str | None = None
    photo: list | None = None
    voice: object | None = None
    document: object | None = None
    media_group_id: str | None = None
    message_id: int = 101


@dataclass
class FakeChat:
    id: int = -100123456


@dataclass
class FakeUser:
    full_name: str = "Alice"
    is_bot: bool = False
    id: int = 5


@dataclass
class FakeUpdate:
    effective_message: FakeMessage
    effective_chat: FakeChat
    effective_user: FakeUser
    update_id: int = 501


@dataclass
class FakeTelegramFile:
    downloads: list[Path]

    async def download_to_drive(self, custom_path=None, **kwargs):
        path = Path(custom_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-ogg-bytes")
        self.downloads.append(path)
        return path


@dataclass
class DownloadingBot:
    downloads: list[Path] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)

    async def get_file(self, file_id: str):
        return FakeTelegramFile(self.downloads)

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return type("Sent", (), {"message_id": 9000})()


def test_voice_message_detected_from_telegram_payload() -> None:
    message = FakeMessage(voice=FakeVoice())
    ref = describe_message_media(message)
    assert ref is not None and ref.kind == "voice"
    assert ref.filename == "voice.ogg"
    assert has_client_content(message) is True


def test_media_label_marks_voice() -> None:
    assert media_label("voice") == "[голосовое]"
    assert media_label("voice", unavailable=True) == "[голосовое, файл недоступен]"


@pytest.mark.asyncio
async def test_voice_transcript_flows_into_episode_and_persists(tmp_path, chat_registry, monkeypatch) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        assert api_key == "test-key"
        assert model == "gpt-4o-mini-transcribe"
        return "Привет, нужен счёт"

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media",
        openai_api_key="test-key", transcription_model="gpt-4o-mini-transcribe",
    )
    bot = DownloadingBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(FakeMessage(voice=FakeVoice(), message_id=606), FakeChat(), FakeUser(), 606),
        type("Ctx", (), {"bot": bot})(),
    )
    await asyncio.sleep(0.05)

    assert len(provider.calls) == 1
    episode = provider.calls[0]["message"]
    assert episode == "[голосовое] Привет, нужен счёт"
    history = store.recent_messages(-100123456)
    assert history[0].text == "Привет, нужен счёт"
    assert history[0].media_kind == "voice"
    # Локальная копия аудио удалена после эпизода.
    assert not bot.downloads or not bot.downloads[0].exists()
    assert all(item["chat_id"] == 7654321 for item in bot.sent)


@pytest.mark.asyncio
async def test_transcription_failure_keeps_row_pending(tmp_path, chat_registry, monkeypatch) -> None:
    from agentbridge.transcribe import TranscriptionError

    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)

    async def failing_transcribe(path, *, api_key: str, model: str) -> str:
        raise TranscriptionError("api down")

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", failing_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654331, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media",
        openai_api_key="test-key",
    )
    bot = DownloadingBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(FakeMessage(voice=FakeVoice(), message_id=707), FakeChat(), FakeUser(), 707),
        type("Ctx", (), {"bot": bot})(),
    )
    await asyncio.sleep(0.05)

    assert len(provider.calls) == 1
    episode = provider.calls[0]["message"]
    # Сбой API не блокирует батч: плейсхолдер вместо транскрипта.
    assert episode == "[голосовое] не удалось распознать речь"
    history = store.recent_messages(-100123456)
    assert history[0].text == "не удалось распознать речь"


@pytest.mark.asyncio
async def test_catchup_path_transcribes_backlog_voice_before_episode(
    tmp_path, chat_registry, monkeypatch,
) -> None:
    """Тот же шаг, что делает _run_catchup после рестарта: транскрипция до catch_up()."""
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    service.ingest_telegram_message(
        update_id=808, chat_id=-100123456, message_id=808, sender_id=5,
        sender_name="Alice", telegram_date="", text="", reply_to_message_id=None,
        is_owner_chat=False, media_kind="voice", telegram_file_id="backlog-voice-id",
    )

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        return "распознано из догрузки"

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )

    provider = FakeProvider()
    restarted_service = AgentBridgeApplication(chat_registry, store, provider)
    bot = DownloadingBot()

    await transcribe_pending_voice_messages(
        restarted_service, bot, media_dir=tmp_path / "media",
        api_key="test-key", model="gpt-4o-mini-transcribe",
        telegram_chat_id=-100123456,
    )
    suggestions = await restarted_service.catch_up()

    assert len(suggestions) == 1
    episode = provider.calls[0]["message"]
    assert episode == "[голосовое] распознано из догрузки"
    assert not restarted_service.store.pending_messages(-100123456)


@pytest.mark.asyncio
async def test_transcription_failure_gets_placeholder_and_episode_proceeds(
    tmp_path, chat_registry, monkeypatch,
) -> None:
    """Сбой API не блокирует батч: плейсхолдер в тексте, эпизод собирается."""
    from agentbridge.transcribe import TranscriptionError

    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    service.ingest_telegram_message(
        update_id=810, chat_id=-100123456, message_id=810, sender_id=5,
        sender_name="Alice", telegram_date="", text="", reply_to_message_id=None,
        is_owner_chat=False, media_kind="voice", telegram_file_id="fail-voice-id",
    )

    async def failing_transcribe(path, *, api_key: str, model: str) -> str:
        raise TranscriptionError("quota exceeded")

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", failing_transcribe,
    )
    provider = FakeProvider()
    restarted_service = AgentBridgeApplication(chat_registry, store, provider)
    bot = DownloadingBot()

    await transcribe_pending_voice_messages(
        restarted_service, bot, media_dir=tmp_path / "media",
        api_key="test-key", model="gpt-4o-mini-transcribe",
        telegram_chat_id=-100123456,
    )
    suggestions = await restarted_service.catch_up()

    assert len(suggestions) == 1
    episode = provider.calls[0]["message"]
    assert episode == "[голосовое] не удалось распознать речь"


def test_no_api_key_treats_voice_as_plain_attachment(chat_registry, tmp_path) -> None:
    """Без ключа транскрибация пропускается: голосовой остаётся pending без падения."""
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    service.ingest_telegram_message(
        update_id=111, chat_id=-100123456, message_id=111, sender_id=5,
        sender_name="Alice", telegram_date="", text="", reply_to_message_id=None,
        is_owner_chat=False, media_kind="voice", telegram_file_id="nokey-voice-id",
    )
    assert len(service.pending_voice_messages(-100123456)) == 1


@pytest.mark.asyncio
async def test_empty_transcript_gets_placeholder(tmp_path, chat_registry, monkeypatch) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)

    async def empty_transcribe(path, *, api_key: str, model: str) -> str:
        return ""

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", empty_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654331, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media",
        openai_api_key="test-key",
    )
    bot = DownloadingBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(FakeMessage(voice=FakeVoice(), message_id=909), FakeChat(), FakeUser(), 909),
        type("Ctx", (), {"bot": bot})(),
    )
    await asyncio.sleep(0.05)

    assert len(provider.calls) == 1
    episode = provider.calls[0]["message"]
    assert episode == "[голосовое] не удалось распознать речь"


def test_store_set_message_text_only_updates_pending(chat_registry, tmp_path) -> None:
    """set_message_text защищает от перезаписи уже обработанных сообщений."""
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    service.ingest_telegram_message(
        update_id=120, chat_id=-100123456, message_id=120, sender_id=5,
        sender_name="Alice", telegram_date="", text="", reply_to_message_id=None,
        is_owner_chat=False, media_kind="voice", telegram_file_id="lock-voice-id",
    )
    row = store.pending_messages(-100123456)[0]
    store.claim_messages([row.id])
    assert store.set_message_text(row.update_id, "поздно") is False
    claimed = store.claim_messages([row.id])
    assert len(claimed) == 1 and claimed[0].text == ""


@dataclass
class FakeOwnerReply:
    message_id: int
    from_user: object


@dataclass
class FakeBotUser:
    id: int = 777
    is_bot: bool = True


@dataclass
class OwnerVoiceMessage(FakeMessage):
    reply_to_message: FakeOwnerReply | None = None


@dataclass
class FakeOwnerBot:
    """Фейковый бот для owner-пути: и скачивает файл, и отправляет сообщения."""

    downloads: list[Path] = field(default_factory=list)
    sent: list[dict] = field(default_factory=list)
    id: int = 777

    async def get_file(self, file_id: str):
        return FakeTelegramFile(self.downloads)

    async def send_message(self, *, chat_id: int, text: str, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return type("Sent", (), {"message_id": 9500 + len(self.sent)})()


@pytest.mark.asyncio
async def test_owner_voice_reply_is_transcribed_and_learns(
    tmp_path, chat_registry, monkeypatch,
) -> None:
    """Голосовое владельца в ответ на рекомендацию = обратная связь."""
    from agentbridge.application import LearningProposal

    class FeedbackService(AgentBridgeApplication):
        def __init__(self):
            super().__init__(chat_registry, ChatThreadStore(tmp_path / "db.sqlite3"), FakeProvider())
            self.feedback_calls: list[dict] = []

        async def handle_owner_feedback(self, owner_chat_id, reply_to_message_id, author_user_id, author_name, feedback, update_id=None):
            self.feedback_calls.append({"reply": reply_to_message_id, "feedback": feedback})
            return LearningProposal(7, "Acme", "понял так", None, "client", False)

    service = FeedbackService()

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        return "сделай ответ теплее"

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media", openai_api_key="k",
    )
    bot = FakeOwnerBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(
            OwnerVoiceMessage(
                voice=FakeVoice(file_id="owner-voice"),
                reply_to_message=FakeOwnerReply(9001, FakeBotUser()),
            ),
            FakeChat(7654321), FakeUser(), 610,
        ),
        type("Ctx", (), {"bot": bot})(),
    )

    assert service.feedback_calls == [{"reply": 9001, "feedback": "сделай ответ теплее"}]
    assert any("Я понял так" in item["text"] for item in bot.sent)
    # Локальная копия аудио удалена.
    assert not bot.downloads or not bot.downloads[0].exists()


@pytest.mark.asyncio
async def test_owner_voice_reply_continues_owner_query_with_transcript(
    tmp_path, chat_registry, monkeypatch,
) -> None:
    """Голосовой ответ на внутренний ответ бота продолжает его цепочку."""

    class QueryContinuationService(AgentBridgeApplication):
        def __init__(self):
            super().__init__(chat_registry, ChatThreadStore(tmp_path / "db.sqlite3"), FakeProvider())
            self.continued: list[dict] = []

        async def continue_owner_query(self, owner_message_id, text, update_id=None):
            self.continued.append({
                "reply": owner_message_id,
                "text": text,
                "update_id": update_id,
            })
            return "Продолжаю внутренний запрос."

        async def handle_owner_feedback(self, *args, **kwargs):
            raise AssertionError("Owner-query voice reply must not enter feedback handling")

    service = QueryContinuationService()

    class RetryOwnerBot(FakeOwnerBot):
        get_file_calls: int = 0

        async def get_file(self, file_id: str):
            self.get_file_calls += 1
            if self.get_file_calls <= 3:
                raise RuntimeError("temporary Telegram download failure")
            return FakeTelegramFile(self.downloads)

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        return "уточни следующий шаг по этому чату"

    async def no_retry_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )
    monkeypatch.setattr("agentbridge.telegram.media.asyncio.sleep", no_retry_delay)
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media", openai_api_key="k",
    )
    bot = RetryOwnerBot()
    callback = application.handlers[0][0].callback
    # Первая попытка исчерпывает Telegram-retry и создаёт служебное сообщение.
    await callback(
        FakeUpdate(
            OwnerVoiceMessage(
                voice=FakeVoice(file_id="owner-query-voice"),
                reply_to_message=FakeOwnerReply(9002, FakeBotUser()),
            ),
            FakeChat(7654321), FakeUser(), 614,
        ),
        type("Ctx", (), {"bot": bot})(),
    )
    assert service.continued == []
    assert any("Не смог скачать голосовое" in item["text"] for item in bot.sent)

    # Повтор ответом на ошибку восстанавливает исходную привязку к сообщению 9002.
    await callback(
        FakeUpdate(
            OwnerVoiceMessage(
                voice=FakeVoice(file_id="owner-query-voice-retry"),
                reply_to_message=FakeOwnerReply(9501, FakeBotUser()),
            ),
            FakeChat(7654321), FakeUser(), 615,
        ),
        type("Ctx", (), {"bot": bot})(),
    )

    assert service.continued == [{
        "reply": 9002,
        "text": "уточни следующий шаг по этому чату",
        "update_id": 615,
    }]
    assert any("Продолжаю внутренний запрос" in item["text"] for item in bot.sent)


@pytest.mark.asyncio
async def test_owner_voice_without_api_key_gets_hint(tmp_path, chat_registry) -> None:
    """Без ключа владелец получает подсказку вместо тихого игнора."""
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321,
        message_service=AgentBridgeApplication(chat_registry, ChatThreadStore(tmp_path / "db.sqlite3"), FakeProvider()),
        batch_seconds=0, media_dir=tmp_path / "media",
    )
    bot = FakeOwnerBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(
            OwnerVoiceMessage(voice=FakeVoice(), reply_to_message=FakeOwnerReply(9001, FakeBotUser())),
            FakeChat(7654321), FakeUser(), 611,
        ),
        type("Ctx", (), {"bot": bot})(),
    )
    assert any("OPENAI_API_KEY" in item["text"] for item in bot.sent)


@pytest.mark.asyncio
async def test_owner_voice_without_addressing_is_ignored(tmp_path, chat_registry, monkeypatch) -> None:
    """Свободное голосовое без реплая и без @упоминания в речи - обычный разговор."""
    queries: list[str] = []

    class QueryService(AgentBridgeApplication):
        async def handle_owner_query(self, text, *, reply_to_message_id=None, update_id=None):
            queries.append(text)

    service = QueryService(chat_registry, ChatThreadStore(tmp_path / "db.sqlite3"), FakeProvider())

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        return "просто болтовня в чате"

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media", openai_api_key="k",
    )
    bot = FakeOwnerBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(OwnerVoiceMessage(voice=FakeVoice()), FakeChat(7654321), FakeUser(), 612),
        type("Ctx", (), {"bot": bot})(),
    )
    assert queries == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_owner_voice_with_spoken_mention_starts_query(tmp_path, chat_registry, monkeypatch) -> None:
    """Обращение «Рик» в начале транскрипта превращает голосовой в запрос."""
    queries: list[str] = []

    class QueryService(AgentBridgeApplication):
        async def handle_owner_query(self, text, *, reply_to_message_id=None, update_id=None):
            queries.append(text)
            return None

    service = QueryService(chat_registry, ChatThreadStore(tmp_path / "db.sqlite3"), FakeProvider())

    async def fake_transcribe(path, *, api_key: str, model: str) -> str:
        return "Рик, что по ОптоБель"

    monkeypatch.setattr(
        "agentbridge.telegram.bot.transcribe_audio_file", fake_transcribe,
    )
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=tmp_path / "media", openai_api_key="k",
    )
    bot = FakeOwnerBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(OwnerVoiceMessage(voice=FakeVoice()), FakeChat(7654321), FakeUser(), 613),
        type("Ctx", (), {"bot": bot})(),
    )
    assert queries == ["Рик, что по ОптоБель"]
