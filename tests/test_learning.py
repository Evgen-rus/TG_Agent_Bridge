from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply, FeedbackAnalysis
from agentbridge.application import AgentBridgeApplication
from agentbridge.chats.loader import ChatConfig, ChatRegistry
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
    original_chat = chat_registry.all_chats()[0]
    chat = ChatConfig(
        original_chat.telegram_chat_id,
        original_chat.name,
        original_chat.agent_provider,
        original_chat.wiki,
        original_chat.directory,
        knowledge_pack="leadgenbureau",
    )
    service, store, provider = await _prepared_service(
        tmp_path, ChatRegistry({chat.telegram_chat_id: chat}), analysis,
    )
    service.knowledge_dir = tmp_path / "knowledge"
    shared_dir = service.knowledge_dir / "leadgenbureau"
    shared_dir.mkdir(parents=True)
    (shared_dir / "core.md").write_text("Shared LeadGenBureau core", encoding="utf-8")
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Write this warmer")
    assert proposal is not None
    assert store.active_rule_texts(-100123456) == []
    result = await service.confirm_learning(proposal.draft_id)
    assert result is not None and result.rule_saved and result.revised_suggestion is not None
    assert result.revised_suggestion.suggested_reply == "Revised reply"
    assert store.active_rule_texts(-100123456) == ["Use a warm, concise tone."]
    assert provider.revise_calls[0]["rules"] == ["Use a warm, concise tone."]
    context_pack = provider.revise_calls[0]["context_pack"]
    assert "Acme uses the Enterprise plan." in context_pack
    assert "Shared LeadGenBureau core" in context_pack
    assert "Текущее состояние чата:" in context_pack
    assert "Can I get the docs?" in context_pack
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
async def test_owner_feedback_creates_pending_memory_candidate_until_scope_is_confirmed(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis(
        "Сначала проверить обработку текущей базы.", None, None, "client", True,
        "Перепиши текущий ответ.",
        "При большом необработанном остатке сначала рассматривать дожим текущей базы.",
        "global",
    )
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "В дальнейшем сначала проверяем текущую базу", 77)

    assert proposal is not None and proposal.memory_proposal is not None
    memory = proposal.memory_proposal
    assert store.get_memory_draft(memory.draft_id).status == "pending"
    assert store.active_memory_entries(-100123456, None) == []
    confirmed = service.confirm_memory(memory.draft_id, "global")
    assert confirmed is not None and confirmed.scope == "global"
    assert [item.content for item in store.active_memory_entries(-100123456, None)] == [memory.content]


