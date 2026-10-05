from __future__ import annotations

from dataclasses import dataclass, field
import json

import pytest
from openai_codex.errors import InvalidRequestError, TransportClosedError

from agentbridge.agents.base import AgentAction, AgentReply
from agentbridge.agents.codex import (
    AGENT_PROMPT_VERSION,
    CodexProvider,
    CodexTransportClosed,
    _CANDIDATE_STATE_PROPERTIES,
    _CRITIQUE_INSTRUCTIONS,
    _FEEDBACK_SCHEMA,
    _GENERAL_TASK_PLAN_SCHEMA,
    _INSTRUCTIONS,
    _ONBOARDING_SCHEMA,
    _OWNER_QUERY_INSTRUCTIONS,
    _OWNER_QUERY_SCHEMA,
    _OWNER_CONTEXT_TRUST_BOUNDARY,
    _OWNER_MEMORY_INSTRUCTIONS,
    _OWNER_MEMORY_SCHEMA,
    _SEPIA_INSTRUCTIONS,
    _SEPIA_SCHEMA,
    _SUGGEST_SCHEMA,
    validate_structured_output_schema,
)
from agentbridge.application import AgentBridgeApplication
from agentbridge.owner_memory import EMPTY_WORKING_CONTEXT
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


@dataclass
class PersistentSepiaProvider(VersionedProvider):
    sepia_calls: list[str | None] = field(default_factory=list)

    async def refactor_reply(
        self, reply: AgentReply, *, thread_id: str | None,
    ) -> tuple[AgentReply, str]:
        self.sepia_calls.append(thread_id)
        return reply, thread_id or "sepia-new-1"


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


@pytest.mark.asyncio
async def test_restart_with_same_prompt_version_keeps_the_sepia_thread(tmp_path, chat_registry) -> None:
    database_path = tmp_path / "agentbridge.sqlite3"
    first_provider = PersistentSepiaProvider()
    first = AgentBridgeApplication(chat_registry, ChatThreadStore(database_path), first_provider)
    await first.handle_message(-100123456, "Alice", "Can I get the docs?")

    restarted_provider = PersistentSepiaProvider()
    restarted_store = ChatThreadStore(database_path)
    restarted = AgentBridgeApplication(chat_registry, restarted_store, restarted_provider)
    await restarted.handle_message(-100123456, "Alice", "And a quote")

    assert first_provider.sepia_calls == [None]
    assert restarted_provider.sepia_calls == ["sepia-new-1"]
    assert restarted_store.get_sepia_thread_id(-100123456) == "sepia-new-1"


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
    resume_kwargs: list[dict] = []
    threads: list[_FakeThread] = []
    suggest_payload: dict = _suggest_payload()
    critique_payload: dict = _suggest_payload(action="observe", suggested_reply="", observation="Closed.")
    owner_payload: dict = {"answer": "Owner answer"}
    memory_payload: dict = {"changed": False, "content": EMPTY_WORKING_CONTEXT}
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
        elif str(kwargs.get("developer_instructions") or "").startswith(_OWNER_MEMORY_INSTRUCTIONS):
            thread = _FakeThread("thread-memory", self.memory_payload)
        elif str(kwargs.get("developer_instructions") or "").startswith(_OWNER_QUERY_INSTRUCTIONS):
            thread = _FakeThread("thread-owner", self.owner_payload)
        elif kwargs.get("developer_instructions") == _SEPIA_INSTRUCTIONS:
            thread = _FakeThread("thread-sepia", self.sepia_payload)
        else:
            thread = _FakeThread("thread-started", self.suggest_payload)
        self.threads.append(thread)
        return thread

    def thread_resume(self, thread_id: str, **kwargs) -> _FakeThread:
        self.resumes.append(thread_id)
        self.resume_kwargs.append(kwargs)
        if thread_id == "thread-owner":
            payload = self.owner_payload
        elif thread_id == "thread-memory":
            payload = self.memory_payload
        elif thread_id == "thread-sepia":
            payload = self.sepia_payload
        else:
            payload = self.suggest_payload
        thread = _FakeThread(thread_id, payload)
        self.threads.append(thread)
        return thread


