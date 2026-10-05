from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess

import pytest

import agentbridge.application as application_module
import agentbridge.owner_memory as memory_module
from agentbridge.agents.base import AgentReply, OwnerMemoryUpdate, OwnerQueryAnswer
from agentbridge.agents.codex import OWNER_CONTEXT_PROMPT_VERSION
from agentbridge.application import AgentBridgeApplication
from agentbridge.chats.loader import ChatConfig, ChatRegistry
from agentbridge.owner_memory import (
    ABSOLUTE_LIMIT_BYTES,
    EMPTY_WORKING_CONTEXT,
    HARD_LIMIT_CHARS,
    OWNER_MEMORY_THREAD_KEY,
    SECTION_HEADINGS,
    SOFT_LIMIT_CHARS,
    WorkingContextCommitError,
    commit_working_context,
    validate_working_context,
    write_working_context,
)
from agentbridge.storage.sqlite import ChatThreadStore


def _context(goal: str = "") -> str:
    bodies = {heading: "" for heading in SECTION_HEADINGS}
    bodies["Goal"] = goal
    return "# Rick Owner Working Context\n\n" + "\n\n".join(
        f"## {heading}\n\n{bodies[heading]}" for heading in SECTION_HEADINGS
    ).rstrip() + "\n"


def _oversized_context(minimum_chars: int) -> str:
    content = _context("x" * minimum_chars)
    assert len(content) > minimum_chars
    return content


def _registry(tmp_path: Path) -> tuple[ChatRegistry, ChatConfig]:
    chat = ChatConfig(-100123456, "Demo Chat", "codex", "", tmp_path / "chats" / "demo")
    return ChatRegistry({chat.telegram_chat_id: chat}), chat


async def _wait_for_memory_tasks(app: AgentBridgeApplication) -> None:
    tasks = tuple(app._owner_memory_tasks)
    if tasks:
        await asyncio.gather(*tasks)


class _MemoryOwnerProvider:
    prompt_version = 14
    memory_prompt_version = 1

    def __init__(self, *, update_result=None, failure: Exception | None = None):
        self.answer_calls: list[dict] = []
        self.memory_calls: list[dict] = []
        self.update_result = update_result or OwnerMemoryUpdate("memory-thread", False, "ignored")
        self.failure = failure
        self.compact_result = None
        self.compact_calls: list[dict] = []

    async def answer_owner_query(self, **kwargs):
        self.answer_calls.append(kwargs)
        return OwnerQueryAnswer("owner-thread", "Решили продолжить текущий проект.")

    async def update_owner_memory(self, **kwargs):
        self.memory_calls.append(kwargs)
        if self.failure:
            raise self.failure
        if callable(self.update_result):
            return self.update_result(**kwargs)
        return self.update_result

    async def compact_owner_memory(self, **kwargs):
        self.compact_calls.append(kwargs)
        if callable(self.compact_result):
            return self.compact_result(**kwargs)
        return self.compact_result


def _app(tmp_path: Path, provider, *, enabled=True) -> tuple[AgentBridgeApplication, ChatConfig, Path]:
    registry, chat = _registry(tmp_path)
    root = tmp_path / "repo"
    memory_path = root / "owner_context" / "working_context.md"
    write_working_context(root, EMPTY_WORKING_CONTEXT)
    app = AgentBridgeApplication(
        registry,
        ChatThreadStore(tmp_path / "runtime.sqlite3"),
        provider,
        owner_chat_id=77,
        owner_provider=provider,
        owner_working_memory_enabled=enabled,
        owner_working_memory_path=memory_path,
    )
    return app, chat, memory_path


@pytest.mark.asyncio
async def test_only_owner_contour_schedules_memory_updates(tmp_path) -> None:
    provider = _MemoryOwnerProvider()
    app, chat, _ = _app(tmp_path, provider)
    class ClientProvider:
        async def suggest(self, **kwargs):
            return AgentReply("client-thread", "Состояние", "Ответ")

    app.provider = ClientProvider()
    await app.handle_message(chat.telegram_chat_id, "Alice", "A client asks about timing")

    assert provider.memory_calls == []
    assert app.store.get_thread_id(chat.telegram_chat_id) is not None


