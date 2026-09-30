from __future__ import annotations

from decimal import Decimal

import pytest

from agentbridge.speech import (
    MAX_SPEECH_BYTES,
    MODEL_CATALOG_TTL_SECONDS,
    SpeechProviderError,
    SpeechProviderRegistry,
    OpenRouterSpeechProvider,
)


FREE_MODEL = "fish-audio/s2.1-pro-free:free"


def _catalog(model: str = FREE_MODEL, *, prompt: str = "0", completion: str = "0", speech: bool = True) -> bytes:
    import json

    return json.dumps({
        "data": [{
            "id": model,
            "architecture": {"output_modalities": ["speech"] if speech else ["text"]},
            "pricing": {"prompt": prompt, "completion": completion},
        }],
    }).encode()


class FakeProvider:
    """Провайдер с фиксированной ценой, чтобы проверять выбор, а не HTTP."""

    def __init__(self, name: str, quote: Decimal | None, error: Exception | None = None) -> None:
        self.name = name
        self._quote = quote
        self._error = error
        self.quote_calls = 0
        self.texts: list[str] = []

    async def quote_usd(self, text: str) -> Decimal | None:
        self.quote_calls += 1
        return self._quote

    async def synthesize(self, text: str):
        self.texts.append(text)
        if self._error is not None:
            raise self._error
        from agentbridge.speech import SpeechArtifact

        return SpeechArtifact(data=b"ID3-mp3")


@pytest.mark.asyncio
async def test_confirmed_free_model_is_used_with_zero_ceiling() -> None:
    provider = FakeProvider("openrouter", Decimal("0"))
    registry = SpeechProviderRegistry((provider,))

    audio = await registry.synthesize("Привет", provider_order=("openrouter",), max_cost_usd=Decimal("0"))

    assert audio.data == b"ID3-mp3"
    assert provider.texts == ["Привет"]


@pytest.mark.asyncio
async def test_paid_quote_above_ceiling_is_skipped() -> None:
    provider = FakeProvider("openrouter", Decimal("0.01"))
    registry = SpeechProviderRegistry((provider,))

    with pytest.raises(SpeechProviderError) as excinfo:
        await registry.synthesize("Привет", provider_order=("openrouter",), max_cost_usd=Decimal("0"))

    assert excinfo.value.reason == "no_provider_within_cost_limit"
    assert provider.texts == []


@pytest.mark.asyncio
async def test_unknown_pricing_fails_closed() -> None:
    provider = FakeProvider("openrouter", None)
    registry = SpeechProviderRegistry((provider,))

    with pytest.raises(SpeechProviderError) as excinfo:
        await registry.synthesize("Привет", provider_order=("openrouter",), max_cost_usd=Decimal("10"))

    assert excinfo.value.reason == "no_provider_within_cost_limit"
    assert provider.texts == []


@pytest.mark.asyncio
async def test_missing_provider_falls_through_to_next_one() -> None:
    missing = FakeProvider("missing", Decimal("0"))
    fallback = FakeProvider("openrouter", Decimal("0"))
    registry = SpeechProviderRegistry((missing, fallback))

    audio = await registry.synthesize(
        "Привет", provider_order=("absent", "openrouter"), max_cost_usd=Decimal("0"),
    )

    assert audio.data == b"ID3-mp3"
    assert fallback.texts == ["Привет"]


@pytest.mark.asyncio
async def test_duplicate_provider_names_are_not_tried_twice() -> None:
    provider = FakeProvider("openrouter", Decimal("0"))
    registry = SpeechProviderRegistry((provider,))

    await registry.synthesize(
        "Привет", provider_order=("openrouter", "OpenRouter", " openrouter "), max_cost_usd=Decimal("0"),
    )

    assert provider.quote_calls == 1
    assert provider.texts == ["Привет"]


def _openrouter(monkeypatch, *, catalog: bytes, model: str = FREE_MODEL) -> tuple[OpenRouterSpeechProvider, list[str]]:
    """Провайдер с подменённым HTTP: настоящий urlopen в тестах не используется."""
    provider = OpenRouterSpeechProvider("secret-key", model=model)
    catalog_calls: list[str] = []

    def fake_request(path: str, body, byte_limit):
        catalog_calls.append(path)
        return catalog

    monkeypatch.setattr(provider, "_request", fake_request)
    return provider, catalog_calls


@pytest.mark.asyncio
async def test_catalog_is_fetched_once_and_reused_within_ttl(monkeypatch) -> None:
    provider, calls = _openrouter(monkeypatch, catalog=_catalog())

    assert await provider.quote_usd("Привет") == Decimal("0")
    assert await provider.quote_usd("Пока") == Decimal("0")
    assert await provider.quote_usd("Ещё раз") == Decimal("0")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_catalog_is_refetched_after_ttl(monkeypatch) -> None:
    provider, calls = _openrouter(monkeypatch, catalog=_catalog())
    assert await provider.quote_usd("Привет") == Decimal("0")

    # TTL истекает: цена обязана проверяться заново, а не по старому ответу.
    provider._catalog_expires_at -= MODEL_CATALOG_TTL_SECONDS + 1
    assert await provider.quote_usd("Пока") == Decimal("0")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_failed_catalog_refresh_does_not_serve_stale_price(monkeypatch) -> None:
    """Сбой обновления не должен ни разрешить модель, ни закрепить старый кэш.

    Цена проверяется fail closed: ошибка обновления поднимается наружу, а
    просроченный кэш не продлевается, поэтому следующий запрос снова идёт
    в сеть, а не тихо отвечает по устаревшей цене.
    """
    provider, calls = _openrouter(monkeypatch, catalog=_catalog())
    assert await provider.quote_usd("Привет") == Decimal("0")
    assert len(calls) == 1

    provider._catalog_expires_at -= MODEL_CATALOG_TTL_SECONDS + 1

    def failing_request(path: str, body, byte_limit):
        raise SpeechProviderError("http_500")

    monkeypatch.setattr(provider, "_request", failing_request)

    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.quote_usd("Пока")
    assert excinfo.value.reason == "http_500"

    # Кэш остался просроченным: следующий вызов обязан снова обратиться к сети.
    with pytest.raises(SpeechProviderError):
        await provider.quote_usd("Ещё раз")


