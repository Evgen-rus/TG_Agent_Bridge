from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

import yaml

from ..knowledge import DEFAULT_KNOWLEDGE_PACK, parse_knowledge_pack


@dataclass(frozen=True)
class ChatConfig:
    telegram_chat_id: int
    name: str
    agent_provider: str
    wiki: str
    directory: Path
    memory_project: str | None = None
    knowledge_pack: str | None = None


_CYRILLIC = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
        "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
        "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
        "я": "ya",
    }
)


def slugify_chat_name(name: str, telegram_chat_id: int) -> str:
    translit = name.strip().casefold().translate(_CYRILLIC)
    slug = re.sub(r"[^a-z0-9]+", "_", translit).strip("_")
    return (slug or f"chat_{abs(telegram_chat_id)}")[:60]


def write_new_chat(
    chats_dir: Path,
    *,
    telegram_chat_id: int,
    name: str,
    wiki: str,
    directory_name: str = "",
) -> ChatConfig:
    chats_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify_chat_name(directory_name or name, telegram_chat_id)
    directory = chats_dir / slug
    if directory.exists():
        directory = chats_dir / f"{slug}_{abs(telegram_chat_id)}"
    directory.mkdir(parents=False, exist_ok=False)
    config_path = directory / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "name": name.strip(),
                "telegram_chat_id": telegram_chat_id,
                "agent_provider": "codex",
                "knowledge_pack": DEFAULT_KNOWLEDGE_PACK,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    wiki_text = wiki.strip() + "\n"
    (directory / "wiki.md").write_text(wiki_text, encoding="utf-8")
    return ChatConfig(
        telegram_chat_id=telegram_chat_id,
        name=name.strip(),
        agent_provider="codex",
        wiki=wiki_text.strip(),
        directory=directory,
        knowledge_pack=DEFAULT_KNOWLEDGE_PACK,
    )


class ChatRegistry:
    def __init__(self, chats: dict[int, ChatConfig]):
        self._chats = chats

    @classmethod
    def load(cls, chats_dir: Path) -> "ChatRegistry":
        if not chats_dir.is_dir():
            raise ValueError(f"Chats directory does not exist: {chats_dir}")

        chats: dict[int, ChatConfig] = {}
        for config_path in sorted(chats_dir.glob("*/config.yaml")):
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            try:
                chat_id = int(raw["telegram_chat_id"])
                name = str(raw["name"]).strip()
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid chat config: {config_path}") from exc
            if not name:
                raise ValueError(f"Chat name is empty: {config_path}")
            provider = str(raw.get("agent_provider", "codex")).strip().lower()
            if provider != "codex":
                raise ValueError(f"Unsupported agent provider {provider!r}: {config_path}")
            wiki_path = config_path.parent / "wiki.md"
            if not wiki_path.is_file():
                raise ValueError(f"Missing wiki.md for chat: {config_path.parent}")
            if chat_id in chats:
                raise ValueError(f"Duplicate telegram_chat_id: {chat_id}")
            chats[chat_id] = ChatConfig(
                telegram_chat_id=chat_id,
                name=name,
                agent_provider=provider,
                wiki=wiki_path.read_text(encoding="utf-8").strip(),
                directory=config_path.parent,
                memory_project=str(raw.get("memory_project", "")).strip() or None,
                knowledge_pack=parse_knowledge_pack(raw.get("knowledge_pack", DEFAULT_KNOWLEDGE_PACK)),
            )
        if not chats:
            raise ValueError(f"No chat configurations found in: {chats_dir}")
        return cls(chats)

    def add(self, chat: ChatConfig) -> ChatConfig:
        existing = self._chats.get(chat.telegram_chat_id)
        if existing is not None:
            return existing
        self._chats[chat.telegram_chat_id] = chat
        return chat

    def get(self, telegram_chat_id: int) -> ChatConfig | None:
        return self._chats.get(telegram_chat_id)

    def all_chats(self) -> list[ChatConfig]:
        return list(self._chats.values())

    def known_ids(self) -> list[int]:
        return list(self._chats)

    def find_by_name(self, text: str) -> ChatConfig | None:
        needle = " ".join(text.strip().casefold().split())
        if not needle:
            return None
        matches: list[ChatConfig] = []
        for chat in self._chats.values():
            name = chat.name.casefold()
            slug = chat.directory.name.casefold()
            slug_spaced = slug.replace("_", " ")
            if name in needle or slug in needle or slug_spaced in needle:
                matches.append(chat)
                continue
            if len(needle) >= 3 and (needle in name or needle in slug or needle in slug_spaced):
                matches.append(chat)
        unique = {chat.telegram_chat_id: chat for chat in matches}
        return next(iter(unique.values())) if len(unique) == 1 else None

    def __len__(self) -> int:
        return len(self._chats)
