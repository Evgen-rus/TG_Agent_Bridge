from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
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


@dataclass
class FakeMessage:
    text: str = ""
    caption: str | None = None
    photo: list | None = None
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
    assert not first.exists() and not second.exists() and not pdf.exists()


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
