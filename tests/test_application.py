from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agentbridge.agents.base import AgentReply, OwnerQueryAnswer
from agentbridge.application import AgentBridgeApplication, OwnerQueryResult
from agentbridge.chats.loader import ChatConfig, ChatRegistry
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
                **kwargs,
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


@pytest.mark.asyncio
async def test_standalone_global_memory_does_not_need_a_client_chat(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    proposal = await service.handle_owner_context(
        7654321, None, 42, "Owner",
        "Общий контекст: фраза про отдел маркетинга утверждена для робота.",
    )
    assert proposal is not None and proposal.scope == "global"
    assert service.confirm_memory(proposal.draft_id) is not None
    await service.handle_message(-100123456, "Alice", "What is the next step?")
    pack = str(provider.calls[-1]["context_pack"])
    assert "фраза про отдел маркетинга утверждена для робота" in pack


@pytest.mark.asyncio
async def test_current_episode_is_not_repeated_in_recent_history(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    await service.handle_message(-100123456, "Alice", "Need the docs", update_id=11)
    await service.handle_message(-100123456, "Alice", "And a timeline", update_id=12)
    pack = str(provider.calls[-1]["context_pack"])
    history, _, current = pack.partition("Текущий эпизод:")
    assert "Need the docs" in history
    assert "And a timeline" not in history
    assert "And a timeline" in current


@dataclass
class QueryProvider(FakeProvider):
    prompt_version: int = 3
    questions: list[dict[str, object]] = field(default_factory=list)
    owner_threads_created: int = 0

    async def answer_owner_query(
        self, *, question: str, chat_name: str, context_pack: str, thread_id: str | None,
    ) -> OwnerQueryAnswer:
        self.questions.append({
            "question": question,
            "chat_name": chat_name,
            "context_pack": context_pack,
            "thread_id": thread_id,
        })
        if thread_id is None:
            self.owner_threads_created += 1
            thread_id = f"owner-thread-{self.owner_threads_created}"
        return OwnerQueryAnswer(thread_id, f"Для {chat_name}: методика дозвона.")


@pytest.mark.asyncio
async def test_owner_query_clarifies_chat_then_uses_original_question(tmp_path) -> None:
    optobel = ChatConfig(
        -1001, "[LR225] ОптоБель", "codex", "Wiki Optobel", Path("chats/lr225_optobel"),
    )
    other = ChatConfig(
        -1002, "[LR220] Риолюкс ЕКБ", "codex", "Wiki Rio", Path("chats/lr220"),
    )
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = QueryProvider()
    service = AgentBridgeApplication(
        ChatRegistry({optobel.telegram_chat_id: optobel, other.telegram_chat_id: other}),
        store,
        provider,
        owner_chat_id=7654321,
        knowledge_dir=tmp_path / "knowledge",
    )
    first = await service.handle_owner_query(
        "@spare_eyes_bot подскажи для клиента сообщение как дозваниваться",
        update_id=501,
    )
    assert isinstance(first, OwnerQueryResult)
    assert first.prompt_id is not None
    names = "[LR225] ОптоБель\n\n[LR220] Риолюкс ЕКБ"
    assert first.text == f"Уточните, о каком чате речь. Сейчас подключены:\n\n{names}"
    service.attach_owner_query_prompt(first.prompt_id, 9001)
    retry = await service.continue_owner_query(9001, "Несуществующий чат", update_id=500)
    assert isinstance(retry, OwnerQueryResult)
    assert retry.text == f"Не нашёл такой чат. Напишите имя ещё раз. Сейчас подключены:\n\n{names}"
    assert retry.prompt_id == first.prompt_id
    assert provider.questions == []
    second = await service.continue_owner_query(9001, "[LR225] ОптоБель", update_id=502)
    assert isinstance(second, OwnerQueryResult)
    assert second.text == "Для [LR225] ОптоБель: методика дозвона."
    assert second.prompt_id is not None
    assert provider.questions[0]["question"] == "@spare_eyes_bot подскажи для клиента сообщение как дозваниваться"
    assert provider.questions[0]["chat_name"] == "[LR225] ОптоБель"
    assert provider.questions[0]["thread_id"] is None
    service.attach_owner_query_prompt(second.prompt_id, 9002)
    third = await service.continue_owner_query(
        9002, "а для тг можешь сделать чтобы красиво читалось", update_id=503,
    )
    assert isinstance(third, OwnerQueryResult)
    assert provider.questions[-1]["question"] == "а для тг можешь сделать чтобы красиво читалось"
    assert provider.questions[-1]["chat_name"] == "[LR225] ОптоБель"
    assert provider.questions[-1]["thread_id"] == "owner-thread-1"
    assert store.get_owner_query_thread_id(-1001) == "owner-thread-1"


@pytest.mark.asyncio
async def test_owner_query_threads_are_per_chat_and_isolated_from_client_thread(tmp_path) -> None:
    chat_a = ChatConfig(-1001, "Client A", "codex", "Wiki A", Path("chats/a"))
    chat_b = ChatConfig(-1002, "Client B", "codex", "Wiki B", Path("chats/b"))
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = QueryProvider()
    service = AgentBridgeApplication(ChatRegistry({-1001: chat_a, -1002: chat_b}), store, provider)

    await service.handle_message(-1001, "Alice", "Client message")
    first_a = await service.handle_owner_query("Что у Client A?")
    assert isinstance(first_a, OwnerQueryResult) and first_a.prompt_id is not None
    service.attach_owner_query_prompt(first_a.prompt_id, 9101)
    await service.continue_owner_query(9101, "А почему?")
    await service.handle_owner_query("Что у Client B?")

    assert [call["thread_id"] for call in provider.questions] == [None, "owner-thread-1", None]
    assert store.get_thread_id(-1001) == "thread-created-on-first-message"
    assert store.get_owner_query_thread_id(-1001) == "owner-thread-1"
    assert store.get_owner_query_thread_id(-1002) == "owner-thread-2"
    assert store.get_thread_id(-1001) != store.get_owner_query_thread_id(-1001)


@pytest.mark.asyncio
async def test_stale_owner_query_prompt_version_starts_new_thread(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.save_owner_query_thread(
        -100123456, "Acme Support", "owner-thread-old", prompt_version=2,
    )
    provider = QueryProvider(prompt_version=3)
    service = AgentBridgeApplication(chat_registry, store, provider)

    await service.handle_owner_query("Что происходит в Acme Support?")

    assert provider.questions[0]["thread_id"] is None
    assert store.get_owner_query_thread_id(-100123456) == "owner-thread-1"
    assert store.get_owner_query_thread_prompt_version(-100123456) == 3
