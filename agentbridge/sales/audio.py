"""Durable audio download/transcription; no deal analysis or Telegram sending here."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

from agentbridge.media import MAX_DOWNLOAD_BYTES, delete_media_file, media_destination
from agentbridge.telegram.media import download_telegram_file
from agentbridge.transcribe import transcribe_audio_file

AUDIO_EXTENSIONS = {".ogg", ".opus", ".mp3", ".m4a", ".mp4", ".wav", ".webm", ".flac", ".mpeg", ".mpga"}


def audio_suffix(header: bytes) -> str:
    """Check the container, rather than trusting a Telegram filename or MIME."""
    if header.startswith(b"OggS") and (b"OpusHead" in header or b"vorbis" in header):
        return ".ogg"
    if header.startswith(b"ID3") or (len(header) > 1 and header[0] == 255 and header[1] & 0xE0 == 0xE0):
        return ".mp3"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return ".wav"
    if header[4:8] == b"ftyp":
        return ".m4a"
    if header.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in header:
        return ".webm"
    if header.startswith(b"fLaC"):
        return ".flac"
    return ""


def failure_reason(exc: Exception) -> tuple[str, bool]:
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            code = detail.get("code", code)
    if status in {401, 403}:
        return "audio_auth", True
    if code in {"insufficient_quota", "billing_hard_limit_reached"}:
        return "audio_quota", True
    if status in {400, 404, 413, 415, 422}:
        return "audio_request", True
    return "audio_transcription_failed", False


class AudioWorker:
    def __init__(self, store, media_dir: Path, api_key: str, model: str, *, transcriber=transcribe_audio_file):
        self.store = store
        self.media_dir = Path(media_dir).resolve()
        self.api_key = api_key
        self.model = model
        self.transcriber = transcriber

    def _path(self, value) -> Path | None:
        if not value:
            return None
        path = Path(value).resolve()
        return path if path.is_relative_to(self.media_dir) else None

    def _failed(self, row, reason: str, terminal: bool = False):
        self.store.audio_failed(row.get("event_id", row.get("id")), reason,
                                terminal=terminal or int(row.get("attempts", 0)) + 1 >= 5)

    async def run_once(self, bot, chat_id: int):
        for row in self.store.audio_cleanup():
            path = self._path(row.get("path"))
            if path:
                await asyncio.to_thread(delete_media_file, path)
                if path.exists():
                    continue
            self.store.audio_cleaned(row.get("event_id", row.get("id")))
        for row in self.store.awaiting_audio(chat_id):
            if float(row.get("retry_at") or 0) > time.time():
                continue
            event_id = row.get("event_id", row.get("id"))
            if int(row.get("attempts", 0)) >= 5:
                self._failed(row, "audio_transcription_failed", True)
                continue
            if int(row.get("file_size") or 0) > MAX_DOWNLOAD_BYTES:
                self._failed(row, "audio_too_large", True)
                continue
            path = self._path(row.get("path"))
            stage = "download"
            try:
                if not path or not path.is_file():
                    remote = await bot.get_file(row["file_id"])
                    if int(getattr(remote, "file_size", None) or 0) > MAX_DOWNLOAD_BYTES:
                        self._failed(row, "audio_too_large", True)
                        continue
                    async def cached_file(_file_id):
                        return remote
                    dest = media_destination(self.media_dir, chat_id, row["message_id"], f"audio_{event_id}.bin", "audio")
                    temp = dest.with_suffix(".part")
                    downloaded = await download_telegram_file(SimpleNamespace(get_file=cached_file), row["file_id"], temp, attempts=1)
                    if downloaded is None:
                        self._failed(row, "audio_download_failed")
                        continue
                    if temp.stat().st_size > MAX_DOWNLOAD_BYTES:
                        delete_media_file(temp)
                        self._failed(row, "audio_too_large", True)
                        continue
                    temp.replace(dest)
                    path = dest
                    self.store.audio_downloaded(event_id, str(path))
                size = path.stat().st_size
                if not size or size > MAX_DOWNLOAD_BYTES:
                    self._failed(row, "audio_empty" if not size else "audio_too_large", True)
                    continue
                with path.open("rb") as stream:
                    suffix = audio_suffix(stream.read(4096))
                if not suffix:
                    self._failed(row, "audio_invalid", True)
                    continue
                if path.suffix != suffix:
                    normalized = path.with_suffix(suffix)
                    path.replace(normalized)
                    path = normalized
                    self.store.audio_downloaded(event_id, str(path))
                if not self.api_key or not self.model:
                    self._failed(row, "audio_auth", True)
                    continue
                stage = "stt"
                transcript = await self.transcriber(path, api_key=self.api_key, model=self.model)
                if not transcript or not transcript.strip():
                    self._failed(row, "audio_transcription_empty", True)
                    continue
                self.store.audio_transcribed(event_id, transcript.strip())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                reason, terminal = failure_reason(exc) if stage == "stt" else ("audio_download_failed", False)
                if stage == 'download' and 'file is too big' in str(exc).lower():
                    reason, terminal = 'audio_too_large', True
                self._failed(row, reason, terminal)
