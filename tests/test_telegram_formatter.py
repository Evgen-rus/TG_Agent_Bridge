from __future__ import annotations

from agentbridge.application import Suggestion
from agentbridge.telegram.formatter import format_owner_message


def test_owner_message_contains_a_compact_copyable_suggestion() -> None:
    suggestion = Suggestion(
        chat_name="Acme Support",
        sender_name="Alice",
        original_message="Can I get the docs?",
        situation="Alice asks for the documentation link.",
        suggested_reply="Yes. Here is the documentation: …",
    )

    text = format_owner_message(suggestion)

    assert "\u0427\u0430\u0442: Acme Support" in text
    assert "Alice:" in text
    assert "Can I get the docs?" in text
    assert "\u0421\u0438\u0442\u0443\u0430\u0446\u0438\u044f:" in text
    assert "Alice asks for the documentation link." in text
    assert "\u041f\u0440\u0435\u0434\u043b\u0430\u0433\u0430\u0435\u043c\u044b\u0439 \u043e\u0442\u0432\u0435\u0442:" in text
    assert "Yes. Here is the documentation: …" in text
