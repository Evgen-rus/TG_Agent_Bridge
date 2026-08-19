"""Temporary client-media cache: files live only for the current Codex turn."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import time

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/bmp",
    "image/tiff",
}
_UNSAFE_NAME = re.compile(r"[^\w.\-]+", re.UNICODE)
DEFAULT_MEDIA_TTL_SECONDS = 3600
# Bot API отдаёт файлы до 20 МБ; больше даже не пытаемся качать.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class MediaRef:
    kind: str
    file_id: str
    filename: str = ""
    mime: str = ""
    file_size: int | None = None
    media_group_id: str = ""


def is_visual_media(kind: str, mime: str = "", filename: str = "") -> bool:
    if (kind or "").strip().casefold() in {"photo", "image"}:
        return True
    if (mime or "").strip().casefold() in _IMAGE_MIMES:
        return True
    return Path(filename or "").suffix.casefold() in _IMAGE_EXTS


def media_label(kind: str, filename: str = "", *, unavailable: bool = False) -> str:
    suffix = ", файл недоступен" if unavailable else ""
    if (kind or "").strip().casefold() == "photo":
        return f"[фото{suffix}]"
    name = (filename or "").strip()
    if name:
        return f"[файл: {name}{suffix}]"
    if kind:
        return f"[вложение{suffix}]"
    return ""


def display_message_text(text: str, kind: str = "", filename: str = "") -> str:
    body = (text or "").strip()
    label = media_label(kind, filename)
    if body and label:
        return f"{label} {body}"
    return body or label


def has_message_content(text: str, kind: str = "", file_id: str = "") -> bool:
    return bool((text or "").strip() or (kind or "").strip() or (file_id or "").strip())


def safe_filename(name: str, fallback: str) -> str:
    cleaned = _UNSAFE_NAME.sub("_", (name or "").strip()).strip("._")
    return (cleaned[:80] or fallback)


def media_destination(root: Path, chat_id: int, message_id: int, filename: str, kind: str) -> Path:
    fallback = "photo.jpg" if (kind or "").casefold() == "photo" else "file.bin"
    return root / str(chat_id) / f"{message_id}_{safe_filename(filename, fallback)}"


def media_file_ready(path: str | Path | None) -> bool:
    if not path:
        return False
    candidate = Path(path)
    return candidate.is_file() and candidate.stat().st_size > 0


def delete_media_file(path: str | Path | None) -> None:
    if not path:
        return
    candidate = Path(path)
    try:
        candidate.unlink(missing_ok=True)
    except OSError:
        return
    parent = candidate.parent
    try:
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        return


def purge_expired_media(root: Path, ttl_seconds: int = DEFAULT_MEDIA_TTL_SECONDS, *, now: float | None = None) -> int:
    """Удаляет локальные копии старше ttl. Метаданные и file_id в SQLite не трогает."""
    if ttl_seconds <= 0 or not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - ttl_seconds
    removed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                delete_media_file(path)
                removed += 1
        except OSError:
            continue
    return removed
