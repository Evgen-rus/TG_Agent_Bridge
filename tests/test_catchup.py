from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentbridge.agents.base import AgentAction, AgentReply
from agentbridge.application import AgentBridgeApplication, IncomingMessage
from agentbridge.chats.loader import ChatConfig, ChatRegistry
from agentbridge.storage.sqlite import ChatThreadStore


def _two_chats() -> ChatRegistry:
    first = ChatConfig(-100123456, "Acme Support", "codex", "Acme wiki", Path("chats/acme"))
    second = ChatConfig(-100999000, "Other Chat", "codex", "Other wiki", Path("chats/other"))
    return ChatRegistry({first.telegram_chat_id: first, second.telegram_chat_id: second})


@dataclass
class RecordingProvider:
    calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        message = str(kwargs["message"])
        if "пришлите расчёт завтра" in message:
            return AgentReply(
                thread_id=kwargs.get("thread_id") or "thread-1",
                situation="Клиент ждёт расчёт по трём проектам завтра. Отвечать сейчас не нужно.",
                suggested_reply="",
                should_notify=False,
                action=AgentAction.NO_ACTION,
                candidate_state={"summary": "Расчёт по трём проектам ожидается завтра", "waiting_from_us": ["расчёт завтра"]},
            )
        return AgentReply(
            thread_id=kwargs.get("thread_id") or "thread-1",
            situation=f"Situation after: {message}",
            suggested_reply="Reply",
            candidate_state={"summary": message},
        )


@pytest.mark.asyncio
async def test_live_batch_keeps_chats_isolated(tmp_path) -> None:
    registry = _two_chats()
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = RecordingProvider()
    service = AgentBridgeApplication(registry, store, provider)
    await service.handle_messages(-100123456, [IncomingMessage("Alice", "Acme hello", 1)])
    await service.handle_messages(-100999000, [IncomingMessage("Bob", "Other hello", 2)])

    assert [call["chat_name"] for call in provider.calls] == ["Acme Support", "Other Chat"]
    assert "Other hello" not in provider.calls[0]["message"]
    assert "Acme hello" not in provider.calls[1]["message"]


@pytest.mark.asyncio
async def test_catchup_backlog_is_one_episode_and_skips_stale_reply(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, RecordingProvider(), episode_size=40)
    for update_id, sender, text in (
        (31, "Alice", "Сколько будет стоить?"),
        (32, "Alice", "Хотя подождите, нам нужно на три проекта."),
        (33, "Евгений Расюк", "Да, считаем по трём."),
        (34, "Alice", "Тогда пришлите расчёт завтра."),
    ):
        service.ingest_telegram_message(
            update_id=update_id, chat_id=-100123456, message_id=update_id, sender_id=1,
            sender_name=sender, telegram_date="", text=text, reply_to_message_id=None, is_owner_chat=False,
        )

    restarted = AgentBridgeApplication(chat_registry, ChatThreadStore(tmp_path / "agentbridge.sqlite3"), RecordingProvider())
    results = await restarted.catch_up()

    assert results == []
    assert len(restarted.provider.calls) == 1
    episode = restarted.provider.calls[0]["message"]
    assert "Сколько будет стоить?" in episode
    assert "пришлите расчёт завтра" in episode
    assert "Евгений Расюк" in episode
    state = restarted.store.get_chat_state(-100123456)
    assert "завтра" in state["summary"]


@pytest.mark.asyncio
async def test_successful_episode_state_is_visible_to_the_next_turn(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = RecordingProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    await service.handle_message(-100123456, "Alice", "Need docs", update_id=41)
    await service.handle_message(-100123456, "Alice", "And a timeline", update_id=42)

    assert "Need docs" in provider.calls[1]["context_pack"]
    assert store.get_chat_state(-100123456)["summary"] == "And a timeline"


@pytest.mark.asyncio
async def test_large_backlog_is_processed_in_order_and_notifies_once(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = RecordingProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, episode_size=2)
    for index in range(1, 5):
        service.ingest_telegram_message(
            update_id=50 + index, chat_id=-100123456, message_id=50 + index, sender_id=1,
            sender_name="Alice", telegram_date="", text=f"step {index}",
            reply_to_message_id=None, is_owner_chat=False,
        )

    results = await service.catch_up()

    assert len(provider.calls) == 2
    assert "step 1" in provider.calls[0]["message"]
    assert "step 2" in provider.calls[0]["message"]
    assert "step 3" in provider.calls[1]["message"]
    assert len(results) == 1
    assert "step 3" in results[0].original_message
