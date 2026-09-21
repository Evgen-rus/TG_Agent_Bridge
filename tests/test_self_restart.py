from __future__ import annotations

from types import SimpleNamespace
import asyncio

import pytest
from telegram.ext import CallbackQueryHandler

from agentbridge.agents.base import GeneralTaskPlan
from agentbridge.application import AgentBridgeApplication
from agentbridge.restart import spawn_restart_helper
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application


class RestartProvider:
    async def plan_general_task(self, **kwargs):
        return GeneralTaskPlan("thread", "Перезапустить AgentBridge.", "restart")


@pytest.mark.asyncio
async def test_restart_is_durable_and_only_created_after_confirmation(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "restart.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, RestartProvider(), owner_chat_id=77)
    selection = await service.handle_owner_query("Рик, перезагрузись")
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)

    assert store.pending_self_restart(-1) is None
    result = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    assert result.restart_marker_id is not None
    assert "Перезапускаюсь" in result.text

    assert service.finish_self_restart(result.restart_marker_id, launched=True)
    marker = store.pending_self_restart(-1)
    assert marker.id == result.restart_marker_id and marker.owner_chat_id == 77
    assert not service.finish_self_restart(marker.id, launched=True)
    assert service.acknowledge_self_restart(marker.id)
    assert store.pending_self_restart(-1) is None


def test_restart_helper_uses_current_project_python_and_old_pid(tmp_path, monkeypatch) -> None:
    root = tmp_path / "project"
    helper = root / "scripts" / "restart_agentbridge.ps1"
    python = root / ".venv" / "Scripts" / "python.exe"
    helper.parent.mkdir(parents=True)
    python.parent.mkdir(parents=True)
    helper.touch()
    python.touch()
    calls = []
    monkeypatch.setattr("agentbridge.restart.self_restart_supported", lambda: True)
    monkeypatch.setattr("agentbridge.restart.subprocess.Popen", lambda args, **kwargs: calls.append((args, kwargs)))

    spawn_restart_helper(old_pid=123, project_root=root, python_executable=python)

    args, kwargs = calls[0]
    assert ["-OldPid", "123"] == args[args.index("-OldPid"):args.index("-OldPid") + 2]
    assert str(root.resolve()) in args and str(python.resolve()) in args
    assert kwargs["cwd"] == root.resolve()
    assert kwargs["creationflags"] & __import__("subprocess").CREATE_BREAKAWAY_FROM_JOB


def test_restart_helper_refuses_non_windows(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    with pytest.raises(RuntimeError, match="only on Windows"):
        spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "python")


def test_callback_handler_accepts_current_general_and_portfolio_buttons() -> None:
    app = create_telegram_application(token="test-token", owner_chat_id=77, message_service=SimpleNamespace())
    handler = next(item for item in app.handlers[0] if isinstance(item, CallbackQueryHandler))
    for data in (
        "general:confirm:1", "general:refine:1", "general:cancel:1",
        "portfolio:general:2", "portfolio:choose:2:0", "portfolio:all:2",
        "learn:yes:3", "onboard:no:3", "memory:global:3",
    ):
        assert handler.pattern.match(data), data


@pytest.mark.asyncio
async def test_new_process_acknowledges_pending_restart_once() -> None:
    marker = SimpleNamespace(id=9)

    class Service:
        acknowledged = False

        def pending_self_restart(self, current_pid):
            return None if self.acknowledged else marker

        def acknowledge_self_restart(self, restart_id):
            self.acknowledged = restart_id == marker.id

    class Bot:
        sent = []

        async def set_my_commands(self, commands, scope=None):
            pass

        async def send_message(self, **kwargs):
            self.sent.append(kwargs)
            return SimpleNamespace(message_id=1)

    service, bot = Service(), Bot()
    app = create_telegram_application(
        token="test-token", owner_chat_id=77, message_service=service,
        delivery_retry_seconds=0.01,
    )
    app.bot = bot
    app._running = True
    await app.post_init(app)
    await asyncio.sleep(0.05)
    await app.post_stop(app)

    assert service.acknowledged
    assert [item["text"] for item in bot.sent] == ["Я вернулся. Мозги обновил, реальность не развалилась. Работаем."]