@pytest.mark.asyncio
async def test_owner_memory_receives_only_current_md_and_owner_delta(tmp_path, monkeypatch) -> None:
    provider = _MemoryOwnerProvider()
    app, chat, _ = _app(tmp_path, provider)
    monkeypatch.setattr(app, "_context_pack", lambda *args, **kwargs: "CLIENT_HISTORY_SENTINEL")

    answer = await app._answer_owner_query_for_chat(chat, "Что решили по текущему плану проекта?")
    await _wait_for_memory_tasks(app)

    assert answer == "Решили продолжить текущий проект."
    assert "CLIENT_HISTORY_SENTINEL" in provider.answer_calls[0]["context_pack"]
    assert len(provider.memory_calls) == 1
    memory_call = provider.memory_calls[0]
    assert memory_call["owner_request"] == "Что решили по текущему плану проекта?"
    assert memory_call["owner_outcome"] == answer
    assert memory_call["current_content"] == EMPTY_WORKING_CONTEXT
    assert "CLIENT_HISTORY_SENTINEL" not in str(memory_call)
    assert "SQLite" not in str(memory_call)
    assert app.store.get_owner_query_thread_id(OWNER_MEMORY_THREAD_KEY) == "memory-thread"


@pytest.mark.asyncio
async def test_memory_failure_does_not_delay_or_break_owner_result(tmp_path, caplog) -> None:
    provider = _MemoryOwnerProvider(failure=RuntimeError("synthetic failure"))
    app, chat, _ = _app(tmp_path, provider)

    answer = await app._answer_owner_query_for_chat(chat, "Что решили по текущему плану проекта?")
    assert answer == "Решили продолжить текущий проект."
    await _wait_for_memory_tasks(app)

    assert "event=owner_memory_agent_failed" in caplog.text


@pytest.mark.asyncio
async def test_owner_answer_returns_while_memory_agent_is_still_running(tmp_path) -> None:
    class SlowProvider(_MemoryOwnerProvider):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def update_owner_memory(self, **kwargs):
            self.memory_calls.append(kwargs)
            self.started.set()
            await self.release.wait()
            return self.update_result

    provider = SlowProvider()
    app, chat, _ = _app(tmp_path, provider)

    answer = await asyncio.wait_for(
        app._answer_owner_query_for_chat(chat, "Что решили по текущему плану проекта?"), timeout=0.2,
    )
    await asyncio.wait_for(provider.started.wait(), timeout=0.2)
    assert answer == "Решили продолжить текущий проект."
    provider.release.set()
    await _wait_for_memory_tasks(app)


@pytest.mark.asyncio
async def test_disabled_flag_skips_memory_but_keeps_owner_answer(tmp_path) -> None:
    provider = _MemoryOwnerProvider()
    app, chat, _ = _app(tmp_path, provider, enabled=False)

    answer = await app._answer_owner_query_for_chat(chat, "Что решили по текущему плану проекта?")

    assert answer == "Решили продолжить текущий проект."
    assert provider.memory_calls == []


def test_acknowledgement_does_not_schedule_memory_update(tmp_path) -> None:
    provider = _MemoryOwnerProvider()
    app, _, _ = _app(tmp_path, provider)

    app._schedule_owner_memory_update("Принято", "Спасибо", result_type="owner_query")

    assert app._owner_memory_tasks == set()
    assert provider.memory_calls == []


@pytest.mark.asyncio
async def test_duplicate_owner_update_schedules_memory_only_once(tmp_path) -> None:
    provider = _MemoryOwnerProvider()
    app, _, _ = _app(tmp_path, provider)

    first = await app.handle_owner_query("Что решили по плану Demo Chat?", update_id=42)
    duplicate = await app.handle_owner_query("Что решили по плану Demo Chat?", update_id=42)
    await _wait_for_memory_tasks(app)

    assert first is not None
    assert duplicate is None
    assert len(provider.answer_calls) == 1
    assert len(provider.memory_calls) == 1


@pytest.mark.asyncio
async def test_general_developer_and_leadrecord_results_are_memory_deltas(tmp_path) -> None:
    class TaskProvider(_MemoryOwnerProvider):
        async def run_general_task(self, *, request, thread_id):
            return OwnerQueryAnswer(thread_id or "general-thread", "General task finished.")

        async def run_code_change(self, *, request, thread_id):
            return OwnerQueryAnswer(thread_id, "Changed files: agentbridge/application.py\nChecks: OK")

    provider = TaskProvider()
    app, _, _ = _app(tmp_path, provider)

    app.store.save_owner_query_thread(0, "Общие задачи", "general-thread", prompt_version=OWNER_CONTEXT_PROMPT_VERSION)
    general_id = app.store.create_general_task(77, "Согласовать план", "Согласовать план", "general", {})
    general_result = await app.handle_general_task_action(general_id, "confirm", 77)
    await _wait_for_memory_tasks(app)
    assert general_result.text == "General task finished."
    assert provider.memory_calls[-1]["result_type"] == "general_task"
    assert "Согласовать план" in provider.memory_calls[-1]["owner_request"]

    app.store.save_owner_query_thread(-1, "Разработка AgentBridge", "developer-thread", prompt_version=OWNER_CONTEXT_PROMPT_VERSION)
    developer_id = app.store.create_general_task(
        77, "Поправить owner memory", "Поправить owner memory", "code_change", {"developer_user_id": 5},
    )
    developer_result = await app.handle_general_task_action(developer_id, "confirm", 77, user_id=5)
    await _wait_for_memory_tasks(app)
    assert "Checks: OK" in developer_result.text
    assert provider.memory_calls[-1]["result_type"] == "developer_task"
    assert "agentbridge/application.py" in provider.memory_calls[-1]["completed_work"]

    class LeadRecord:
        async def run(self, value, output, remember_run):
            return "LeadRecord: 4 projects reviewed", {"rows": 4}, output / "report.xlsx"

    app.leadrecord_client = LeadRecord()
    leadrecord_id = app.store.create_general_task(
        77, "Проверить аналитику проектов", "Аналитика", "leadrecord_analytics",
        {"analytics": {"group_id": 12}},
    )
    leadrecord_result = await app.handle_general_task_action(leadrecord_id, "confirm", 77)
    await _wait_for_memory_tasks(app)
    assert leadrecord_result.text == "LeadRecord: 4 projects reviewed"
    assert provider.memory_calls[-1]["result_type"] == "leadrecord_analytics"
    assert "LeadRecord analytics completed" in provider.memory_calls[-1]["completed_work"]


