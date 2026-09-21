from __future__ import annotations

import platform
from pathlib import Path
import subprocess
import time


def self_restart_supported() -> bool:
    return platform.system() == "Windows"


def spawn_restart_helper(*, old_pid: int, project_root: Path, python_executable: Path) -> None:
    if not self_restart_supported():
        raise RuntimeError("Self-restart is supported only on Windows.")
    root = project_root.resolve()
    python = python_executable.resolve()
    ready = root / "runtime" / f"restart-helper-{old_pid}.ready"
    if old_pid <= 0 or not python.is_file():
        raise RuntimeError("Self-restart paths or PID are invalid.")
    ready.unlink(missing_ok=True)
    helper = subprocess.Popen(
        [
            str(python), "-m", "agentbridge.restart_helper",
            "--old-pid", str(old_pid), "--project-root", str(root), "--ready-file", str(ready),
        ],
        cwd=root,
        creationflags=(
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_BREAKAWAY_FROM_JOB
        ),
        close_fds=True,
    )
    for _ in range(30):
        if ready.is_file():
            return
        if helper.poll() is not None:
            raise RuntimeError(f"Self-restart helper exited with code {helper.returncode}.")
        time.sleep(0.1)
    raise RuntimeError("Self-restart helper did not become ready.")
