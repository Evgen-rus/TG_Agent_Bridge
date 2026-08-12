from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply, FeedbackAnalysis
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class LearningProvider:
    analysis: FeedbackAnalysis
    suggest_calls: list[dict] = field(default_factory=list)
    revise_calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs) -> AgentReply:
        self.suggest_calls.append(kwargs)
        return AgentReply(kwargs.get("thread_id") or "thread-1", "Situation", "First reply")

    async def analyze_feedback(self, **kwargs) -> FeedbackAnalysis:
        return self.analysis

    async def revise(self, **kwargs) -> AgentReply:
        self.revise_calls.append(kwargs)
        return AgentReply(kwargs["thread_id"], "Revised situation", "Revised reply")


async def _prepared_service(tmp_path, chat_registry, analysis):
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = LearningProvider(analysis)
    service = AgentBridgeApplication(chat_registry, store, provider)
    suggestion = await service.handle_message(-100123456, "Alice", "Can I get the docs?")
    assert suggestion is not None
    service.record_owner_delivery(suggestion.recommendation_id, 7654321, 9001)
    return service, store, provider


@pytest.mark.asyncio
async def test_confirmed_rule_is_persisted_and_current_reply_is_revised(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis("Use a warmer tone.", "Use a warm, concise tone.", "reply_tone", "client", True, "Rewrite this reply in a warmer tone.")
    service, store, provider = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Write this warmer")
    assert proposal is not None
    assert store.active_rule_texts(-100123456) == []
    result = await service.confirm_learning(proposal.draft_id)
    assert result is not None and result.rule_saved and result.revised_suggestion is not None
    assert result.revised_suggestion.suggested_reply == "Revised reply"
    assert store.active_rule_texts(-100123456) == ["Use a warm, concise tone."]
    assert provider.revise_calls[0]["rules"] == ["Use a warm, concise tone."]
    assert await service.confirm_learning(proposal.draft_id) is None


@pytest.mark.asyncio
async def test_global_scope_requires_explicit_owner_wording(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis("Keep replies short.", "Keep replies short.", "reply_length", "global", False, None)
    service, _, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    local = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Пиши короче")
    explicit = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Для всех клиентов пиши короче")
    assert local is not None and local.scope == "client"
    assert explicit is not None and explicit.scope == "global"


@pytest.mark.asyncio
async def test_confirmed_rule_can_suppress_future_owner_notification(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis("Ignore this situation", "Ignore greetings", "notify_greeting", "client", False, None)
    service, store, provider = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Don't notify for greetings")
    assert proposal is not None
    await service.confirm_learning(proposal.draft_id)

    async def suppressed(**kwargs):
        provider.suggest_calls.append(kwargs)
        return AgentReply(kwargs.get("thread_id") or "thread-1", "Ignored by rule", "", False)

    provider.suggest = suppressed
    result = await service.handle_message(-100123456, "Alice", "Hello", update_id=777)
    assert result is None
    assert store.is_update_processed(777)
    assert provider.suggest_calls[-1]["rules"] == ["Ignore greetings"]


@pytest.mark.asyncio
async def test_clarification_replaces_pending_interpretation(tmp_path, chat_registry) -> None:
    first = FeedbackAnalysis("First understanding", None, None, "client", False, None)
    service, store, provider = await _prepared_service(tmp_path, chat_registry, first)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Not like that")
    assert proposal is not None
    service.mark_awaiting_clarification(proposal.draft_id, 9100)
    provider.analysis = FeedbackAnalysis("Clarified understanding", "New rule", "topic", "client", False, None)
    clarified = await service.clarify_feedback(9100, "I meant this")
    assert clarified is not None and clarified.draft_id == proposal.draft_id
    assert clarified.understanding == "Clarified understanding"
    assert store.active_rule_texts(-100123456) == []


def test_new_rule_with_same_conflict_key_supersedes_old_rule(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    recommendation_id = store.create_recommendation(-1001, "Client", "Alice", "Hello", "Greeting", "Hi")
    first = FeedbackAnalysis("First", "Be formal", "tone", "client", False, None)
    second = FeedbackAnalysis("Second", "Be informal", "tone", "client", False, None)
    store.confirm_draft(store.create_learning_draft(recommendation_id, 1, "Owner A", "formal", first).id)
    store.confirm_draft(store.create_learning_draft(recommendation_id, 2, "Owner B", "informal", second).id)
    restarted = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    assert restarted.active_rule_texts(-1001) == ["Be informal"]
    assert restarted.list_active_rules()[0].author_name == "Owner B"
    undone = restarted.undo_latest_rule()
    assert undone is not None and undone.rule_text == "Be informal"
    assert restarted.active_rule_texts(-1001) == ["Be formal"]
