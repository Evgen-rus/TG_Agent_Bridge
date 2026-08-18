from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ChatConfig:
    telegram_chat_id: int
    name: str
    agent_provider: str
    wiki: str
    directory: Path
    memory_project: str | None = None


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
            )
        if not chats:
            raise ValueError(f"No chat configurations found in: {chats_dir}")
        return cls(chats)

    def get(self, telegram_chat_id: int) -> ChatConfig | None:
        return self._chats.get(telegram_chat_id)

    def __len__(self) -> int:
        return len(self._chats)
