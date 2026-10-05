from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile


OWNER_MEMORY_THREAD_KEY = -2
OWNER_MEMORY_THREAD_NAME = "Рабочая память владельца"
OWNER_MEMORY_PROMPT_VERSION = 1
OWNER_MEMORY_RELATIVE_PATH = Path("owner_context/working_context.md")
OWNER_MEMORY_COMMIT_MESSAGE = "context(owner): update working memory"

TARGET_CHARS = 8_000
SOFT_LIMIT_CHARS = 10_000
HARD_LIMIT_CHARS = 12_000
ABSOLUTE_LIMIT_BYTES = 16 * 1024

SECTION_HEADINGS = (
    "Goal",
    "Active",
    "Decisions",
    "Constraints",
    "Known Issues",
    "Rejected",
    "Next",
    "References",
)

EMPTY_WORKING_CONTEXT = "# Rick Owner Working Context\n\n" + "\n\n".join(
    f"## {heading}" for heading in SECTION_HEADINGS
) + "\n"


class WorkingContextValidationError(ValueError):
    pass


class WorkingContextCommitError(RuntimeError):
    def __init__(self, error_type: str, returncode: int | None = None):
        super().__init__("working context Git commit failed")
        self.error_type = error_type
        self.returncode = returncode


@dataclass(frozen=True)
class WorkingContextWrite:
    changed: bool
    chars_before: int
    bytes_before: int
    chars_after: int
    bytes_after: int


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|passwd|secret)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
)


def normalize_working_context(content: str) -> str:
    if not isinstance(content, str):
        raise WorkingContextValidationError("content must be a string")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    return normalized + "\n" if normalized else ""


def validate_working_context(content: str) -> str:
    normalized = normalize_working_context(content)
    if not normalized:
        raise WorkingContextValidationError("content is empty")
    lines = [line.strip() for line in normalized.splitlines()]
    expected = ["# Rick Owner Working Context", *(f"## {item}" for item in SECTION_HEADINGS)]
    actual = [line for line in lines if line.startswith("#")]
    if actual != expected:
        raise WorkingContextValidationError("content has an unexpected heading structure")
    if any(pattern.search(normalized) for pattern in _SECRET_PATTERNS):
        raise WorkingContextValidationError("content appears to contain a credential")
    if any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise WorkingContextValidationError("content contains control characters")
    return normalized


def memory_size(content: str) -> tuple[int, int]:
    return len(content), len(content.encode("utf-8"))


def read_working_context(project_root: Path) -> str:
    path = project_root / OWNER_MEMORY_RELATIVE_PATH
    try:
        if path.is_symlink():
            return EMPTY_WORKING_CONTEXT
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_WORKING_CONTEXT


def write_working_context(project_root: Path, content: str) -> WorkingContextWrite:
    normalized = validate_working_context(content)
    root = project_root.resolve()
    path = root / OWNER_MEMORY_RELATIVE_PATH
    directory = path.parent
    if directory.is_symlink():
        raise OSError("owner context directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)

    try:
        old_content = path.read_text(encoding="utf-8") if not path.is_symlink() else ""
    except FileNotFoundError:
        old_content = ""
    old_chars, old_bytes = memory_size(old_content)
    new_chars, new_bytes = memory_size(normalized)
    if old_content == normalized:
        return WorkingContextWrite(False, old_chars, old_bytes, new_chars, new_bytes)

    old_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() and not path.is_symlink() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=".working_context.", suffix=".tmp", dir=directory)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), old_mode)
            stream.write(normalized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)

    return WorkingContextWrite(True, old_chars, old_bytes, new_chars, new_bytes)


def commit_working_context(project_root: Path) -> None:
    """Commit only the fixed memory path, leaving unrelated index entries alone."""
    try:
        with tempfile.TemporaryDirectory(prefix="agentbridge-owner-memory-hooks-") as hooks_path:
            result = subprocess.run(
                [
                    "git", "-c", f"core.hooksPath={hooks_path}", "-c", "commit.gpgsign=false",
                    "commit", "--only", "-m", OWNER_MEMORY_COMMIT_MESSAGE,
                    "--", OWNER_MEMORY_RELATIVE_PATH.as_posix(),
                ],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkingContextCommitError(type(exc).__name__) from None
    if result.returncode != 0:
        raise WorkingContextCommitError("GitCommandFailed", result.returncode)
