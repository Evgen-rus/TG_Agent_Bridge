from __future__ import annotations

import platform
from pathlib import Path
import subprocess


def self_restart_supported() -> bool:
    return platform.system() == "Windows"


def spawn_restart_helper(*, old_pid: int, project_root: Path, python_executable: Path) -> None:
    if not self_restart_supported():
        raise RuntimeError("Self-restart is supported only on Windows.")
    root = project_root.resolve()
    python = python_executable.resolve()
    helper = root / "scripts" / "restart_agentbridge.ps1"
    if old_pid <= 0 or not helper.is_file() or not python.is_file():
        raise RuntimeError("Self-restart paths or PID are invalid.")
    subprocess.Popen(
        [
            "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(helper), "-OldPid", str(old_pid), "-ProjectRoot", str(root),
            "-PythonExecutable", str(python),
        ],
        cwd=root,
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
