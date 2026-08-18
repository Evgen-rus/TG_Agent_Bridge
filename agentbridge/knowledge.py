from __future__ import annotations

from pathlib import Path

CORE_FILENAME = "core.md"
DEFAULT_KNOWLEDGE_PACK = "leadgenbureau"
_DISABLED_PACKS = frozenset({"", "none", "off", "false", "0"})
_SKIP_INDEX = frozenset({"core.md", "readme.md"})
_INTERNAL_DOCS = frozenset({"cases.md", "needs_review.md"})


def parse_knowledge_pack(raw: object | None, *, default: str | None = DEFAULT_KNOWLEDGE_PACK) -> str | None:
    """Return a pack name, or None when the chat opts out."""
    if raw is None:
        return default
    value = str(raw).strip()
    if value.casefold() in _DISABLED_PACKS:
        return None
    return value


def load_knowledge_pack(knowledge_dir: Path | None, pack_name: str | None) -> str:
    """Load compact core plus an on-demand file index. Missing files stay empty."""
    if knowledge_dir is None or not pack_name:
        return ""
    pack_dir = knowledge_dir / pack_name
    if not pack_dir.is_dir():
        return ""
    core_path = pack_dir / CORE_FILENAME
    core = core_path.read_text(encoding="utf-8").strip() if core_path.is_file() else ""
    extras = _extra_doc_lines(knowledge_dir, pack_dir, pack_name)
    parts: list[str] = []
    if core:
        parts.append(f"Общие знания {pack_name}:\n{core}")
    if extras:
        parts.append(
            "Дополнительные документы пакета — читай файл только если он нужен "
            "для текущего хода, не загружай всю базу:\n" + "\n".join(extras)
        )
    return "\n\n".join(parts)


def _extra_doc_lines(knowledge_dir: Path, pack_dir: Path, pack_name: str) -> list[str]:
    root = knowledge_dir.resolve()
    project_root = root.parent
    lines: list[str] = []
    for path in sorted(pack_dir.glob("*.md")):
        name = path.name
        if name.casefold() in _SKIP_INDEX:
            continue
        try:
            rel = path.resolve().relative_to(project_root).as_posix()
        except ValueError:
            rel = f"{knowledge_dir.name}/{pack_name}/{name}"
        audience = "internal" if name.casefold() in _INTERNAL_DOCS else "shared"
        lines.append(f"- {rel} [{audience}]")
    return lines
