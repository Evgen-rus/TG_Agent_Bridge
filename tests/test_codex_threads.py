from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest

from agentbridge.agents.base import AgentAction, AgentReply
from agentbridge.agents.codex import (
    AGENT_PROMPT_VERSION,
    CodexProvider,
    _CANDIDATE_STATE_PROPERTIES,
    _CRITIQUE_INSTRUCTIONS,
    _FEEDBACK_SCHEMA,
    _INSTRUCTIONS,
    _ONBOARDING_SCHEMA,
    _OWNER_QUERY_INSTRUCTIONS,
    _OWNER_QUERY_SCHEMA,
    _SEPIA_INSTRUCTIONS,
    _SEPIA_SCHEMA,
    _SUGGEST_SCHEMA,
    validate_structured_output_schema,
)
from agentbridge.application import AgentBridgeApplication
from agentbridge.storage.sqlite import ChatThreadStore


@dataclass
class VersionedProvider:
    prompt_version: int = AGENT_PROMPT_VERSION
    calls: list[dict] = field(default_factory=list)
    created: int = 0

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        if kwargs.get("thread_id"):
            return AgentReply(kwargs["thread_id"], f"Situation after: {kwargs['message']}", "Resume reply")
        self.created += 1
        return AgentReply(f"thread-new-{self.created}", f"Situation after: {kwargs['message']}", "New reply")


