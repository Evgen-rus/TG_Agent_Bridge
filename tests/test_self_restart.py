from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace
import asyncio

import pytest
from telegram.ext import CallbackQueryHandler

from agentbridge.agents.base import GeneralTaskPlan
from agentbridge.application import AgentBridgeApplication
from agentbridge.restart import process_is_going_away, spawn_restart_helper
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


def test_restart_helper_refuses_outside_systemd(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Windows")
    with pytest.raises(RuntimeError, match="requires the rick systemd service"):
        spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "python")


def test_linux_restart_uses_only_fixed_systemd_unit_after_systemd_check(tmp_path, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "test")
    monkeypatch.setattr("agentbridge.restart.subprocess.run", lambda args, **kwargs: calls.append((args, kwargs)) or SimpleNamespace(returncode=0))
    spawn_restart_helper(old_pid=123, project_root=tmp_path, python_executable=tmp_path / "unused")
    assert calls[0][0] == ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "--no-block", "restart", "rick.service"]
    assert calls[0][1]["stdin"] == __import__("subprocess").DEVNULL


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


def test_restart_counts_as_done_when_the_process_is_already_dying(tmp_path, monkeypatch) -> None:
    """Главная гонка: systemctl убивает нас, поэтому ответа мы не дождёмся.

    `restart` останавливает сервис, а вместе с ним и процесс Рика. Раньше
    ненулевой код возврата читался как отказ, и владелец после каждого
    удачного перезапуска получал ложное «не смог». Теперь ненулевой код при
    уходящем процессе — это успех."""
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "test")
    monkeypatch.setattr("agentbridge.restart.subprocess.run", lambda args, **kwargs: SimpleNamespace(returncode=-15))
    monkeypatch.setattr("agentbridge.restart.process_is_going_away", lambda pid: True)
    # Ошибки нет: рестарт уже выполняется, просто нас не дождались.
    spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "unused")


def test_restart_treats_a_signalled_child_as_success(tmp_path, monkeypatch) -> None:
    """Реальный механизм на VPS: KillMode=control-group убивает дочерний systemctl.

    Юнит работает с `KillMode=control-group`, поэтому systemd останавливает весь
    контрольный набор вместе с дочерним `systemctl`, который сам только что
    отдал приказ. `subprocess.run` возвращает `-15`, хотя приказ отработал и
    сервис штатно перезапустился. Раньше именно это читалось как отказ.

    Именно этот сценарий воспроизводился на VPS, поэтому он проверяется
    отдельным тестом, а не через `process_is_going_away`."""
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "test")
    # Минус в коде возврата = убит сигналом = нас уже остановили.
    monkeypatch.setattr("agentbridge.restart.subprocess.run", lambda args, **kwargs: SimpleNamespace(returncode=-15))
    # Процесс при этом ЖИВ: python-telegram-bot ловит SIGTERM и сначала гасит
    # приложение, только потом завершается.
    monkeypatch.setattr("agentbridge.restart.process_is_going_away", lambda pid: False)
    # Ошибки нет: приказ systemctl отработал, нас убили уже после этого.
    spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "unused")


def test_restart_reports_a_real_rejection_while_the_process_survives(tmp_path, monkeypatch) -> None:
    """Настоящий отказ systemctl честно сообщается владельцу.

    Если процесс жив, код возврата положительный и он ненулевой, рестарт не
    начнётся сам — молчать об этом нельзя, иначе владелец будет ждать
    перезапуска, которого не будет."""
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "test")
    monkeypatch.setattr("agentbridge.restart.subprocess.run", lambda args, **kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr("agentbridge.restart.process_is_going_away", lambda pid: False)
    with pytest.raises(RuntimeError, match="was rejected"):
        spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "unused")


def test_restart_reports_a_timeout_while_the_process_survives(tmp_path, monkeypatch) -> None:
    """Зависший systemctl при живом процессе — тоже настоящий отказ.

    `--no-block` возвращается почти мгновенно, поэтому зависание при живом
    процессе означает, что запрос не ушёл и перезапуск не начнётся."""
    monkeypatch.setattr("agentbridge.restart.platform.system", lambda: "Linux")
    monkeypatch.setenv("INVOCATION_ID", "test")

    def _timeout(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=5)

    monkeypatch.setattr("agentbridge.restart.subprocess.run", _timeout)
    monkeypatch.setattr("agentbridge.restart.process_is_going_away", lambda pid: False)
    with pytest.raises(RuntimeError, match="timed out"):
        spawn_restart_helper(old_pid=1, project_root=tmp_path, python_executable=tmp_path / "unused")