@pytest.fixture
def fake_codex(monkeypatch):
    _FakeCodex.starts = []
    _FakeCodex.resumes = []
    _FakeCodex.resume_kwargs = []
    _FakeCodex.threads = []
    _FakeCodex.memory_payload = {"changed": False, "content": EMPTY_WORKING_CONTEXT}
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
async def test_closed_transport_is_retried_once_on_a_new_codex(fake_codex, monkeypatch) -> None:
    """Оборванный транспорт повторяется ровно один раз и даёт ответ."""
    opened: list[int] = []

    def flaky_run(self, prompt, **kwargs):
        opened.append(1)
        if len(opened) == 1:
            raise TransportClosedError("transport closed")
        return _FakeResult({"answer": "Owner answer"})

    monkeypatch.setattr(_FakeThread, "run", flaky_run)
    provider = CodexProvider()

    result = await provider.answer_owner_query(
        question="What now?", chat_name="Acme", context_pack="pack", thread_id="thread-owner",
    )

    assert result.answer == "Owner answer"
    assert len(opened) == 2


@pytest.mark.asyncio
async def test_second_closed_transport_fails_without_a_third_attempt(fake_codex, monkeypatch) -> None:
    """Второй обрыв — отказ без третьей попытки, но тип `CodexTransportClosed`
    сохраняется: иначе метка причины для владельца стала бы общей."""
    attempts = 0

    def always_closed(self, prompt, **kwargs):
        nonlocal attempts
        attempts += 1
        raise TransportClosedError("transport closed")

    monkeypatch.setattr(_FakeThread, "run", always_closed)
    provider = CodexProvider()

    with pytest.raises(CodexTransportClosed):
        await provider.answer_owner_query(
            question="What now?", chat_name="Acme", context_pack="pack", thread_id="thread-owner",
        )

    assert attempts == 2


@pytest.mark.asyncio
async def test_other_errors_are_never_retried(fake_codex, monkeypatch) -> None:
    """Повторяется только оборванный транспорт, а не любая ошибка подряд."""
    attempts = 0

    def failing(self, prompt, **kwargs):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("some other failure")

    monkeypatch.setattr(_FakeThread, "run", failing)
    provider = CodexProvider()

    with pytest.raises(RuntimeError, match="Codex turn failed"):
        await provider.answer_owner_query(
            question="What now?", chat_name="Acme", context_pack="pack", thread_id="thread-owner",
        )

    assert attempts == 1


