from __future__ import annotations

import os
import platform
from pathlib import Path
import subprocess


def self_restart_supported() -> bool:
    return platform.system() == "Linux" and bool(os.getenv("INVOCATION_ID"))


def process_is_going_away(pid: int) -> bool:
    """Жив ли ещё процесс, который сейчас держит SQLite и Telegram.

    Вспомогательная проверка, а не основной критерий. Процесс при штатном
    перезапуске остаётся жив ещё несколько секунд: python-telegram-botics
    ловит SIGTERM через event loop и сначала корректно гасит приложение.
    Поэтому на момент проверки процесс обычно ещё на месте, даже когда
    перезапуск уже идёт.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Процесс есть, но он наш: значит, нас ещё не остановили.
        return False
    return False


def spawn_restart_helper(*, old_pid: int, project_root: Path, python_executable: Path) -> None:
    """Запросить перезапуск и вернуться, только если он точно не состоялся.

    Ключевой момент: юнит работает с `KillMode=control-group`, поэтому systemd
    по сигналу останавливает весь контрольный набор, а вместе с ним и дочерний
    `systemctl`, который сам же только что отдал приказ. Дочерний процесс
    умирает от SIGTERM, и `subprocess.run` возвращает `-15`.

    Минус в коде возврата — это не «systemctl отказал», а «нас остановили».
    Считать минус отказом нельзя: именно это происходило при каждом удачном
    перезапуске, и владелец получал «не смог запустить перезапуск» от живого
    и исправного сервиса.

    Настоящий отказ выглядит иначе: systemctl отвечает кодом 1 и при этом
    никто не посылает нам сигнал. Только его и считаем отказом.
    """
    if not self_restart_supported():
        raise RuntimeError("Self-restart requires the rick systemd service.")
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "--no-block", "restart", "rick.service"],
            cwd=project_root.resolve(), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # `--no-block` возвращается почти мгновенно, поэтому зависание здесь
        # означает, что запрос уже ушёл, а ответ мы не дождались.
        if not process_is_going_away(old_pid):
            raise RuntimeError("Fixed systemd restart request timed out") from exc
        return
    if result.returncode == 0:
        return
    if result.returncode < 0:
        # Нас остановили по KillMode=control-group: перезапуск уже выполняется,
        # просто дочерний systemctl умер вместе с нами.
        return
    if process_is_going_away(old_pid):
        return
    raise RuntimeError("Fixed systemd restart request was rejected")