@pytest.mark.asyncio
async def test_new_chat_saves_current_prompt_version(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = VersionedProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    await service.handle_message(-100123456, "Alice", "Can I get the docs?")
    assert provider.calls[0]["thread_id"] is None
    assert store.get_thread_id(-100123456) == "thread-new-1"
    assert store.get_thread_prompt_version(-100123456) == AGENT_PROMPT_VERSION


@pytest.mark.asyncio
async def test_matching_prompt_version_resumes_the_same_thread(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = VersionedProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    await service.handle_message(-100123456, "Alice", "Can I get the docs?")
    await service.handle_message(-100123456, "Alice", "When will it be ready?")
    assert [call["thread_id"] for call in provider.calls] == [None, "thread-new-1"]
    assert store.get_thread_id(-100123456) == "thread-new-1"
    assert provider.created == 1


@pytest.mark.asyncio
async def test_stale_or_null_prompt_version_starts_a_new_thread(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    store.save_thread(-100123456, "Acme Support", "thread-legacy")
    provider = VersionedProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    await service.handle_message(-100123456, "Alice", "Need a timeline")
    assert provider.calls[0]["thread_id"] is None
    assert "Need a timeline" in str(provider.calls[0]["context_pack"])
    assert store.get_thread_id(-100123456) == "thread-new-1"
    assert store.get_thread_prompt_version(-100123456) == AGENT_PROMPT_VERSION


@pytest.mark.asyncio
async def test_restart_with_same_prompt_version_keeps_the_thread(tmp_path, chat_registry) -> None:
    database_path = tmp_path / "agentbridge.sqlite3"
    first_store = ChatThreadStore(database_path)
    first = AgentBridgeApplication(chat_registry, first_store, VersionedProvider())
    await first.handle_message(-100123456, "Alice", "Can I get the docs?")
    restarted_store = ChatThreadStore(database_path)
    provider = VersionedProvider()
    restarted = AgentBridgeApplication(chat_registry, restarted_store, provider)
    await restarted.handle_message(-100123456, "Alice", "And a quote")
    assert provider.calls[0]["thread_id"] == "thread-new-1"
    assert restarted_store.get_thread_id(-100123456) == "thread-new-1"
    assert restarted_store.get_thread_prompt_version(-100123456) == AGENT_PROMPT_VERSION


@dataclass
class CritiqueProvider:
    prompt_version: int = AGENT_PROMPT_VERSION
    suggest_calls: list[dict] = field(default_factory=list)
    critique_calls: list[dict] = field(default_factory=list)
    needs_critique: bool = True
    confidence: float | None = 0.2

    async def suggest(self, **kwargs) -> AgentReply:
        self.suggest_calls.append(kwargs)
        return AgentReply(
            "thread-main",
            "Maybe reply",
            "First draft",
            action=AgentAction.REPLY,
            needs_critique=self.needs_critique,
            confidence=self.confidence,
        )

    async def critique(self, **kwargs) -> AgentReply:
        self.critique_calls.append(kwargs)
        return AgentReply(
            "thread-critique",
            "Safer observe",
            "",
            action=AgentAction.OBSERVE,
            observation="The later message already closed this.",
        )


@pytest.mark.asyncio
async def test_critique_does_not_replace_the_persistent_thread(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = CritiqueProvider()
    service = AgentBridgeApplication(chat_registry, store, provider)
    result = await service.handle_message(-100123456, "Alice", "Need a quote")
    assert result is not None
    assert result.action == AgentAction.OBSERVE
    assert result.observation == "The later message already closed this."
    assert len(provider.critique_calls) == 1
    assert provider.critique_calls[0]["previous"].thread_id == "thread-main"
    assert store.get_thread_id(-100123456) == "thread-main"


@pytest.mark.asyncio
async def test_high_confidence_turn_skips_critique(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = CritiqueProvider(needs_critique=False, confidence=0.9)
    service = AgentBridgeApplication(chat_registry, store, provider)
    result = await service.handle_message(-100123456, "Alice", "Need a quote")
    assert result is not None
    assert result.suggested_reply == "First draft"
    assert provider.critique_calls == []
    assert store.get_thread_id(-100123456) == "thread-main"


def _suggest_payload(**overrides) -> dict:
    payload = {
        "action": "reply",
        "situation": "Alice needs docs",
        "suggested_reply": "I will send the link.",
        "observation": "",
        "unknowns": "",
        "owner_question": "",
        "should_notify": True,
        "confidence": 0.9,
        "needs_critique": False,
        "candidate_state": None,
    }
    payload.update(overrides)
    return payload


class _FakeResult:
    def __init__(self, payload: dict):
        self.error = None
        self.final_response = json.dumps(payload)


class _FakeThread:
    def __init__(self, thread_id: str, payload: dict):
        self.id = thread_id
        self.payload = payload
        self.prompts: list[str] = []

    def run(self, prompt, **kwargs) -> _FakeResult:
        self.prompts.append(prompt)
        return _FakeResult(self.payload)


class _FakeCodex:
    starts: list[dict] = []
    resumes: list[str] = []
    threads: list[_FakeThread] = []
    suggest_payload: dict = _suggest_payload()
    critique_payload: dict = _suggest_payload(action="observe", suggested_reply="", observation="Closed.")
    owner_payload: dict = {"answer": "Owner answer"}
    sepia_payload: dict = {
        "refactored_reply": "Пришлю ссылку завтра в 10:00.",
        "facts_preserved": True,
        "commitments_preserved": True,
    }

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def thread_start(self, **kwargs) -> _FakeThread:
        self.starts.append(kwargs)
        if kwargs.get("developer_instructions") == _CRITIQUE_INSTRUCTIONS:
            thread = _FakeThread("thread-critique", self.critique_payload)
        elif kwargs.get("developer_instructions") == _OWNER_QUERY_INSTRUCTIONS:
            thread = _FakeThread("thread-owner", self.owner_payload)
        elif kwargs.get("developer_instructions") == _SEPIA_INSTRUCTIONS:
            thread = _FakeThread("thread-sepia", self.sepia_payload)
        else:
            thread = _FakeThread("thread-started", self.suggest_payload)
        self.threads.append(thread)
        return thread

    def thread_resume(self, thread_id: str, **kwargs) -> _FakeThread:
        self.resumes.append(thread_id)
        payload = self.owner_payload if thread_id == "thread-owner" else self.suggest_payload
        thread = _FakeThread(thread_id, payload)
        self.threads.append(thread)
        return thread


@pytest.fixture
def fake_codex(monkeypatch):
    _FakeCodex.starts = []
    _FakeCodex.resumes = []
    _FakeCodex.threads = []
    _FakeCodex.sepia_payload = {
        "refactored_reply": "Пришлю ссылку завтра в 10:00.",
        "facts_preserved": True,
        "commitments_preserved": True,
    }
    monkeypatch.setattr("agentbridge.agents.codex.Codex", _FakeCodex)
    return _FakeCodex


@pytest.mark.asyncio
async def test_codex_owner_query_starts_then_resumes_its_thread(fake_codex) -> None:
    provider = CodexProvider()
    first = await provider.answer_owner_query(
        question="What now?", chat_name="Acme", context_pack="pack one", thread_id=None,
    )
    second = await provider.answer_owner_query(
        question="Why?", chat_name="Acme", context_pack="pack two", thread_id=first.thread_id,
    )

    assert first.thread_id == "thread-owner"
    assert second.thread_id == "thread-owner"
    assert second.answer == "Owner answer"
    assert fake_codex.resumes == ["thread-owner"]
    assert fake_codex.starts[0]["developer_instructions"] == _OWNER_QUERY_INSTRUCTIONS


@pytest.mark.asyncio
async def test_codex_owner_query_restarts_when_saved_thread_is_unavailable(fake_codex, monkeypatch) -> None:
    def unavailable(self, thread_id: str, **kwargs):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(_FakeCodex, "thread_resume", unavailable)
    provider = CodexProvider()

    result = await provider.answer_owner_query(
        question="What now?", chat_name="Acme", context_pack="fresh pack", thread_id="missing-thread",
    )

    assert result.thread_id == "thread-owner"
    assert result.answer == "Owner answer"
    assert fake_codex.starts[-1]["developer_instructions"] == _OWNER_QUERY_INSTRUCTIONS


@pytest.mark.asyncio
async def test_codex_suggest_resumes_only_the_main_thread(fake_codex) -> None:
    provider = CodexProvider()
    first = await provider.suggest(
        message="Need docs", sender_name="Alice", chat_name="Acme", wiki="wiki", rules=[], thread_id=None,
    )
    second = await provider.suggest(
        message="When?", sender_name="Alice", chat_name="Acme", wiki="wiki", rules=[], thread_id=first.thread_id,
        context_pack="pack",
    )
    assert first.thread_id == "thread-started"
    assert second.thread_id == "thread-started"
    assert fake_codex.resumes == ["thread-started"]
    assert fake_codex.starts[0]["developer_instructions"] == _INSTRUCTIONS


@pytest.mark.asyncio
async def test_codex_critique_is_ephemeral_and_keeps_main_thread_id(fake_codex) -> None:
    provider = CodexProvider()
    previous = AgentReply("thread-main", "Maybe reply", "Draft", action=AgentAction.REPLY, needs_critique=True)
    result = await provider.critique(
        previous=previous,
        message="Need a quote, actually never mind",
        sender_name="Alice",
        chat_name="Acme",
        wiki="wiki",
        rules=[],
        thread_id="thread-main",
        context_pack="Текущий эпизод:\nNeed a quote, actually never mind",
    )
    assert result.thread_id == "thread-main"
    assert result.action == AgentAction.OBSERVE
    assert fake_codex.resumes == []
    assert fake_codex.starts[-1]["developer_instructions"] == _CRITIQUE_INSTRUCTIONS
    assert "thread-critique" not in fake_codex.resumes


@pytest.mark.asyncio
async def test_sepia_refactors_only_compact_draft_context(fake_codex) -> None:
    provider = CodexProvider(sepia_enabled=True)
    draft = AgentReply(
        "thread-main",
        "Нужно подтвердить срок отправки ссылки",
        "На данный момент я пришлю ссылку завтра в 10:00.",
        action=AgentAction.REPLY,
        candidate_state={"commitments": ["Отправить ссылку завтра в 10:00"]},
    )

    result = await provider.refactor_reply(draft)

    assert result.suggested_reply == "Пришлю ссылку завтра в 10:00."
    sepia_thread = fake_codex.threads[-1]
    assert fake_codex.starts[-1]["developer_instructions"] == _SEPIA_INSTRUCTIONS
    assert fake_codex.starts[-1]["config"]["model_reasoning_effort"] == "low"
    assert "Draft ответа:" in sepia_thread.prompts[0]
    assert "Недавняя история" not in sepia_thread.prompts[0]


@pytest.mark.asyncio
async def test_sepia_falls_back_to_rick_draft_when_a_number_changes(fake_codex) -> None:
    fake_codex.sepia_payload = {
        "refactored_reply": "Пришлю ссылку завтра в 11:00.",
        "facts_preserved": True,
        "commitments_preserved": True,
    }
    provider = CodexProvider(sepia_enabled=True)
    draft = AgentReply(
        "thread-main", "Нужно подтвердить срок", "Пришлю ссылку завтра в 10:00.", action=AgentAction.REPLY,
    )

    assert (await provider.refactor_reply(draft)).suggested_reply == draft.suggested_reply


@pytest.mark.asyncio
async def test_codex_suggest_attaches_images_and_pdfs_to_the_chat_thread(fake_codex, tmp_path) -> None:
    from openai_codex import LocalImageInput, MentionInput, TextInput

    from agentbridge.agents.base import MediaAttachment

    image = tmp_path / "shot.jpg"
    pdf = tmp_path / "scan.pdf"
    image.write_bytes(b"jpeg")
    pdf.write_bytes(b"%PDF")
    provider = CodexProvider()
    first = await provider.suggest(
        message="[фото]", sender_name="Alice", chat_name="Acme", wiki="wiki", rules=[], thread_id=None,
        attachments=(MediaAttachment(str(image), "photo", "image/jpeg", "shot.jpg"),),
    )
    await provider.suggest(
        message="[файл: scan.pdf]", sender_name="Alice", chat_name="Acme", wiki="wiki", rules=[],
        thread_id=first.thread_id,
        attachments=(MediaAttachment(str(pdf), "document", "application/pdf", "scan.pdf"),),
    )
    first_input = fake_codex.threads[0].prompts[0]
    second_input = fake_codex.threads[1].prompts[0]
    assert isinstance(first_input[0], TextInput)
    assert any(isinstance(item, LocalImageInput) and item.path == str(image.resolve()) for item in first_input)
    assert any(isinstance(item, MentionInput) and item.path == str(pdf.resolve()) for item in second_input)
    assert fake_codex.resumes == ["thread-started"]


def test_codex_output_schemas_match_structured_outputs_subset() -> None:
    for schema in (_SUGGEST_SCHEMA, _FEEDBACK_SCHEMA, _OWNER_QUERY_SCHEMA, _ONBOARDING_SCHEMA, _SEPIA_SCHEMA):
        validate_structured_output_schema(schema)
    field = _SUGGEST_SCHEMA["properties"]["candidate_state"]
    assert field["additionalProperties"] is False
    assert set(field["required"]) == set(_CANDIDATE_STATE_PROPERTIES)
    assert field["properties"]["summary"]["type"] == ["string", "null"]
    assert field["properties"]["facts"]["type"] == ["array", "null"]
    assert set(_SUGGEST_SCHEMA["required"]) == set(_SUGGEST_SCHEMA["properties"])


def test_structured_output_schema_rejects_the_errors_we_already_hit() -> None:
    with pytest.raises(ValueError, match="type=\\['object', 'null'\\]"):
        validate_structured_output_schema(
            {
                "type": "object",
                "properties": {"candidate_state": {"type": ["object", "null"]}},
                "required": ["candidate_state"],
                "additionalProperties": False,
            }
        )
    with pytest.raises(ValueError, match="Missing 'candidate_state'|missing="):
        validate_structured_output_schema(
            {
                "type": "object",
                "properties": {"action": {"type": "string"}, "candidate_state": {"type": "null"}},
                "required": ["action"],
                "additionalProperties": False,
            }
        )
