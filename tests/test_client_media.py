from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path

import pytest

from agentbridge.agents.base import AgentReply, MediaAttachment
from agentbridge.application import AgentBridgeApplication, IncomingMessage
from agentbridge.media import purge_expired_media
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application
from agentbridge.telegram.media import describe_message_media, has_client_content


@dataclass
class FakeProvider:
    calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return AgentReply("thread-1", "Situation", "Reply")


@dataclass
class FakePhotoSize:
    file_id: str
    file_size: int = 1200


@dataclass
class FakeDocument:
    file_id: str
    file_name: str = "scan.pdf"
    mime_type: str = "application/pdf"
    file_size: int = 2048
    file_unique_id: str = "unique-pdf"


@dataclass
class FakeMessage:
    text: str = ""
    caption: str | None = None
    photo: list | None = None
    document: object | None = None
    media_group_id: str | None = None
    message_id: int = 101
    date: datetime | None = None
    forward_origin: object | None = None


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
        path.write_bytes(b"fake-image")
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


def test_purge_expired_media_keeps_fresh_files(tmp_path) -> None:
    root = tmp_path / "media"
    old = root / "-1001" / "1_photo.jpg"
    fresh = root / "-1001" / "2_photo.jpg"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old")
    fresh.write_bytes(b"new")
    now = 2_000_000
    older = now - 3601
    os.utime(old, (older, older))
    os.utime(fresh, (now, now))
    removed = purge_expired_media(root, ttl_seconds=3600, now=now)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_purge_keeps_retained_document(tmp_path) -> None:
    root = tmp_path / "media"
    pdf = root / "123" / "invoice.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    os.utime(pdf, (1, 1))
    assert purge_expired_media(root, ttl_seconds=60, now=1000, retained_paths={str(pdf)}) == 0
    assert pdf.exists()


def test_photo_and_pdf_are_detected_from_telegram_payload() -> None:
    photo = FakeMessage(photo=[FakePhotoSize("file-small", 10), FakePhotoSize("file-large", 9000)], media_group_id="grp-1")
    pdf = FakeMessage(document=FakeDocument("file-pdf"), caption="счёт")
    photo_ref = describe_message_media(photo)
    pdf_ref = describe_message_media(pdf)
    assert photo_ref is not None and photo_ref.file_id == "file-large"
    assert photo_ref.media_group_id == "grp-1"
    assert pdf_ref is not None and pdf_ref.filename == "scan.pdf"
    assert has_client_content(FakeMessage(text="")) is False
    assert has_client_content(photo) is True