@pytest.mark.asyncio
async def test_client_specific_memory_candidate_cannot_be_promoted_to_global(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis(
        "Сохраняем только для этого клиента.", None, None, "client", False, None,
        "Для Acme сначала использовать текущую базу.", "chat",
    )
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Для Acme сначала текущая база", 78)

    assert proposal is not None and proposal.memory_proposal is not None
    assert proposal.memory_proposal.scope == "chat"
    assert service.confirm_memory(proposal.memory_proposal.draft_id, "global") is None


@pytest.mark.asyncio
async def test_chat_candidate_scope_is_enforced_at_confirmation(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis(
        "Правило только для этого чата.", None, None, "client", False, None,
        "Для этого чата сначала проверить остаток текущей базы.", "chat",
    )
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Для этого чата сначала остаток", 81)
    assert proposal is not None and proposal.memory_proposal is not None
    draft_id = proposal.memory_proposal.draft_id
    assert service.confirm_memory(draft_id, "global") is None
    assert service.confirm_memory(draft_id, "chat") is not None


@pytest.mark.asyncio
async def test_one_off_feedback_needs_no_memory_or_rule_confirmation(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis("Только перепиши текущий ответ.", None, None, "client", True, "Rewrite it.")
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Перепиши только этот ответ", 79)
    assert proposal is not None
    result = await service.confirm_learning(proposal.draft_id)
    assert result is not None
    assert store.active_rule_texts(-100123456) == []
    assert store.active_memory_entries(-100123456, None) == []
    assert store.recent_experience(-100123456)


@pytest.mark.asyncio
async def test_memory_candidate_is_suppressed_when_knowledge_already_covers_it(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis(
        "Дозвон не равен отказу.", None, None, "client", False, None,
        "Недозвон не равен отказу.", "global",
    )
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    service.knowledge_dir = tmp_path / "knowledge"
    current = chat_registry.all_chats()[0]
    service.registry = ChatRegistry({current.telegram_chat_id: ChatConfig(
        current.telegram_chat_id, current.name, current.agent_provider, current.wiki,
        current.directory, knowledge_pack="leadgenbureau",
    )})
    shared_dir = service.knowledge_dir / "leadgenbureau"
    shared_dir.mkdir(parents=True)
    (shared_dir / "core.md").write_text("Недозвон не равен отказу.", encoding="utf-8")
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Это общий принцип", 80)
    assert proposal is not None and proposal.memory_proposal is None
    assert store.active_memory_entries(-100123456, None) == []


@pytest.mark.asyncio
async def test_memory_candidate_is_suppressed_by_active_memory_or_rule(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = LearningProvider(FeedbackAnalysis("Same", None, None, "client", False, None))
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    recommendation = await service.handle_message(-100123456, "Alice", "Need docs")
    assert recommendation is not None
    service.record_owner_delivery(recommendation.recommendation_id, 7654321, 9001)
    existing = store.create_memory_draft(
        recommendation.recommendation_id, 1, "Owner", "Проверять текущую базу", "global", None,
    )
    assert store.confirm_memory_draft(existing.id) is not None
    analysis = FeedbackAnalysis(
        "Повтор", None, None, "client", False, None, "Проверять текущую базу", "global",
    )
    provider.analysis = analysis
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "В дальнейшем проверять базу", 82)
    assert proposal is not None and proposal.memory_proposal is None


@pytest.mark.asyncio
async def test_memory_candidate_checks_active_rule_and_non_core_knowledge(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = LearningProvider(FeedbackAnalysis("Same", None, None, "client", False, None))
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=7654321)
    service.knowledge_dir = tmp_path / "knowledge"
    current = chat_registry.all_chats()[0]
    service.registry = ChatRegistry({current.telegram_chat_id: ChatConfig(
        current.telegram_chat_id, current.name, current.agent_provider, current.wiki,
        current.directory, knowledge_pack="leadgenbureau",
    )})
    shared_dir = service.knowledge_dir / "leadgenbureau"
    shared_dir.mkdir(parents=True)
    (shared_dir / "operations.md").write_text("Сначала проверить текущую базу перед расширением объёма.", encoding="utf-8")
    recommendation = await service.handle_message(-100123456, "Alice", "Need docs")
    assert recommendation is not None
    service.record_owner_delivery(recommendation.recommendation_id, 7654321, 9001)
    rule_draft = store.create_learning_draft(
        recommendation.recommendation_id, 1, "Owner", "seed",
        FeedbackAnalysis("Same", "Проверять текущую базу перед расширением объёма.", "volume", "client", False, None),
    )
    assert store.confirm_draft(rule_draft.id)
    provider.analysis = FeedbackAnalysis(
        "Повтор", None, None, "client", False, None,
        "Проверять текущую базу перед расширением объёма.", "chat",
    )
    proposal = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "В дальнейшем проверять базу", 84)
    assert proposal is not None and proposal.memory_proposal is None


@pytest.mark.asyncio
async def test_duplicate_owner_update_creates_only_one_memory_draft(tmp_path, chat_registry) -> None:
    analysis = FeedbackAnalysis(
        "Проверять остаток.", None, None, "client", False, None,
        "Проверять остаток текущей базы.", "chat",
    )
    service, store, _ = await _prepared_service(tmp_path, chat_registry, analysis)
    first = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Проверь остаток", 83)
    second = await service.handle_owner_feedback(7654321, 9001, 42, "Owner", "Проверь остаток", 83)
    assert first is not None and first.memory_proposal is not None
    assert second is None
    with store._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_drafts").fetchone()[0] == 1


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
