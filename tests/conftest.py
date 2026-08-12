from __future__ import annotations

from pathlib import Path

import pytest

from agentbridge.chats.loader import ChatConfig, ChatRegistry


@pytest.fixture
def chat_registry() -> ChatRegistry:
    chat = ChatConfig(
        telegram_chat_id=-100_123_456,
        name="Acme Support",
        agent_provider="codex",
        wiki="Acme uses the Enterprise plan.",
        directory=Path("chats/acme_support"),
    )
    return ChatRegistry({chat.telegram_chat_id: chat})
