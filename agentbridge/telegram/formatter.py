"""Owner-facing literal-text formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentbridge.agents.base import AgentAction
from agentbridge.application import action_label


def format_owner_message(suggestion: "Suggestion") -> str:
    action = suggestion.action or AgentAction.REPLY
    parts = [
        f"Чат: {suggestion.chat_name}",
        f"Новый контекст:\n{suggestion.sender_name}:\n{suggestion.original_message}",
        f"Ситуация:\n{suggestion.situation}",
    ]
    if suggestion.observation.strip():
        parts.append(f"Наблюдение:\n{suggestion.observation.strip()}")
    parts.append(f"Рекомендуемое действие: {action_label(action)}")
    if action == AgentAction.ASK_OWNER and suggestion.owner_question.strip():
        parts.append(f"Вопрос нам:\n{suggestion.owner_question.strip()}")
    if suggestion.unknowns.strip() and action != AgentAction.ASK_OWNER:
        parts.append(f"Чего не хватает:\n{suggestion.unknowns.strip()}")
    if action == AgentAction.REPLY and suggestion.suggested_reply.strip():
        parts.append(f"Предлагаемый ответ:\n{suggestion.suggested_reply}")
    return "\n\n".join(parts) + "\n\n"


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


def format_memory_proposal(proposal: "MemoryProposal") -> str:
    scope = {
        "chat": f"только для чата «{proposal.chat_name}»",
        "project": "для связанного проекта",
        "global": "для всех подключённых чатов",
    }[proposal.scope]
    return (
        f"Контекст для сохранения:\n{proposal.content}\n\n"
        f"Область: {scope}\n\n"
        "Сохранить?"
    )


def format_onboarding_notice(notice: "OnboardingNotice") -> str:
    added = notice.added_by_name.strip() or "неизвестно кто"
    return (
        f"Новая группа без wiki:\n{notice.chat_title}\n"
        f"ID: {notice.telegram_chat_id}\n"
        f"Добавил/права: {added}\n\n"
        "Бот уже админ, но этого чата ещё нет в AgentBridge. "
        "Сообщения копятся и пока не анализируются.\n\n"
        "Ответьте на это сообщение: кто это за клиент и какой контекст нужен."
    )


def format_onboarding_draft(proposal: "OnboardingDraftProposal") -> str:
    return (
        f"Черновик wiki для «{proposal.name}»\n"
        f"Группа: {proposal.chat_title}\n"
        f"ID: {proposal.telegram_chat_id}\n\n"
        f"{proposal.wiki.strip()}\n\n"
        "Сохранить этот чат и начать следить?"
    )


def format_rules(rules: list["RuleRecord"]) -> str:
    if not rules:
        return "Активных подтверждённых правил пока нет."
    lines = ["Активные правила:"]
    for rule in rules:
        scope = "все клиенты" if rule.scope == "global" else rule.chat_name
        lines.append(f"\n#{rule.id} · {scope}\n{rule.rule_text}\nАвтор: {rule.author_name}")
    return "\n".join(lines)


if TYPE_CHECKING:
    from agentbridge.application import LearningProposal, MemoryProposal, OnboardingDraftProposal, OnboardingNotice, Suggestion
    from agentbridge.storage.sqlite import RuleRecord
