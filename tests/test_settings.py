from __future__ import annotations

from agentbridge.settings import Settings


def test_owner_codex_defaults_are_separate_from_client(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "7654321")
    monkeypatch.delenv("OWNER_CODEX_MODEL", raising=False)
    monkeypatch.delenv("OWNER_CODEX_REASONING_EFFORT", raising=False)

    settings = Settings.from_env(tmp_path)

    assert settings.codex_model == "gpt-5.6-luna"
    assert settings.codex_reasoning_effort == "xhigh"
    assert settings.owner_codex_model == "gpt-5.6-sol"
    assert settings.owner_codex_reasoning_effort == "low"


def test_owner_codex_model_and_reasoning_are_configurable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "7654321")
    monkeypatch.setenv("OWNER_CODEX_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("OWNER_CODEX_REASONING_EFFORT", "none")

    settings = Settings.from_env(tmp_path)

    assert settings.owner_codex_model == "gpt-5.6-terra"
    assert settings.owner_codex_reasoning_effort == "none"
