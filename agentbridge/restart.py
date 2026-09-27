from __future__ import annotations

import platform
import os
from pathlib import Path
import subprocess


def self_restart_supported() -> bool:
    return platform.system() == "Linux" and bool(os.getenv("INVOCATION_ID"))


def spawn_restart_helper(*, old_pid: int, project_root: Path, python_executable: Path) -> None:
    if not self_restart_supported():
        raise RuntimeError("Self-restart requires the rick systemd service.")
    result = subprocess.run(
        ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "--no-block", "restart", "rick.service"],
        cwd=project_root.resolve(), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=5, check=False,
    )
    if result.returncode:
        raise RuntimeError("Fixed systemd restart request was rejected")
