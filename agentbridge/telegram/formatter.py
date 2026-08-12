"""Owner-facing literal-text formatting."""

from __future__ import annotations


def format_owner_message(suggestion: "Suggestion") -> str:
    return (
        "Новый запрос\n\n"
        f"Чат: {suggestion.chat_name}\n"
        f"{suggestion.sender_name}:\n{suggestion.original_message}\n\n"
        f"Ситуация:\n{suggestion.situation}\n\n"
        f"Предлагаемый ответ:\n{suggestion.suggested_reply}\n\n"
        "Чтобы скорректировать или обучить помощника, ответьте на это сообщение."
    )


def format_learning_proposal(proposal: "LearningProposal") -> str:
    scope = "для всех клиентов" if proposal.scope == "global" else f"только для клиента «{proposal.chat_name}»"
    rule = proposal.proposed_rule or "Постоянное правило не сохранять."
    revision = "да" if proposal.regenerate_current else "нет"
    return (
        f"Клиент: {proposal.chat_name}\n\n"
        f"Я понял так:\n{proposal.understanding}\n\n"
        f"Правило:\n{rule}\n\n"
        f"Область: {scope}\n"
        f"Переделать текущую рекомендацию: {revision}\n\n"
        "Всё верно?"
    )


def format_rules(rules: list["RuleRecord"]) -> str:
    if not rules:
        return "Активных подтверждённых правил пока нет."
    lines = ["Активные правила:"]
    for rule in rules:
        scope = "все клиенты" if rule.scope == "global" else rule.chat_name
        lines.append(f"\n#{rule.id} · {scope}\n{rule.rule_text}\nАвтор: {rule.author_name}")
    return "\n".join(lines)


from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from agentbridge.application import LearningProposal, Suggestion
    from agentbridge.storage.sqlite import RuleRecord