@pytest.mark.asyncio
async def test_general_task_transport_is_retried_once(fake_codex, monkeypatch) -> None:
    """Тот же единственный повтор действует для general turn."""
    attempts = 0

    def flaky_run(self, prompt, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportClosedError("transport closed")
        return _FakeResult({"answer": "Готово"})

    monkeypatch.setattr(_FakeThread, "run", flaky_run)
    provider = CodexProvider()

    result = await provider.run_general_task(request="Сделай отчёт", thread_id="thread-owner")

    assert result.answer == "Готово"
    assert attempts == 2


@pytest.mark.asyncio
async def test_startup_probe_makes_a_real_ephemeral_codex_turn(fake_codex, monkeypatch) -> None:
    from openai_codex import ApprovalMode, Sandbox

    calls = []

    def probe_run(self, prompt, **kwargs):
        calls.append(kwargs)
        return _FakeResult({"answer": "Codex отвечает"})

    monkeypatch.setattr(_FakeThread, "run", probe_run)
    await CodexProvider().probe()
    assert fake_codex.starts[-1]["ephemeral"] is True
    assert fake_codex.starts[-1]["approval_mode"] == ApprovalMode.deny_all
    assert calls[-1]["sandbox"] == Sandbox.read_only
    assert calls[-1]["effort"] == "low"


@pytest.mark.asyncio
async def test_developer_turn_is_workspace_write_and_checks_are_fixed(fake_codex, monkeypatch) -> None:
    from openai_codex import ApprovalMode, Sandbox
    from types import SimpleNamespace

    calls = []
    checks = []

    def developer_run(self, prompt, **kwargs):
        calls.append(kwargs)
        return _FakeResult({"answer": "Код изменён"})

    monkeypatch.setattr(_FakeThread, "run", developer_run)
    monkeypatch.setattr("agentbridge.agents.codex.subprocess.run",
        lambda command, **kwargs: checks.append(command) or SimpleNamespace(returncode=0, stdout=" M agentbridge/main.py\n"))
    result = await CodexProvider().run_code_change(request="Поправь код", thread_id="developer-thread")
    assert result.checks_passed and len(checks) == 5
    assert fake_codex.resume_kwargs[-1]["sandbox"] == Sandbox.workspace_write
    assert fake_codex.resume_kwargs[-1]["approval_mode"] == ApprovalMode.deny_all
    assert calls[-1]["sandbox"] == Sandbox.workspace_write


@pytest.mark.asyncio
async def test_owner_memory_uses_its_own_persistent_read_only_thread(fake_codex, tmp_path) -> None:
    from openai_codex import ApprovalMode, Sandbox

    memory_path = tmp_path / "owner_context" / "working_context.md"
    memory_path.parent.mkdir()
    memory_path.write_text(EMPTY_WORKING_CONTEXT, encoding="utf-8")
    payload = {"changed": False, "content": EMPTY_WORKING_CONTEXT}
    fake_codex.memory_payload = payload
    provider = CodexProvider(
        model="gpt-6-luna", reasoning_effort="high", cwd=tmp_path,
        owner_context_path=memory_path,
    )

    first = await provider.update_owner_memory(
        current_content=EMPTY_WORKING_CONTEXT,
        owner_request="Продолжить исследование рынка",
        owner_outcome="Сравнили три сегмента",
        result_type="owner_query",
        completed_work="Черновой анализ готов",
        thread_id=None,
    )
    second = await provider.compact_owner_memory(content=EMPTY_WORKING_CONTEXT, thread_id=first.thread_id)

    assert first.thread_id == second.thread_id == "thread-memory"
    start = fake_codex.starts[-1]
    assert start["developer_instructions"].startswith(_OWNER_MEMORY_INSTRUCTIONS)
    assert _OWNER_CONTEXT_TRUST_BOUNDARY in start["developer_instructions"]
    assert start["model"] == "gpt-6-luna"
    assert start["config"]["model_reasoning_effort"] == "high"
    assert start["cwd"] == str(memory_path.parent)
    assert start["sandbox"] == Sandbox.read_only
    assert start["approval_mode"] == ApprovalMode.deny_all
    assert fake_codex.resume_kwargs[-1]["sandbox"] == Sandbox.read_only
    assert fake_codex.resume_kwargs[-1]["approval_mode"] == ApprovalMode.deny_all
    assert fake_codex.threads[-2].prompts[-1].find("Продолжить исследование рынка") >= 0
    assert "full owner history" not in fake_codex.threads[-2].prompts[-1]
    assert "Compact further" in fake_codex.threads[-1].prompts[-1]


@pytest.mark.asyncio
async def test_owner_memory_replaces_an_unavailable_saved_thread(fake_codex, monkeypatch, tmp_path) -> None:
    def unavailable(self, thread_id: str, **kwargs):
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(_FakeCodex, "thread_resume", unavailable)
    memory_path = tmp_path / "owner_context" / "working_context.md"
    memory_path.parent.mkdir()
    provider = CodexProvider(owner_context_path=memory_path)

    result = await provider.update_owner_memory(
        current_content=EMPTY_WORKING_CONTEXT,
        owner_request="Выбрал следующий проект",
        owner_outcome="Работа начата",
        result_type="owner_query",
        completed_work="",
        thread_id="lost-memory-thread",
    )

    assert result.thread_id == "thread-memory"
    assert fake_codex.starts[-1]["developer_instructions"].startswith(_OWNER_MEMORY_INSTRUCTIONS)


@pytest.mark.asyncio
async def test_owner_context_is_untrusted_and_never_added_to_client_turns(fake_codex, tmp_path) -> None:
    from agentbridge.owner_memory import validate_working_context

    memory_path = tmp_path / "owner_context" / "working_context.md"
    memory_path.parent.mkdir()
    content = validate_working_context(
        "# Rick Owner Working Context\n\n## Goal\n\nSentinel owner state\n\n"
        "## Active\n\n## Decisions\n\n## Constraints\n\n## Known Issues\n\n"
        "## Rejected\n\n## Next\n\n## References\n"
    )
    memory_path.write_text(content, encoding="utf-8")
    owner = CodexProvider(owner_context_path=memory_path)
    client = CodexProvider()

    await owner.answer_owner_query(
        question="What is the current goal?", chat_name="Owner", context_pack="", thread_id=None,
    )
    owner_thread = fake_codex.threads[-1]
    assert "Sentinel owner state" in owner_thread.prompts[-1]
    assert _OWNER_CONTEXT_TRUST_BOUNDARY in fake_codex.starts[-1]["developer_instructions"]
    assert "Sentinel owner state" not in fake_codex.starts[-1]["developer_instructions"]
    assert owner.prompt_version != client.prompt_version

    await client.suggest(
        message="Need the contract", sender_name="Client", chat_name="Client chat",
        wiki="", rules=[], thread_id=None,
    )
    client_thread = fake_codex.threads[-1]
    assert "Sentinel owner state" not in client_thread.prompts[-1]
    assert "working_context" not in client_thread.prompts[-1]


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
    assert fake_codex.resume_kwargs[-1]["include_turns"] is False
    assert fake_codex.starts[0]["developer_instructions"] == _INSTRUCTIONS


@pytest.mark.asyncio
async def test_codex_suggest_replaces_an_unavailable_saved_thread(fake_codex, monkeypatch) -> None:
    def unavailable(self, thread_id: str, **kwargs):
        raise InvalidRequestError(-32600, f"no rollout found for thread id {thread_id}")

    monkeypatch.setattr(_FakeCodex, "thread_resume", unavailable)
    provider = CodexProvider()

    result = await provider.suggest(
        message="Need docs", sender_name="Alice", chat_name="Acme", wiki="wiki", rules=[],
        thread_id="missing-thread",
    )

    assert result.thread_id == "thread-started"
    assert fake_codex.starts[-1]["developer_instructions"] == _INSTRUCTIONS


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

    result, thread_id = await provider.refactor_reply(draft, thread_id=None)

    assert result.suggested_reply == "Пришлю ссылку завтра в 10:00."
    assert thread_id == "thread-sepia"
    sepia_thread = fake_codex.threads[-1]
    assert fake_codex.starts[-1]["developer_instructions"] == _SEPIA_INSTRUCTIONS
    assert fake_codex.starts[-1]["config"]["model_reasoning_effort"] == "low"
    assert "Draft ответа:" in sepia_thread.prompts[0]
    assert "Недавняя история" not in sepia_thread.prompts[0]

    resumed, resumed_thread_id = await provider.refactor_reply(draft, thread_id=thread_id)
    assert resumed.suggested_reply == "Пришлю ссылку завтра в 10:00."
    assert resumed_thread_id == "thread-sepia"
    assert fake_codex.resumes == ["thread-sepia"]


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

    result, _ = await provider.refactor_reply(draft, thread_id=None)
    assert result.suggested_reply == draft.suggested_reply


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
    for schema in (
        _SUGGEST_SCHEMA, _FEEDBACK_SCHEMA, _OWNER_QUERY_SCHEMA, _OWNER_MEMORY_SCHEMA,
        _GENERAL_TASK_PLAN_SCHEMA, _ONBOARDING_SCHEMA, _SEPIA_SCHEMA,
    ):
        validate_structured_output_schema(schema)
    field = _SUGGEST_SCHEMA["properties"]["candidate_state"]
    assert field["additionalProperties"] is False
    assert set(field["required"]) == set(_CANDIDATE_STATE_PROPERTIES)
    assert field["properties"]["summary"]["type"] == ["string", "null"]
    assert field["properties"]["facts"]["type"] == ["array", "null"]
    assert set(_SUGGEST_SCHEMA["required"]) == set(_SUGGEST_SCHEMA["properties"])
    assert {"image", "leadrecord_analytics"} <= set(_GENERAL_TASK_PLAN_SCHEMA["properties"]["kind"]["enum"])
    analytics = _GENERAL_TASK_PLAN_SCHEMA["properties"]["analytics"]
    assert "skip_new_projects" in analytics["required"]
    assert analytics["properties"]["skip_new_projects"]["type"] == "boolean"
    assert "periods" in analytics["required"]
    period = analytics["properties"]["periods"]["items"]
    assert period["required"] == ["period_start", "period_end"]


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
