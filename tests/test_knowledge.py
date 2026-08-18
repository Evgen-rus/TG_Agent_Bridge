from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentbridge.agents.base import AgentReply
from agentbridge.application import AgentBridgeApplication
from agentbridge.chats.loader import ChatConfig, ChatRegistry
from agentbridge.knowledge import load_knowledge_pack, parse_knowledge_pack
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class FakeProvider:
    calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return AgentReply("thread-1", f"Situation after: {kwargs['message']}", "Reply")


def _write_pack(knowledge_dir: Path) -> None:
    pack = knowledge_dir / "leadgenbureau"
    pack.mkdir(parents=True)
    (pack / "core.md").write_text("CORE: идентификация не лид. Недозвон не отказ.", encoding="utf-8")
    (pack / "cases.md").write_text("INTERNAL CASE: компания СекретПлюс, конверсия 12%.", encoding="utf-8")
    (pack / "playbooks.md").write_text("Сначала спросить актуальность потребности.", encoding="utf-8")
    (pack / "README.md").write_text("Human readme, not for every prompt.", encoding="utf-8")


def _registry(*chats: ChatConfig) -> ChatRegistry:
    return ChatRegistry({chat.telegram_chat_id: chat for chat in chats})


def test_parse_knowledge_pack_defaults_and_opt_out() -> None:
    assert parse_knowledge_pack(None) == "leadgenbureau"
    assert parse_knowledge_pack("none") is None
    assert parse_knowledge_pack("  OFF ") is None
    assert parse_knowledge_pack("custom") == "custom"


def test_missing_knowledge_directory_returns_empty(tmp_path) -> None:
    assert load_knowledge_pack(tmp_path / "missing", "leadgenbureau") == ""
    assert load_knowledge_pack(tmp_path, None) == ""


def test_knowledge_pack_loads_core_and_lists_extra_files_only(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    _write_pack(knowledge_dir)
    text = load_knowledge_pack(knowledge_dir, "leadgenbureau")
    assert "CORE: идентификация не лид" in text
    assert "knowledge/leadgenbureau/playbooks.md [shared]" in text.replace("\\", "/")
    assert "knowledge/leadgenbureau/cases.md [internal]" in text.replace("\\", "/")
    assert "INTERNAL CASE: компания СекретПлюс" not in text
    assert "Human readme" not in text


@pytest.mark.asyncio
async def test_shared_core_is_injected_only_for_attached_chat(tmp_path) -> None:
    knowledge_dir = tmp_path / "knowledge"
    _write_pack(knowledge_dir)
    attached = ChatConfig(
        -1001, "Lead chat", "codex", "Wiki клиента А: бетон.", Path("chats/a"),
        knowledge_pack="leadgenbureau",
    )
    other = ChatConfig(
        -1002, "Other project", "codex", "Wiki клиента B: нейророп.", Path("chats/b"),
        knowledge_pack=None,
    )
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(
        _registry(attached, other), store, provider, knowledge_dir=knowledge_dir,
    )
    await service.handle_message(-1001, "Alice", "Мы прозвонили, никто не берёт трубку.")
    await service.handle_message(-1002, "Bob", "Как там релиз?")

    lead_pack = str(provider.calls[0]["context_pack"])
    other_pack = str(provider.calls[1]["context_pack"])
    assert "Wiki клиента А: бетон." in lead_pack
    assert "CORE: идентификация не лид" in lead_pack
    assert "INTERNAL CASE: компания СекретПлюс" not in lead_pack
    assert "Wiki клиента B: нейророп." not in lead_pack
    assert "CORE: идентификация не лид" not in other_pack
    assert "Wiki клиента А: бетон." not in other_pack
    assert provider.calls[0]["wiki"] == "Wiki клиента А: бетон."


@pytest.mark.asyncio
async def test_missing_pack_does_not_break_suggest(tmp_path) -> None:
    chat = ChatConfig(
        -1001, "Lead chat", "codex", "Клиентский wiki.", Path("chats/a"),
        knowledge_pack="leadgenbureau",
    )
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(
        _registry(chat), store, provider, knowledge_dir=tmp_path / "absent",
    )
    result = await service.handle_message(-1001, "Alice", "Нужна выгрузка")
    assert result is not None
    pack = str(provider.calls[0]["context_pack"])
    assert "Клиентский wiki." in pack
    assert "Общие знания" not in pack
    assert store.get_thread_id(-1001) == "thread-1"