@pytest.mark.asyncio
async def test_photo_episode_reaches_codex_then_deletes_local_copy(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    photo = tmp_path / "shot.jpg"
    photo.write_bytes(b"jpeg-bytes")

    suggestion = await service.handle_messages(
        -100123456,
        [IncomingMessage(
            "Alice", "контакт на скрине", update_id=11, message_id=101,
            media_kind="photo", media_path=str(photo), telegram_file_id="AgAC-photo",
            media_mime="image/jpeg", media_filename="photo.jpg", media_group_id="alb-1",
        )],
    )

    assert suggestion is not None
    attachments = provider.calls[0]["attachments"]
    assert len(attachments) == 1
    assert attachments[0] == MediaAttachment(str(photo), "photo", "image/jpeg", "photo.jpg")
    assert "[фото]" in provider.calls[0]["message"]
    assert not photo.exists()
    history = store.recent_messages(-100123456)
    assert history[0].telegram_file_id == "AgAC-photo"
    assert history[0].media_path == ""
    assert history[0].media_group_id == "alb-1"


@pytest.mark.asyncio
async def test_album_and_pdf_stay_in_one_chat_episode(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    first = tmp_path / "1.jpg"
    second = tmp_path / "2.jpg"
    pdf = tmp_path / "scan.pdf"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    pdf.write_bytes(b"%PDF")

    await service.handle_messages(
        -100123456,
        [
            IncomingMessage("Alice", "", update_id=21, message_id=201, media_kind="photo", media_path=str(first), telegram_file_id="p1", media_filename="1.jpg", media_group_id="g1"),
            IncomingMessage("Alice", "и pdf", update_id=22, message_id=202, media_kind="photo", media_path=str(second), telegram_file_id="p2", media_filename="2.jpg", media_group_id="g1"),
            IncomingMessage("Alice", "", update_id=23, message_id=203, media_kind="document", media_path=str(pdf), telegram_file_id="d1", media_mime="application/pdf", media_filename="scan.pdf"),
        ],
    )

    assert len(provider.calls) == 1
    assert len(provider.calls[0]["attachments"]) == 3
    assert provider.calls[0]["attachments"][2].filename == "scan.pdf"
    assert "scan.pdf" in provider.calls[0]["message"]
    assert not first.exists() and not second.exists() and pdf.exists()


@pytest.mark.asyncio
async def test_forwarded_owner_invoice_and_client_payment_remain_readable(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.ingest_telegram_message(
        update_id=1, chat_id=7654321, message_id=1, sender_id=42, sender_name="Owner",
        telegram_date="2026-09-25T05:00:00+00:00", text="привет", reply_to_message_id=None,
        role="owner", processing_status="ignored",
    )
    class OwnerProvider:
        calls: list[dict] = []
        async def answer_owner_query(self, **kwargs):
            self.calls.append(kwargs)
            return "ok"
    owner_provider = OwnerProvider()
    service = AgentBridgeApplication(chat_registry, store, FakeProvider(), owner_chat_id=7654321, owner_provider=owner_provider)
    app = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0, media_dir=tmp_path / "media")
    bot = DownloadingBot()
    callback = app.handlers[0][0].callback
    invoice = FakeMessage(
        document=FakeDocument("invoice-id", "Счёт №580.pdf"), caption="Счёт",
        message_id=7300, date=datetime(2026, 9, 25, 5, 4, 19, tzinfo=timezone.utc),
        forward_origin=type("Origin", (), {"sender_user": type("User", (), {"full_name": "Дмитрий"})()})(),
    )
    payment = FakeMessage(
        document=FakeDocument("payment-id", "Платежное поручение №195.pdf"),
        message_id=7305, date=datetime(2026, 9, 25, 5, 46, 59, tzinfo=timezone.utc),
    )
    context = type("Ctx", (), {"bot": bot})()
    await callback(FakeUpdate(invoice, FakeChat(), FakeUser("Owner", id=42), 501), context)
    await callback(FakeUpdate(payment, FakeChat(), FakeUser("Анатолий", id=55), 502), context)
    await asyncio.sleep(0.1)
    files = store.list_chat_attachments(-100123456)
    assert [(row.media_filename, row.role) for row in files] == [
        ("Счёт №580.pdf", "owner"), ("Платежное поручение №195.pdf", "client"),
    ]
    assert files[0].forward_origin == "Дмитрий"
    assert files[0].media_file_unique_id == "unique-pdf"
    assert files[0].telegram_date == "2026-09-25T05:04:19+00:00"
    assert files[1].telegram_date == "2026-09-25T05:46:59+00:00"
    assert all(row.download_status == "available" and Path(row.media_path).read_bytes() == b"fake-image" for row in files), [(row.download_status, row.media_path) for row in files]
    await service._answer_owner_query_for_chat(chat_registry.get(-100123456), "Сравни счёт 580 и платежку 195")
    assert len(owner_provider.calls[-1]["attachments"]) == 2
    assert "05:04:19" in owner_provider.calls[-1]["context_pack"]
    assert "05:46:59" in owner_provider.calls[-1]["context_pack"]


def test_owner_document_without_forward_is_classified_and_listed(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.ingest_telegram_message(
        update_id=1, chat_id=7654321, message_id=1, sender_id=42, sender_name="Owner",
        telegram_date="2026-09-25T05:00:00+00:00", text="ready", reply_to_message_id=None,
        role="owner", processing_status="ignored",
    )
    service = AgentBridgeApplication(chat_registry, store, FakeProvider(), owner_chat_id=7654321)
    service.ingest_telegram_message(
        update_id=2, chat_id=-100123456, message_id=2, sender_id=42, sender_name="Owner",
        telegram_date="2026-09-25T05:04:00+00:00", text="счёт", reply_to_message_id=None,
        is_owner_chat=False, media_kind="document", telegram_file_id="invoice", media_filename="invoice.pdf",
    )
    row = store.list_chat_attachments(-100123456)[0]
    assert (row.role, row.forward_origin, row.download_status) == ("owner", "", "pending")


@pytest.mark.asyncio
async def test_processed_document_is_refetched_for_owner_query(tmp_path, chat_registry, monkeypatch) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.ingest_telegram_message(
        update_id=11, chat_id=-100123456, message_id=10, sender_id=5, sender_name="Alice",
        telegram_date="2026-09-25T05:00:00+00:00", text="", reply_to_message_id=None,
        role="client", processing_status="processed", media_kind="document",
        telegram_file_id="old-file", media_filename="Счёт №580.pdf", media_mime="application/pdf",
    )
    class OwnerProvider:
        calls: list[dict] = []
        async def answer_owner_query(self, **kwargs):
            self.calls.append(kwargs)
            return "ok"
    owner_provider = OwnerProvider()
    service = AgentBridgeApplication(chat_registry, store, FakeProvider(), owner_chat_id=7654321, owner_provider=owner_provider)
    create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0, media_dir=tmp_path / "media")
    async def fetch(_bot, _ref, root, chat_id, message_id):
        path = root / str(chat_id) / f"{message_id}_recovered.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF recovered")
        return path
    monkeypatch.setattr("agentbridge.telegram.bot.materialize_media_ref", fetch)
    await service._answer_owner_query_for_chat(chat_registry.get(-100123456), "Проверь счёт 580")
    row = store.list_chat_attachments(-100123456)[0]
    assert row.download_status == "available"
    assert Path(row.media_path).read_bytes() == b"%PDF recovered"
    assert len(owner_provider.calls[-1]["attachments"]) == 1


@pytest.mark.asyncio
async def test_failed_document_download_is_reported(tmp_path, chat_registry, monkeypatch) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.ingest_telegram_message(
        update_id=12, chat_id=-100123456, message_id=11, sender_id=5, sender_name="Alice",
        telegram_date="2026-09-25T05:00:00+00:00", text="", reply_to_message_id=None,
        role="client", processing_status="processed", media_kind="document",
        telegram_file_id="missing-file", media_filename="missing.pdf", media_mime="application/pdf",
    )
    class OwnerProvider:
        calls: list[dict] = []
        async def answer_owner_query(self, **kwargs):
            self.calls.append(kwargs)
            return "ok"
    owner_provider = OwnerProvider()
    service = AgentBridgeApplication(chat_registry, store, FakeProvider(), owner_chat_id=7654321, owner_provider=owner_provider)
    create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0, media_dir=tmp_path / "media")
    async def fail(_bot, _ref, _root, _chat_id, _message_id):
        return None
    monkeypatch.setattr("agentbridge.telegram.bot.materialize_media_ref", fail)
    await service._answer_owner_query_for_chat(chat_registry.get(-100123456), "Проверь missing.pdf")
    row = store.list_chat_attachments(-100123456)[0]
    assert row.download_status == "download_failed" and row.download_error
    assert "status=download_failed" in owner_provider.calls[-1]["context_pack"]
    assert "attachments" not in owner_provider.calls[-1]


