from __future__ import annotations

from agentbridge.settings import Settings


def test_owner_codex_defaults_match_current_configuration(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "7654321")
    monkeypatch.delenv("CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("OWNER_CODEX_MODEL", raising=False)
    monkeypatch.delenv("OWNER_CODEX_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_MODEL", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_SIZE", raising=False)
    monkeypatch.delenv("IMAGE_GENERATION_QUALITY", raising=False)

    settings = Settings.from_env(tmp_path)

    assert settings.codex_model == "gpt-6-luna"
    assert settings.codex_reasoning_effort == "xhigh"
    assert settings.owner_codex_model == "gpt-6-luna"
    assert settings.owner_codex_reasoning_effort == "xhigh"
    assert settings.sepia_enabled is True
    assert settings.image_generation_model == "gpt-image-2.5-flare"
    assert settings.image_generation_size == "1024x1024"
    assert settings.image_generation_quality == "low"


def test_sepia_can_be_disabled(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "7654321")
    monkeypatch.setenv("SEPIA_ENABLED", "off")

    assert Settings.from_env(tmp_path).sepia_enabled is False


def test_owner_codex_model_and_reasoning_are_configurable(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("OWNER_CHAT_ID", "7654321")
    monkeypatch.setenv("OWNER_CODEX_MODEL", "gpt-6-sol")
    monkeypatch.setenv("OWNER_CODEX_REASONING_EFFORT", "none")

    settings = Settings.from_env(tmp_path)

    assert settings.owner_codex_model == "gpt-6-sol"
    assert settings.owner_codex_reasoning_effort == "none"
