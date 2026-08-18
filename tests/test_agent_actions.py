from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentAction, AgentReply
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class ActionProvider:
    reply: AgentReply
    calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return self.reply


async def _handle(tmp_path, chat_registry, reply: AgentReply):
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = ActionProvider(reply)
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    result = await service.handle_message(-100123456, "Alice", "Need a quote", update_id=61)
    return result, store, provider


@pytest.mark.asyncio
async def test_reply_action_creates_an_owner_recommendation(tmp_path, chat_registry) -> None:
    result, store, _ = await _handle(
        tmp_path, chat_registry,
        AgentReply("t", "Клиент просит расчёт", "Пришлём завтра", action=AgentAction.REPLY),
    )
    assert result is not None
    assert result.action == AgentAction.REPLY
    assert result.suggested_reply == "Пришлём завтра"
    assert store.get_recommendation(result.recommendation_id) is not None


@pytest.mark.asyncio
async def test_ask_owner_creates_a_linked_question(tmp_path, chat_registry) -> None:
    result, store, _ = await _handle(
        tmp_path, chat_registry,
        AgentReply(
            "t", "Неясно, действует ли тестовый период", "",
            action=AgentAction.ASK_OWNER,
            owner_question="Тестовый период ещё действует?",
            unknowns="срок теста",
        ),
    )
    assert result is not None
    assert result.action == AgentAction.ASK_OWNER
    store.attach_owner_message(result.recommendation_id, 7654321, 9001)
    question = store.get_owner_question_by_message(9001)
    assert question is not None
    assert question.telegram_chat_id == -100123456
    assert "Тестовый период" in question.question

    service = AgentBridgeApplication(chat_registry, store, ActionProvider(AgentReply("t", "Тест ещё действует", "Ок", action=AgentAction.REPLY)), 7654321)
    answered = await service.handle_owner_question_reply(7654321, 9001, 42, "Owner", "Да, тест ещё действует")
    assert answered is not None
    assert answered.memory_proposal is not None
    assert answered.memory_proposal.scope == "chat"
    assert store.confirm_memory_draft(answered.memory_proposal.draft_id) is not None
    assert "Да, тест ещё действует" in store.active_memory_texts(-100123456, None)


@pytest.mark.asyncio
async def test_observe_notifies_without_a_client_reply(tmp_path, chat_registry) -> None:
    result, _, _ = await _handle(
        tmp_path, chat_registry,
        AgentReply("t", "Клиент сменил объём", "", action=AgentAction.OBSERVE, observation="Вопрос уже изменился."),
    )
    assert result is not None
    assert result.action == AgentAction.OBSERVE
    assert result.suggested_reply == ""


@pytest.mark.asyncio
async def test_no_action_does_not_create_a_recommendation(tmp_path, chat_registry) -> None:
    result, store, provider = await _handle(
        tmp_path, chat_registry,
        AgentReply("t", "Уже закрыто", "", False, action=AgentAction.NO_ACTION),
    )
    assert result is None
    assert store.pending_recommendations(7654321) == []
    assert len(provider.calls) == 1
    assert store.is_update_processed(61) is True
