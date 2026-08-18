from __future__ import annotations

from agentbridge.chats.loader import ChatRegistry, write_new_chat


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
    extra = write_new_chat(
        tmp_path, telegram_chat_id=-100555, name="Новый клиент", wiki="Контекст нового клиента.",
    )
    added = registry.add(extra)

    selected = registry.get(-100123456)
    assert selected is not None
    assert selected.name == "Acme Support"
    assert selected.wiki == "Acme-specific context."
    assert registry.get(-100000000) is None
    assert added.name == "Новый клиент"
    assert (added.directory / "config.yaml").is_file()
    assert "Контекст нового клиента." in (added.directory / "wiki.md").read_text(encoding="utf-8")
    assert ChatRegistry.load(tmp_path).get(-100555) is not None
