"""Fixed Sales Buddy restart boundary; never accepts a service or command from Telegram."""
from __future__ import annotations

import os
import subprocess

from agentbridge.restart import process_is_going_away, self_restart_supported


def restart_sales() -> None:
    if not self_restart_supported():
        raise RuntimeError('Sales restart requires systemd')
    try:
        result = subprocess.run(
            ['/usr/bin/sudo', '-n', '/usr/bin/systemctl', '--no-block', 'restart', 'sales-buddy.service'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False)
    except subprocess.TimeoutExpired as exc:
        if not process_is_going_away(os.getpid()):
            raise RuntimeError('Sales restart timed out') from exc
        return
    if result.returncode > 0 and not process_is_going_away(os.getpid()):
        raise RuntimeError('Sales restart was rejected')