@pytest.mark.asyncio
async def test_failed_episode_keeps_local_file_for_retry(tmp_path, chat_registry) -> None:
    class BoomProvider:
        async def suggest(self, **kwargs) -> AgentReply:
            raise RuntimeError("model down")

    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, BoomProvider())
    photo = tmp_path / "keep.jpg"
    photo.write_bytes(b"jpeg")
    with pytest.raises(RuntimeError, match="model down"):
        await service.handle_messages(
            -100123456,
            [IncomingMessage("Alice", "", update_id=31, message_id=301, media_kind="photo", media_path=str(photo), telegram_file_id="keep-id")],
        )
    assert photo.exists()
    pending = store.pending_messages(-100123456)
    assert pending[0].media_path == str(photo)
    assert pending[0].telegram_file_id == "keep-id"


@pytest.mark.asyncio
async def test_telegram_photo_without_text_is_ingested_and_downloaded(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    media_dir = tmp_path / "media"
    application = create_telegram_application(
        token="test-token", owner_chat_id=7654321, message_service=service,
        batch_seconds=0, media_dir=media_dir,
    )
    bot = DownloadingBot()
    callback = application.handlers[0][0].callback
    await callback(
        FakeUpdate(FakeMessage(photo=[FakePhotoSize("file-photo")], message_id=404), FakeChat(), FakeUser(), 404),
        type("Ctx", (), {"bot": bot})(),
    )
    await asyncio.sleep(0.05)

    assert len(provider.calls) == 1
    assert provider.calls[0]["attachments"]
    assert bot.downloads
    assert not bot.downloads[0].exists()
    assert store.recent_messages(-100123456)[0].telegram_file_id == "file-photo"
    assert store.recent_messages(-100123456)[0].media_path == ""
    assert all(item["chat_id"] == 7654321 for item in bot.sent)