@pytest.mark.asyncio
async def test_concurrent_quotes_share_one_catalog_request(monkeypatch) -> None:
    import asyncio

    provider, calls = _openrouter(monkeypatch, catalog=_catalog())

    await asyncio.gather(*(provider.quote_usd(f"Привет {index}") for index in range(5)))

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_missing_api_key_is_not_a_free_quote(monkeypatch) -> None:
    provider = OpenRouterSpeechProvider("   ", model=FREE_MODEL)
    monkeypatch.setattr(
        provider, "_request", lambda *a, **k: pytest.fail("каталог не должен запрашиваться без ключа"),
    )

    assert await provider.quote_usd("Привет") is None
    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.synthesize("Привет")
    assert excinfo.value.reason == "provider_not_configured"


@pytest.mark.asyncio
async def test_non_speech_model_is_not_quoted(monkeypatch) -> None:
    provider = OpenRouterSpeechProvider("key", model=FREE_MODEL)
    monkeypatch.setattr(provider, "_request", lambda *a, **k: _catalog(speech=False))

    assert await provider.quote_usd("Привет") is None


@pytest.mark.asyncio
async def test_synthesize_rejects_wrong_mime_and_oversized_audio(monkeypatch) -> None:
    provider = OpenRouterSpeechProvider("key")

    provider._request = lambda *a, **k: (b"not-audio", "text/plain")
    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.synthesize("Привет")
    assert excinfo.value.reason == "unsupported_audio_response"

    provider._request = lambda *a, **k: (b"x" * (MAX_SPEECH_BYTES + 1), "audio/mpeg")
    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.synthesize("Привет")
    assert excinfo.value.reason == "unsupported_audio_response"


@pytest.mark.asyncio
async def test_too_long_text_is_refused_before_any_provider_call() -> None:
    provider = FakeProvider("openrouter", Decimal("0"))
    registry = SpeechProviderRegistry((provider,))

    with pytest.raises(SpeechProviderError) as excinfo:
        await registry.synthesize(
            "я" * 12_001, provider_order=("openrouter",), max_cost_usd=Decimal("0"),
        )

    assert excinfo.value.reason == "text_too_long"
    assert provider.quote_calls == 0
    assert provider.texts == []


@pytest.mark.asyncio
async def test_http_failures_become_fixed_labels_without_body_or_key(monkeypatch) -> None:
    """401/429/5xx -> фиксированная метка; тело ответа и ключ не уходят дальше.

    Здесь подменяется сам `urlopen`: конвертацию ошибок делает `_request`,
    и подменить её было бы значило протестировать заглушку вместо контракта.
    """
    from urllib.error import HTTPError, URLError
    import agentbridge.speech as speech_module

    provider = OpenRouterSpeechProvider("SUPER-SECRET-KEY")

    for code, expected in ((401, "http_401"), (429, "http_429"), (503, "http_503")):
        def raising_http(request, timeout=None, code=code):
            raise HTTPError(
                f"https://openrouter.ai/api/v1/audio/speech", code,
                "quota exceeded for SUPER-SECRET-KEY", {}, None,
            )

        monkeypatch.setattr(speech_module, "urlopen", raising_http)
        with pytest.raises(SpeechProviderError) as excinfo:
            await provider.synthesize("Привет")
        assert excinfo.value.reason == expected
        assert "SUPER-SECRET-KEY" not in str(excinfo.value)
        assert "quota" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_network_timeout_is_a_fixed_label_without_url(monkeypatch) -> None:
    from urllib.error import URLError
    import agentbridge.speech as speech_module

    provider = OpenRouterSpeechProvider("SUPER-SECRET-KEY")

    def raising_network(request, timeout=None):
        raise URLError("timed out calling https://openrouter.ai/api/v1/audio/speech with SUPER-SECRET-KEY")

    monkeypatch.setattr(speech_module, "urlopen", raising_network)
    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.synthesize("Привет")

    assert excinfo.value.reason == "network_error"
    assert "openrouter.ai" not in str(excinfo.value)
    assert "SUPER-SECRET-KEY" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_oversized_speech_response_is_refused(monkeypatch) -> None:
    import agentbridge.speech as speech_module

    class Response:
        headers = type("H", (), {"get_content_type": lambda self: "audio/mpeg"})()

        def read(self, limit):
            return b"x" * (MAX_SPEECH_BYTES + 1)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(speech_module, "urlopen", lambda request, timeout=None: Response())
    provider = OpenRouterSpeechProvider("key")

    with pytest.raises(SpeechProviderError) as excinfo:
        await provider.synthesize("Привет")
    assert excinfo.value.reason == "response_too_large"


@pytest.mark.asyncio
async def test_registry_surfaces_last_provider_failure(monkeypatch) -> None:
    """Ошибка синтеза не маскируется как «нет провайдера в лимите»."""
    provider = FakeProvider("openrouter", Decimal("0"), error=SpeechProviderError("http_429"))
    registry = SpeechProviderRegistry((provider,))

    with pytest.raises(SpeechProviderError) as excinfo:
        await registry.synthesize("Привет", provider_order=("openrouter",), max_cost_usd=Decimal("0"))

    assert excinfo.value.reason == "http_429"