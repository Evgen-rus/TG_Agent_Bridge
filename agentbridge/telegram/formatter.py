"""Plain-text messages delivered by AgentBridge to its owner."""

from __future__ import annotations


def format_owner_message(suggestion: "Suggestion") -> str:
    """Return a compact literal-text notification for the owner."""
    return (
        "Новый запрос\n\n"
        f"Чат: {suggestion.chat_name}\n"
        f"{suggestion.sender_name}:\n{suggestion.original_message}\n\n"
        f"Ситуация:\n{suggestion.situation}\n\n"
        f"Предлагаемый ответ:\n{suggestion.suggested_reply}"
    )


from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agentbridge.application import Suggestion
