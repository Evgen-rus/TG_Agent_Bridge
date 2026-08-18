from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class FakeProvider:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def suggest(
        self,
        *,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        rules: list[str],
        thread_id: str | None,
        **kwargs,
    ) -> AgentReply:
        self.calls.append(
            {
                "message": message,
                "sender_name": sender_name,
                "chat_name": chat_name,
                "wiki": wiki,
                "rules": rules,
                "thread_id": thread_id,
            }
        )
        return AgentReply(
            thread_id=thread_id or "thread-created-on-first-message",
            situation=f"Situation after: {message}",
            suggested_reply="A concise proposed reply.",
        )


@pytest.mark.asyncio
async def test_first_message_creates_thread_and_next_message_resumes_it(
    tmp_path, chat_registry
) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)

    first = await service.handle_message(-100123456, "Alice", "Can I get the docs?")
    second = await service.handle_message(-100123456, "Alice", "When will it be ready?")

    assert first is not None
    assert first.chat_name == "Acme Support"
    assert second is not None
    assert [call["thread_id"] for call in provider.calls] == [
        None,
        "thread-created-on-first-message",
    ]
    assert provider.calls[0]["wiki"] == "Acme uses the Enterprise plan."
    assert store.get_thread_id(-100123456) == "thread-created-on-first-message"


@pytest.mark.asyncio
async def test_unknown_chat_is_ignored_without_calling_the_provider(
    tmp_path, chat_registry
) -> None:
    provider = FakeProvider()
    service = AgentBridgeApplication(
        chat_registry, ChatThreadStore(tmp_path / "agentbridge.sqlite3"), provider
    )

    result = await service.handle_message(-100404, "Alice", "Hello")

    assert result is None
    assert provider.calls == []


@pytest.mark.asyncio
async def test_duplicate_update_is_ignored_across_restart(tmp_path, chat_registry) -> None:
    database_path = tmp_path / "agentbridge.sqlite3"
    provider = FakeProvider()
    first_service = AgentBridgeApplication(
        chat_registry, ChatThreadStore(database_path), provider
    )
    await first_service.handle_message(
        -100123456, "Alice", "Hello", update_id=789
    )

    restarted_service = AgentBridgeApplication(
        chat_registry, ChatThreadStore(database_path), provider
    )
    duplicate = await restarted_service.handle_message(
        -100123456, "Alice", "Hello", update_id=789
    )

    assert duplicate is None
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_internal_participant_is_saved_without_a_recommendation(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)

    result = await service.handle_message(
        -100123456, "Евгений Расюк", "Клиент подтвердил самостоятельный прозвон.", update_id=91,
    )

    assert result is None
    assert provider.calls == []
    assert store.recent_internal_context(-100123456) == [
        "Евгений Расюк: Клиент подтвердил самостоятельный прозвон."
    ]
    assert store.is_update_processed(91)


@pytest.mark.asyncio
async def test_confirmed_chat_memory_and_internal_context_are_sent_only_to_that_chat(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    suggestion = await service.handle_message(-100123456, "Alice", "Can I get the docs?")
    assert suggestion is not None
    service.record_owner_delivery(suggestion.recommendation_id, 7654321, 9001)
    proposal = await service.handle_owner_context(
        7654321, 9001, 42, "Owner", "Контекст: Клиенту нужен договор до запуска проекта."
    )
    assert proposal is not None and proposal.scope == "chat"
    assert service.confirm_memory(proposal.draft_id) is not None
    await service.handle_message(-100123456, "Евгений Расюк", "Юрист уже получил реквизиты.")
    await service.handle_message(-100123456, "Alice", "What is the next step?")

    wiki = str(provider.calls[-1]["wiki"])
    assert "Клиенту нужен договор до запуска проекта." in wiki
    assert "Юрист уже получил реквизиты." in wiki