def test_process_is_going_away_detects_a_dead_pid() -> None:
    """Мёртвый pid читается как «нас уже остановили»."""
    # Заведомо несуществующий pid: текущий процесс не может быть им.
    assert process_is_going_away(2 ** 30) is True
    assert process_is_going_away(os.getpid()) is False


def test_abandoned_prepared_marker_is_closed_on_startup(tmp_path) -> None:
    """Маркер, оставшийся в prepared, закрывается как failed.

    Процесс умирает прямо на вызове systemctl и не успевает дописать метку.
    Если новый процесс её не закроет, она навсегда останется невидимой для
    acknowledge-цикла, который ищет только launched."""
    store = ChatThreadStore(tmp_path / "restart.sqlite3")
    task_id = store.create_general_task(77, "перезагрузись", "Понял: перезапустить", "restart", {})
    store.set_general_task_status(task_id, "confirming", "executing")
    marker_id = store.create_self_restart(task_id, 77, old_pid=999_999, reason="перезагрузись")
    assert store.fail_abandoned_self_restarts(os.getpid()) == 1
    # Повторный старт ничего не находит: метка уже закрыта.
    assert store.fail_abandoned_self_restarts(os.getpid()) == 0
    assert store.pending_self_restart(-1) is None
    assert store.get_general_task(task_id).status == "failed"
    # Второй confirm на закрытую метку не действует.
    assert store.finish_self_restart(marker_id, launched=True) is False


def test_marker_of_the_current_process_is_not_treated_as_abandoned(tmp_path) -> None:
    """Свой собственный prepared закрывать нельзя.

    Он может появиться в этом же процессе, пока systemctl ещё не вызван."""
    store = ChatThreadStore(tmp_path / "restart.sqlite3")
    task_id = store.create_general_task(77, "перезагрузись", "Понял: перезапустить", "restart", {})
    store.set_general_task_status(task_id, "confirming", "executing")
    store.create_self_restart(task_id, 77, old_pid=os.getpid(), reason="перезагрузись")
    assert store.fail_abandoned_self_restarts(os.getpid()) == 0


@pytest.mark.asyncio
async def test_successful_restart_never_reports_failure_to_the_owner(tmp_path, chat_registry) -> None:
    """Полный путь: подтверждение, метка committed до systemctl, без ложной ошибки.

    Это тот сценарий, что наблюдался на VPS: рестарт проходил, а владелец
    получал «не смог». Здесь тот же порядок вызовов, что в коде бота."""
    store = ChatThreadStore(tmp_path / "restart.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, RestartProvider(), owner_chat_id=77)
    selection = await service.handle_owner_query("Рик, перезагрузись")
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)
    result = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)

    # Метка коммитится до вызова systemctl, как теперь в bot.py.
    assert service.finish_self_restart(result.restart_marker_id, launched=True)
    # systemctl убивает процесс: ответа нет, ошибки нет.
    assert process_is_going_away(2 ** 30) is True

    # Новый процесс — у него другой pid, поэтому acknowledge-цикл видит метку.
    marker = store.pending_self_restart(current_pid=os.getpid() + 1)
    assert marker.id == result.restart_marker_id
    # Значит, на старте владелец получит «Я вернулся», а не «не смог».
    assert service.acknowledge_self_restart(marker.id)
    assert store.pending_self_restart(current_pid=os.getpid() + 1) is None


def test_rejected_launch_rolls_the_committed_marker_back_to_failed(tmp_path, chat_registry) -> None:
    """Настоящий отказ systemctl не должен оставлять маркер в launched.

    Метка коммитится до вызова systemctl, иначе гонка съедает результат. Но
    если отказ настоящий, процесс выжил — и база обязана сказать правду,
    иначе следующий процесс отправит «Я вернулся», не перезапустив ничего."""
    store = ChatThreadStore(tmp_path / "restart.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, RestartProvider(), owner_chat_id=77)

    task_id = store.create_general_task(77, "перезагрузись", "Понял: перезапустить", "restart", {})
    store.set_general_task_status(task_id, "confirming", "executing")
    marker_id = store.create_self_restart(task_id, 77, old_pid=os.getpid(), reason="перезагрузись")

    assert service.finish_self_restart(marker_id, launched=True)
    # systemctl отказал, процесс выжил.
    assert service.abort_self_restart(marker_id)
    # finish(launched=False) уже не сработает: метка не prepared.
    assert service.finish_self_restart(marker_id, launched=False) is False
    assert store.get_general_task(task_id).status == "failed"
    # Новый процесс не отправит «Я вернулся».
    assert store.pending_self_restart(current_pid=os.getpid() + 1) is None
