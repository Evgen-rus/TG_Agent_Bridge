from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
MAX_SPEECH_BYTES = 10 * 1024 * 1024
MAX_MODEL_CATALOG_BYTES = 8 * 1024 * 1024
MAX_SPEECH_TEXT_CHARS = 12_000
# Каталог цен OpenRouter меняется редко, а владелец спрашивает голосом часто.
# Короткий TTL держит проверку цены свежей, не превращая каждый ответ
# владельца в дополнительный запрос к каталогу.
MODEL_CATALOG_TTL_SECONDS = 900.0


class SpeechProviderError(RuntimeError):
    """Speech synthesis failed; reason is a fixed, secret-free diagnostic label."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SpeechArtifact:
    data: bytes
    media_type: str = "audio/mpeg"
    extension: str = ".mp3"


class SpeechProvider(Protocol):
    name: str

    async def quote_usd(self, text: str) -> Decimal | None: ...

    async def synthesize(self, text: str) -> SpeechArtifact: ...


class SpeechProviderRegistry:
    """Chooses among registered providers in the configured order and cost limit."""

    def __init__(self, providers: tuple[SpeechProvider, ...] = ()) -> None:
        self._providers: dict[str, SpeechProvider] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: SpeechProvider) -> None:
        self._providers[provider.name.casefold()] = provider

    async def synthesize(
        self,
        text: str,
        *,
        provider_order: tuple[str, ...],
        max_cost_usd: Decimal,
    ) -> SpeechArtifact:
        if not text.strip():
            raise SpeechProviderError("empty_text")
        if len(text) > MAX_SPEECH_TEXT_CHARS:
            raise SpeechProviderError("text_too_long")
        failures: list[SpeechProviderError] = []
        visited: set[str] = set()
        for provider_name in provider_order:
            name = provider_name.strip().casefold()
            if not name or name in visited:
                continue
            visited.add(name)
            provider = self._providers.get(name)
            if provider is None:
                continue
            try:
                quote = await provider.quote_usd(text)
                if quote is None or quote < 0 or quote > max_cost_usd:
                    continue
                return await provider.synthesize(text)
            except SpeechProviderError as exc:
                failures.append(exc)
        if failures:
            raise failures[-1]
        raise SpeechProviderError("no_provider_within_cost_limit")


class OpenRouterSpeechProvider:
    """OpenRouter TTS adapter. Unknown prices fail closed under the shared selector."""

    name = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "fish-audio/s2.1-pro-free:free",
        voice: str = "",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._api_key = api_key.strip()
        self.model = model.strip()
        self.voice = voice.strip()
        self.timeout_seconds = timeout_seconds
        self._catalog: bytes | None = None
        self._catalog_expires_at = 0.0
        self._catalog_lock = asyncio.Lock()

    async def _model_catalog(self) -> bytes:
        """Каталог моделей из памяти, обновляется не чаще одного раза за TTL.

        Кэш только ускоряет проверку: цена берётся из того же ответа `/models`,
        а после истечения TTL каталог запрашивается заново. Неудачное обновление
        кэш не продлевает и оставляет предыдущие данные просроченными, поэтому
        цена проверяется заново, а не по устаревшему остатку TTL.
        """
        now = time.monotonic()
        cached = self._catalog
        if cached is not None and now < self._catalog_expires_at:
            return cached
        async with self._catalog_lock:
            now = time.monotonic()
            if self._catalog is not None and now < self._catalog_expires_at:
                return self._catalog
            try:
                payload = await asyncio.to_thread(
                    self._request, "/models?output_modalities=speech", None, MAX_MODEL_CATALOG_BYTES,
                )
            except SpeechProviderError:
                raise
            self._catalog = payload
            self._catalog_expires_at = now + MODEL_CATALOG_TTL_SECONDS
            return payload

    async def quote_usd(self, text: str) -> Decimal | None:
        if not self._api_key:
            return None
        payload = await self._model_catalog()
        try:
            catalog = json.loads(payload)
            model = next(item for item in catalog["data"] if item.get("id") == self.model)
            modalities = model.get("architecture", {}).get("output_modalities", [])
            if "speech" not in modalities:
                return None
            pricing = model["pricing"]
            prompt_price = Decimal(str(pricing["prompt"]))
            completion_price = Decimal(str(pricing["completion"]))
            if not prompt_price.is_finite() or not completion_price.is_finite():
                return None
        except (KeyError, StopIteration, TypeError, ValueError, InvalidOperation):
            return None
        if prompt_price == 0 and completion_price == 0:
            return Decimal("0")
        # Fish Audio's paid OpenRouter models price speech input per UTF-8 byte;
        # output audio is listed as zero. Other paid pricing shapes stay blocked
        # until an adapter can provide a reliable estimate for them.
        if self.model.startswith("fish-audio/") and prompt_price > 0 and completion_price == 0:
            return prompt_price * Decimal(len(text.encode("utf-8")))
        return None

    async def synthesize(self, text: str) -> SpeechArtifact:
        if not self._api_key:
            raise SpeechProviderError("provider_not_configured")
        body: dict[str, str] = {
            "model": self.model,
            "input": text,
            "response_format": "mp3",
        }
        if self.voice:
            body["voice"] = self.voice
        data, media_type = await asyncio.to_thread(
            self._request, "/audio/speech", body, MAX_SPEECH_BYTES,
        )
        if not data or len(data) > MAX_SPEECH_BYTES or media_type not in {"audio/mpeg", "audio/mp3"}:
            raise SpeechProviderError("unsupported_audio_response")
        return SpeechArtifact(data=data)

    def _request(
        self, path: str, body: dict[str, str] | None, byte_limit: int,
    ) -> tuple[bytes, str] | bytes:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = Request(f"{OPENROUTER_API_BASE}{path}", data=data, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read(byte_limit + 1)
                if len(payload) > byte_limit:
                    raise SpeechProviderError("response_too_large")
                media_type = response.headers.get_content_type().lower()
        except HTTPError as exc:
            raise SpeechProviderError(f"http_{exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise SpeechProviderError("network_error") from None
        except SpeechProviderError:
            raise
        except Exception:
            raise SpeechProviderError("request_failed") from None
        if body is None:
            return payload
        return payload, media_type