@pytest.mark.asyncio
async def test_changed_false_does_not_write_or_commit(tmp_path, monkeypatch) -> None:
    provider = _MemoryOwnerProvider(update_result=OwnerMemoryUpdate("memory-thread", False, "not markdown"))
    app, _, path = _app(tmp_path, provider)
    before = path.read_text(encoding="utf-8")
    writes: list[str] = []
    commits: list[Path] = []
    monkeypatch.setattr(application_module, "write_working_context", lambda root, content: writes.append(content))
    monkeypatch.setattr(application_module, "commit_working_context", lambda root: commits.append(root))

    await app._update_owner_working_memory(
        "Зафиксировали направление проекта", "Продолжить по плану", result_type="owner_query", completed_work="",
    )

    assert path.read_text(encoding="utf-8") == before
    assert writes == []
    assert commits == []


@pytest.mark.asyncio
async def test_soft_limit_runs_exactly_one_compaction_on_same_thread(tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level("INFO")
    large = _oversized_context(SOFT_LIMIT_CHARS)
    compacted = _context("Продолжить рабочий проект.")
    provider = _MemoryOwnerProvider(update_result=OwnerMemoryUpdate("memory-thread", True, large))
    provider.compact_result = OwnerMemoryUpdate("memory-thread", True, compacted)
    app, _, path = _app(tmp_path, provider)
    commits: list[Path] = []
    monkeypatch.setattr(application_module, "commit_working_context", lambda root: commits.append(root))

    await app._update_owner_working_memory(
        "Владелец выбрал следующее направление", "План сохранён", result_type="general_task", completed_work="",
    )

    assert len(provider.memory_calls) == 1
    assert len(provider.compact_calls) == 1
    assert provider.compact_calls[0]["thread_id"] == "memory-thread"
    assert provider.compact_calls[0]["content"] == large
    assert path.read_text(encoding="utf-8") == validate_working_context(compacted)
    assert len(commits) == 1
    assert "event=owner_memory_compaction_retry" in caplog.text


@pytest.mark.asyncio
async def test_oversized_final_result_keeps_previous_memory_and_skips_commit(tmp_path, monkeypatch, caplog) -> None:
    original = _context("Сохранённая цель.")
    too_large = _oversized_context(HARD_LIMIT_CHARS)
    provider = _MemoryOwnerProvider(update_result=OwnerMemoryUpdate("memory-thread", True, _oversized_context(SOFT_LIMIT_CHARS)))
    provider.compact_result = OwnerMemoryUpdate("memory-thread", True, too_large)
    app, _, path = _app(tmp_path, provider)
    write_working_context(path.parents[1], original)
    commits: list[Path] = []
    monkeypatch.setattr(application_module, "commit_working_context", lambda root: commits.append(root))

    await app._update_owner_working_memory(
        "Владелец обозначил новую цель проекта", "Появился следующий шаг", result_type="owner_query", completed_work="",
    )

    assert path.read_text(encoding="utf-8") == validate_working_context(original)
    assert commits == []
    assert "event=owner_memory_rejected_size" in caplog.text


@pytest.mark.asyncio
async def test_utf8_absolute_guard_triggers_compaction_and_rejects_oversized_bytes(tmp_path, monkeypatch, caplog) -> None:
    caplog.set_level("INFO")
    unicode_large = _context("界" * 6000)
    unicode_still_too_large = _context("界" * 5500)
    assert len(unicode_large) <= SOFT_LIMIT_CHARS
    assert len(unicode_large.encode("utf-8")) > ABSOLUTE_LIMIT_BYTES
    assert len(unicode_still_too_large) <= HARD_LIMIT_CHARS
    assert len(unicode_still_too_large.encode("utf-8")) > ABSOLUTE_LIMIT_BYTES
    provider = _MemoryOwnerProvider(update_result=OwnerMemoryUpdate("memory-thread", True, unicode_large))
    provider.compact_result = OwnerMemoryUpdate("memory-thread", True, unicode_still_too_large)
    app, _, path = _app(tmp_path, provider)
    previous = path.read_text(encoding="utf-8")
    commits: list[Path] = []
    monkeypatch.setattr(application_module, "commit_working_context", lambda root: commits.append(root))

    await app._update_owner_working_memory(
        "Обсудили устойчивую стратегию", "Зафиксировали основу", result_type="owner_query", completed_work="",
    )

    assert len(provider.compact_calls) == 1
    assert path.read_text(encoding="utf-8") == previous
    assert commits == []
    assert "event=owner_memory_compaction_retry" in caplog.text
    assert "event=owner_memory_rejected_size" in caplog.text


def test_atomic_write_replaces_only_after_complete_temp_file(tmp_path, monkeypatch) -> None:
    original = _context("Старая цель.")
    updated = _context("Новая цель.")
    root = tmp_path / "repo"
    write_working_context(root, original)
    path = root / "owner_context" / "working_context.md"
    real_replace = memory_module.os.replace
    observed: list[tuple[Path, Path]] = []

    def replace(source, target):
        assert path.read_text(encoding="utf-8") == validate_working_context(original)
        assert Path(source).parent == path.parent
        observed.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(memory_module.os, "replace", replace)
    result = write_working_context(root, updated)

    assert result.changed is True
    assert path.read_text(encoding="utf-8") == validate_working_context(updated)
    assert len(observed) == 1


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test Owner")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "owner_context").mkdir()
    (root / "owner_context" / "working_context.md").write_text(EMPTY_WORKING_CONTEXT, encoding="utf-8")
    (root / "staged.txt").write_text("base staged\n", encoding="utf-8")
    (root / "unstaged.txt").write_text("base unstaged\n", encoding="utf-8")
    _git(root, "add", "owner_context/working_context.md", "staged.txt", "unstaged.txt")
    _git(root, "commit", "-qm", "base")


def test_memory_commit_contains_only_memory_and_preserves_other_index_changes(tmp_path, monkeypatch) -> None:
    root = tmp_path / "git-repo"
    _init_git_repo(root)
    (root / "staged.txt").write_text("foreign staged change\n", encoding="utf-8")
    _git(root, "add", "staged.txt")
    (root / "unstaged.txt").write_text("foreign unstaged change\n", encoding="utf-8")
    staged_blob_before = _git(root, "rev-parse", ":staged.txt")
    commands: list[list[str]] = []
    real_run = memory_module.subprocess.run

    def record_run(args, **kwargs):
        if "commit" in args:
            commands.append(list(args))
        return real_run(args, **kwargs)

    monkeypatch.setattr(memory_module.subprocess, "run", record_run)
    write_working_context(root, _context("Удерживать фокус на проекте."))
    commit_working_context(root)

    committed_paths = _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines()
    assert committed_paths == ["owner_context/working_context.md"]
    assert _git(root, "diff", "--cached", "--name-only").splitlines() == ["staged.txt"]
    assert _git(root, "diff", "--name-only").splitlines() == ["unstaged.txt"]
    assert _git(root, "rev-parse", ":staged.txt") == staged_blob_before
    assert len(commands) == 1
    assert commands[0][commands[0].index("commit") + 1:commands[0].index("--")] == [
        "--only", "-m", "context(owner): update working memory",
    ]
    assert "push" not in commands[0]


@pytest.mark.asyncio
async def test_git_failure_leaves_updated_file_and_does_not_retry(tmp_path, monkeypatch, caplog) -> None:
    provider = _MemoryOwnerProvider(update_result=OwnerMemoryUpdate("memory-thread", True, _context("Обновлённая цель.")))
    app, _, path = _app(tmp_path, provider)
    calls: list[Path] = []

    def failed_commit(root):
        calls.append(root)
        raise WorkingContextCommitError("GitCommandFailed", 1)

    monkeypatch.setattr(application_module, "commit_working_context", failed_commit)
    await app._update_owner_working_memory(
        "Зафиксировали приоритет проекта", "Приоритет обновлён", result_type="owner_query", completed_work="",
    )

    assert "Обновлённая цель." in path.read_text(encoding="utf-8")
    assert len(calls) == 1
    assert "event=owner_memory_git_commit_failed" in caplog.text
