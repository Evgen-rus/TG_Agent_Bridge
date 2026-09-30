from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from openai_codex.errors import TransportClosedError

from agentbridge.agents.codex import (
    CodexProvider,
    CodexTransportClosed,
    _raise_turn_failure,
    codex_failure_hint,
)
from agentbridge.application import AgentBridgeApplication, OwnerQueryResult
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class ExplainingProvider:
    prompt_version: int = 1
    error: object = None
    calls: list[dict] = field(default_factory=list)

    def explain_failure(self, error: object) -> tuple[str, str]:
        return ("codex_sandbox_missing", "на VPS нет bubblewrap")

    async def answer_owner_query(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "ок"


def _application(tmp_path, provider: ExplainingProvider) -> AgentBridgeApplication:
    application = AgentBridgeApplication.__new__(AgentBridgeApplication)
    application.store = ChatThreadStore(tmp_path / "state.sqlite3")
    application.owner_provider = provider
    application.registry = None
    application.logger = None
    return application


def test_codex_failure_hint_detects_missing_bubblewrap() -> None:
    label, hint = codex_failure_hint(
        CodexTransportClosed(
            "TransportClosedError: Codex could not find bubblewrap on PATH. "
            "Install bubblewrap with your OS package manager."
        )
    )
    assert label == "codex_sandbox_missing"
    assert "bubblewrap" in hint


def test_codex_failure_hint_distinguishes_plain_transport_closed() -> None:
    label, _ = codex_failure_hint(CodexTransportClosed("Codex process closed stdout."))
    assert label == "codex_transport_closed"


def test_codex_failure_hint_recognises_usage_limit() -> None:
    label, _ = codex_failure_hint("You've hit your usage limit. try again at 11:27 AM")
    assert label == "codex_usage_limit"


def test_codex_failure_hint_recognises_auth_failure() -> None:
    label, _ = codex_failure_hint("401 Unauthorized: invalid_api_key")
    assert label == "codex_auth"


def test_codex_failure_hint_never_leaks_error_text() -> None:
    error = CodexTransportClosed("failed with key sk-secret-value-123456 for /home/rick/secret.txt")
    label, hint = codex_failure_hint(error)
    assert "sk-secret" not in label
    assert "sk-secret" not in hint
    assert "/home/rick" not in hint


def test_codex_failure_hint_falls_back_to_generic_label() -> None:
    label, _ = codex_failure_hint(TimeoutError("read timed out"))
    assert label == "codex_turn_failed"


def test_turn_failure_preserves_stderr_reason() -> None:
    stderr = "ERROR codex_app_server: Codex could not find bubblewrap on PATH."
    try:
        _raise_turn_failure(TransportClosedError(stderr))
    except CodexTransportClosed as exc:
        assert "bubblewrap" in str(exc)
    else:
        raise AssertionError("expected CodexTransportClosed")


def test_owner_failure_text_uses_provider_explanation(tmp_path) -> None:
    application = _application(tmp_path, ExplainingProvider())
    text = application._owner_failure_text(TimeoutError("boom"), what="общую задачу")
    assert "codex_sandbox_missing" in text
    assert "bubblewrap" in text
    assert "boom" not in text


def test_owner_failure_text_survives_provider_without_explanation(tmp_path) -> None:
    @dataclass
    class DumbProvider:
        prompt_version: int = 1

    application = _application(tmp_path, DumbProvider())
    text = application._owner_failure_text(TimeoutError("boom"))
    assert "Не удалось выполнить задачу." in text


def test_owner_failure_text_never_raises_on_explaining_provider(tmp_path) -> None:
    @dataclass
    class BrokenProvider:
        prompt_version: int = 1

        def explain_failure(self, error: object) -> tuple[str, str]:
            raise ValueError("explainer is broken")

    application = _application(tmp_path, BrokenProvider())
    text = application._owner_failure_text(TimeoutError("boom"))
    assert "Не удалось выполнить задачу." in text


def test_owner_query_result_carries_failure_text(tmp_path) -> None:
    application = _application(tmp_path, ExplainingProvider())
    result = application._owner_failure_text(TimeoutError("boom"), what="понимание задачи")
    assert isinstance(OwnerQueryResult(result), OwnerQueryResult)
    assert "Причина:" in result


@pytest.mark.asyncio
async def test_second_transport_closed_keeps_its_type() -> None:
    """После второго обрыва тип сохраняется, иначе метка для владельца
    скатилась бы в общую `codex_turn_failed`."""
    provider = CodexProvider()
    calls: list[int] = []

    def always_closed(*args) -> str:
        calls.append(1)
        raise CodexTransportClosed("Codex process closed stdout.")

    with pytest.raises(CodexTransportClosed):
        await provider._owner_turn_with_retry(always_closed)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_second_transport_closed_explains_as_transport_closed() -> None:
    provider = CodexProvider()

    def always_closed(*args) -> str:
        raise CodexTransportClosed("Codex process closed stdout.")

    with pytest.raises(CodexTransportClosed) as raised:
        await provider._owner_turn_with_retry(always_closed)
    label, _ = codex_failure_hint(raised.value)
    assert label == "codex_transport_closed"
