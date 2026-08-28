from __future__ import annotations

import pytest

from agentbridge.application import Suggestion
from agentbridge.telegram.formatter import format_owner_message, split_owner_message


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


@pytest.mark.parametrize("text", ["Короткий ответ", "я" * 4096, "😀" * 2048], ids=["short", "limit", "emoji-limit"])
def test_short_text_is_unchanged(text) -> None:
    assert split_owner_message(text) == [text]


@pytest.mark.parametrize("text", [
    "я" * 4097, "😀" * 4096, "Строка с пробелами.\n\n" * 700,
    "БезПробелов" * 7000, "\n" * 9000, "Кириллица 👩‍💻 и emoji 🚀\n" * 500,
], ids=["over-limit", "emoji", "paragraphs", "unbroken", "newlines", "mixed-unicode"])
def test_long_text_fits_with_headers_and_preserves_all_content(text) -> None:
    parts = split_owner_message(text)
    assert len(parts) > 1
    assert all(len(part.encode("utf-16-le")) // 2 <= 4096 for part in parts)
    assert "".join(part.split("\n\n", 1)[1] for part in parts) == text
    for index, part in enumerate(parts, 1):
        assert part.startswith(f"Часть {index}/{len(parts)}\n\n")


def test_split_prefers_paragraph_boundary() -> None:
    paragraph = "а" * 3000 + "\n\n"
    parts = split_owner_message(paragraph + "б" * 3000)
    assert parts[0].split("\n\n", 1)[1] == paragraph
