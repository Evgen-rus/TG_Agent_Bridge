from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time


def _log(path: Path, message: str) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now().astimezone().isoformat()} {message}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-pid", type=int, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    logs = root / "runtime" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "restart.log"
    try:
        _log(log, f"helper_started old_pid={args.old_pid}")
        args.ready_file.parent.mkdir(parents=True, exist_ok=True)
        args.ready_file.touch()
        deadline = time.monotonic() + 60
        while _process_exists(args.old_pid):
            if time.monotonic() >= deadline:
                _log(log, f"old_process_timeout old_pid={args.old_pid}")
                return 2
            time.sleep(0.25)
        _log(log, f"old_process_exited old_pid={args.old_pid}")
        stdout = (logs / "restart-child.stdout.log").open("ab")
        stderr = (logs / "restart-child.stderr.log").open("ab")
        child = subprocess.Popen(
            [sys.executable, "-m", "agentbridge.main"], cwd=root,
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        time.sleep(2)
        if child.poll() is not None:
            _log(log, f"child_exited pid={child.pid} exit_code={child.returncode}")
            return 3
        _log(log, f"child_started pid={child.pid}")
        return 0
    except Exception as exc:
        _log(log, f"helper_failed error={type(exc).__name__}: {exc}")
        return 1
    finally:
        args.ready_file.unlink(missing_ok=True)


def _process_exists(pid: int) -> bool:
    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
