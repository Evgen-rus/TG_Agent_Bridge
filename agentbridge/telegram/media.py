"""Extract and download Telegram client attachments without keeping a local dump."""

from __future__ import annotations

import logging
from pathlib import Path

from agentbridge.media import MAX_DOWNLOAD_BYTES, MediaRef, media_destination

logger = logging.getLogger(__name__)


def message_text(message) -> str:
    return (getattr(message, "text", None) or getattr(message, "caption", None) or "").strip()


def describe_message_media(message) -> MediaRef | None:
    if message is None:
        return None
    group_id = str(getattr(message, "media_group_id", None) or "")
    photos = getattr(message, "photo", None) or ()
    if photos:
        best = max(photos, key=lambda item: int(getattr(item, "file_size", 0) or 0))
        file_id = str(getattr(best, "file_id", "") or "")
        if file_id:
            return MediaRef(
                kind="photo",
                file_id=file_id,
                filename="photo.jpg",
                mime="image/jpeg",
                file_size=getattr(best, "file_size", None),
                media_group_id=group_id,
            )
    voice = getattr(message, "voice", None)
    if voice is not None:
        file_id = str(getattr(voice, "file_id", "") or "")
        if file_id:
            return MediaRef(
                kind="voice",
                file_id=file_id,
                filename="voice.ogg",
                mime=str(getattr(voice, "mime_type", "") or "audio/ogg"),
                file_size=getattr(voice, "file_size", None),
                media_group_id=group_id,
            )
    document = getattr(message, "document", None)
    if document is not None:
        file_id = str(getattr(document, "file_id", "") or "")
        if file_id:
            return MediaRef(
                kind="document",
                file_id=file_id,
                filename=str(getattr(document, "file_name", "") or "document"),
                mime=str(getattr(document, "mime_type", "") or ""),
                file_size=getattr(document, "file_size", None),
                media_group_id=group_id,
            )
    return None


def has_client_content(message) -> bool:
    return bool(message_text(message) or describe_message_media(message))


async def download_telegram_file(bot, file_id: str, dest: Path) -> Path | None:
    if not file_id or bot is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        telegram_file = await bot.get_file(file_id)
        await telegram_file.download_to_drive(custom_path=str(dest))
    except Exception:
        logger.warning("event=media_download_failed file_id_present=1 dest=%s", dest)
        return None
    if not dest.is_file() or dest.stat().st_size <= 0:
        return None
    return dest


async def materialize_media_ref(bot, ref: MediaRef, root: Path, chat_id: int, message_id: int) -> Path | None:
    size = ref.file_size
    if isinstance(size, int) and size > MAX_DOWNLOAD_BYTES:
        logger.info("event=media_skipped reason=too_large chat_id=%s message_id=%s size=%s", chat_id, message_id, size)
        return None
    dest = media_destination(root, chat_id, message_id, ref.filename, ref.kind)
    return await download_telegram_file(bot, ref.file_id, dest)
