"""Speech-to-text for client voice messages via the OpenAI transcription API."""

from __future__ import annotations

import asyncio
import io
import logging

logger = logging.getLogger(__name__)

# Голосовые Telegram приходят в OGG/OPUS; API принимает их без конвертации.
DEFAULT_LANGUAGE = "ru"


class TranscriptionError(RuntimeError):
    """Транскрибация не удалась (сеть, ключ, модель). Сообщение остаётся pending."""


async def transcribe_audio_bytes(
    audio: bytes,
    *,
    api_key: str,
    model: str,
    filename: str = "voice.ogg",
    language: str = DEFAULT_LANGUAGE,
) -> str:
    if not api_key:
        raise TranscriptionError("OPENAI_API_KEY is not configured")
    if not model:
        raise TranscriptionError("TRANSCRIPTION_MODEL is not configured")
    if not audio:
        return ""
    # Клиент OpenAI держит свой httpx-пул; создаём его на вызов, чтобы не тянуть
    # глобальное состояние между эпизодами разных чатов.
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    try:
        transcript = await client.audio.transcriptions.create(
            model=model,
            file=(filename, io.BytesIO(audio)),
            language=language,
        )
    finally:
        await client.close()
    text = str(getattr(transcript, "text", "") or "").strip()
    logger.info("event=voice_transcribed model=%s bytes=%d chars=%d", model, len(audio), len(text))
    return text


async def transcribe_audio_file(path, *, api_key: str, model: str) -> str:
    """Читает уже скачанный файл с диска и транскрибирует его содержимое."""
    data = await asyncio.to_thread(_read_bytes, path)
    return await transcribe_audio_bytes(data, api_key=api_key, model=model)


def _read_bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()
