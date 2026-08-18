from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply
from agentbridge.application import AgentBridgeApplication, IncomingMessage
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class FakeProvider:
    calls: list[dict] = field(default_factory=list)
    reply: AgentReply = field(default_factory=lambda: AgentReply("thread-1", "Situation", "Reply"))

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return self.reply


@dataclass
class BoomProvider:
    async def suggest(self, **kwargs) -> AgentReply:
        raise RuntimeError("model down")


@pytest.mark.asyncio
async def test_telegram_update_is_saved_before_model_processing(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, BoomProvider())

    with pytest.raises(RuntimeError, match="model down"):
        await service.handle_messages(
            -100123456,
            [IncomingMessage("Alice", "Can I get the docs?", update_id=11, message_id=101)],
        )

    pending = store.pending_messages(-100123456)
    assert len(pending) == 1
    assert pending[0].update_id == 11
    assert pending[0].text == "Can I get the docs?"
    assert store.is_update_processed(11) is False


@pytest.mark.asyncio
async def test_duplicate_update_does_not_create_a_second_inbox_row(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, FakeProvider())
    first = service.ingest_telegram_message(
        update_id=11, chat_id=-100123456, message_id=101, sender_id=5,
        sender_name="Alice", telegram_date="2026-08-18T00:00:00", text="Hello",
        reply_to_message_id=None, is_owner_chat=False,
    )
    second = service.ingest_telegram_message(
        update_id=11, chat_id=-100123456, message_id=101, sender_id=5,
        sender_name="Alice", telegram_date="2026-08-18T00:00:00", text="Hello",
        reply_to_message_id=None, is_owner_chat=False,
    )

    assert first is True
    assert second is False
    assert len(store.pending_messages(-100123456)) == 1


@pytest.mark.asyncio
async def test_failed_processing_is_retried_after_restart(tmp_path, chat_registry) -> None:
    database_path = tmp_path / "agentbridge.sqlite3"
    store = ChatThreadStore(database_path)
    failing = AgentBridgeApplication(chat_registry, store, BoomProvider())
    with pytest.raises(RuntimeError):
        await failing.handle_message(-100123456, "Alice", "Need a quote", update_id=21)

    restarted_store = ChatThreadStore(database_path)
    provider = FakeProvider()
    restarted = AgentBridgeApplication(chat_registry, restarted_store, provider)
    results = await restarted.catch_up()

    assert len(results) == 1
    assert results[0].original_message == "Need a quote"
    assert len(provider.calls) == 1
    assert restarted_store.is_update_processed(21) is True
    assert restarted_store.pending_messages(-100123456) == []
