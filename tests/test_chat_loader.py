from __future__ import annotations

from agentbridge.chats.loader import ChatRegistry


def test_registry_selects_the_matching_chat_and_its_wiki(tmp_path) -> None:
    first = tmp_path / "acme"
    first.mkdir()
    (first / "config.yaml").write_text(
        "name: Acme Support\ntelegram_chat_id: -100123456\nagent_provider: codex\n",
        encoding="utf-8",
    )
    (first / "wiki.md").write_text("Acme-specific context.", encoding="utf-8")

    second = tmp_path / "other"
    second.mkdir()
    (second / "config.yaml").write_text(
        "name: Other Chat\ntelegram_chat_id: -100987654\nagent_provider: codex\n",
        encoding="utf-8",
    )
    (second / "wiki.md").write_text("Other context.", encoding="utf-8")

    registry = ChatRegistry.load(tmp_path)

    selected = registry.get(-100123456)
    assert selected is not None
    assert selected.name == "Acme Support"
    assert selected.wiki == "Acme-specific context."
    assert registry.get(-100000000) is None
