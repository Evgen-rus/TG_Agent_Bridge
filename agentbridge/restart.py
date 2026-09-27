from __future__ import annotations

import os
import platform
from pathlib import Path
import subprocess


def self_restart_supported() -> bool:
    return platform.system() == "Linux" and bool(os.getenv("INVOCATION_ID"))


def process_is_going_away(pid: int) -> bool:
    """Жив ли ещё процесс, который сейчас держит SQLite и Telegram.

    Нужен, чтобы отличить состоявшийся перезапуск от отказа. `systemctl
    --no-block restart` останавливает сервис, а значит и сам процесс Рика, и
    возврата из `subprocess.run` может не быть вовсе:нас убивают в момент
    вызова. Код возврата в такой ситуации — это не «systemctl отказал», а
    «нас не дождались», поэтому по нему судить нельзя.
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

    Ошибка поднимается в одном-единственном случае: команда вернулась, процесс
    на месте, и код возврата ненулевой. Тогда systemctl отказал по-настоящему
    (например, правило sudoers снесли) и Рик должен честно сказать об этом.

    Все остальные исходы — успех. Процесс не дождал ответа, потому что его
    остановили, и рестарт уже в работе. Раньше здесь поднималась ошибка по
    любому ненулевому коду, и Рик после каждого удачного перезапуска писал
    владельцу «не смог», хотя сервис штатно поднимался через пару секунд.
    """
    if not self_restart_supported():
        raise RuntimeError("Self-restart requires the rick systemd service.")
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "--no-block", "restart", "rick.service"],
        cwd=project_root.resolve(), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    if not result.returncode:
        return
    if process_is_going_away(old_pid):
        # Нас успели остановить: systemd рестарт уже выполняет.
        return
    raise RuntimeError("Fixed systemd restart request was rejected")
