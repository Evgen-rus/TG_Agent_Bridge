from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re

from .agents.base import AgentProvider, FeedbackAnalysis
from .chats.loader import ChatRegistry
from .storage.sqlite import ChatThreadStore, LearningDraft, RuleRecord

logger = logging.getLogger(__name__)
_GLOBAL_WORDING = re.compile(r"\b(для\s+всех|всем\s+клиент|глобальн)", re.IGNORECASE)


@dataclass(frozen=True)
class Suggestion:
    chat_name: str
    sender_name: str
    original_message: str
    situation: str
    suggested_reply: str
    recommendation_id: int = 0
    telegram_chat_id: int = 0


@dataclass(frozen=True)
class IncomingMessage:
    sender_name: str
    text: str
    update_id: int | None = None


@dataclass(frozen=True)
class LearningProposal:
    draft_id: int
    chat_name: str
    understanding: str
    proposed_rule: str | None
    scope: str
    regenerate_current: bool


@dataclass(frozen=True)
class LearningResult:
    chat_name: str
    rule_saved: bool
    revised_suggestion: Suggestion | None
    current_suppressed: bool = False


class AgentBridgeApplication:
    def __init__(
        self,
        registry: ChatRegistry,
        store: ChatThreadStore,
        provider: AgentProvider,
        owner_chat_id: int | None = None,
    ):
        self.registry = registry
        self.store = store
        self.provider = provider
        self.owner_chat_id = owner_chat_id
        self._chat_locks: dict[int, asyncio.Lock] = {}

    async def handle_message(self, telegram_chat_id: int, sender_name: str, message: str, update_id: int | None = None) -> Suggestion | None:
        return await self.handle_messages(telegram_chat_id, [IncomingMessage(sender_name, message, update_id)])

    async def handle_messages(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> Suggestion | None:
        chat = self.registry.get(telegram_chat_id)
        if chat is None:
            logger.info("event=client_batch_ignored reason=unknown_chat chat_id=%s", telegram_chat_id)
            return None
        lock = self._chat_locks.setdefault(telegram_chat_id, asyncio.Lock())
        async with lock:
            pending = [item for item in messages if item.text.strip() and (item.update_id is None or not self.store.is_update_processed(item.update_id))]
            if not pending:
                logger.info("event=client_batch_ignored reason=empty_or_duplicate chat_id=%s", telegram_chat_id)
                return None
            sender_names = list(dict.fromkeys(item.sender_name.strip() or "Неизвестный отправитель" for item in pending))
            sender_name = sender_names[0] if len(sender_names) == 1 else ", ".join(sender_names)
            combined_message = pending[0].text.strip() if len(pending) == 1 else "\n".join(
                f"{item.sender_name.strip() or 'Неизвестный отправитель'}: {item.text.strip()}" for item in pending
            )
            rules = self.store.active_rule_texts(telegram_chat_id)
            thread_id = self.store.get_thread_id(telegram_chat_id)
            logger.info("event=codex_suggest_start chat_id=%s chat=%r batch_size=%d rule_count=%d thread=%s", telegram_chat_id, chat.name, len(pending), len(rules), "resume" if thread_id else "new")
            reply = await self.provider.suggest(message=combined_message, sender_name=sender_name, chat_name=chat.name, wiki=chat.wiki, rules=rules, thread_id=thread_id)
            self.store.save_thread(chat.telegram_chat_id, chat.name, reply.thread_id, chat.agent_provider)
            for item in pending:
                if item.update_id is not None:
                    self.store.mark_update_processed(item.update_id)
            if not reply.should_notify:
                logger.info("event=codex_suggestion_suppressed chat_id=%s rule_count=%d", telegram_chat_id, len(rules))
                return None
            recommendation_id = self.store.create_recommendation(
                telegram_chat_id, chat.name, sender_name, combined_message, reply.situation,
                reply.suggested_reply, self.owner_chat_id,
            )
            logger.info("event=codex_suggest_done chat_id=%s recommendation_id=%s", telegram_chat_id, recommendation_id)
        return Suggestion(chat.name, sender_name, combined_message, reply.situation, reply.suggested_reply, recommendation_id, telegram_chat_id)

    def record_owner_delivery(self, recommendation_id: int, owner_chat_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_message(recommendation_id, owner_chat_id, owner_message_id)
        logger.info("event=owner_delivery_linked recommendation_id=%s owner_message_id=%s", recommendation_id, owner_message_id)

    def pending_suggestions(self, owner_chat_id: int) -> list[Suggestion]:
        self.store.assign_unowned_pending_recommendations(owner_chat_id)
        return [
            Suggestion(
                chat_name=record.chat_name,
                sender_name=record.sender_name,
                original_message=record.original_message,
                situation=record.situation,
                suggested_reply=record.suggested_reply,
                recommendation_id=record.id,
                telegram_chat_id=record.telegram_chat_id,
            )
            for record in self.store.pending_recommendations(owner_chat_id)
        ]

    def is_owner_delivery_linked(self, recommendation_id: int, owner_chat_id: int) -> bool:
        record = self.store.get_recommendation(recommendation_id)
        return bool(
            record is not None
            and record.owner_chat_id == owner_chat_id
            and record.owner_message_id is not None
        )

    async def handle_owner_feedback(self, owner_chat_id: int, reply_to_message_id: int, author_user_id: int, author_name: str, feedback: str, update_id: int | None = None) -> LearningProposal | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        recommendation = self.store.get_recommendation_by_owner_message(owner_chat_id, reply_to_message_id)
        if recommendation is None:
            return None
        rules = self.store.active_rule_texts(recommendation.telegram_chat_id)
        logger.info("event=feedback_analysis_start recommendation_id=%s author_id=%s", recommendation.id, author_user_id)
        analysis = await self.provider.analyze_feedback(
            feedback=feedback, chat_name=recommendation.chat_name, original_message=recommendation.original_message,
            situation=recommendation.situation, suggested_reply=recommendation.suggested_reply, rules=rules,
        )
        if analysis.scope == "global" and not _GLOBAL_WORDING.search(feedback):
            analysis = FeedbackAnalysis(
                analysis.understanding, analysis.proposed_rule, analysis.conflict_key, "client",
                analysis.regenerate_current, analysis.revision_instruction,
            )
        draft = self.store.create_learning_draft(recommendation.id, author_user_id, author_name, feedback, analysis)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        logger.info("event=feedback_analysis_done draft_id=%s recommendation_id=%s scope=%s regenerate=%s", draft.id, recommendation.id, draft.scope, draft.regenerate_current)
        return self._proposal(draft, recommendation.chat_name)

    async def clarify_feedback(self, prompt_message_id: int, feedback: str, update_id: int | None = None) -> LearningProposal | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        draft = self.store.get_draft_by_clarification_prompt(prompt_message_id)
        if draft is None:
            return None
        recommendation = self.store.get_recommendation(draft.recommendation_id)
        if recommendation is None:
            return None
        combined_feedback = f"Первоначальное замечание:\n{draft.feedback}\n\nУточнение владельца:\n{feedback}"
        analysis = await self.provider.analyze_feedback(
            feedback=combined_feedback, chat_name=recommendation.chat_name, original_message=recommendation.original_message,
            situation=recommendation.situation, suggested_reply=recommendation.suggested_reply,
            rules=self.store.active_rule_texts(recommendation.telegram_chat_id),
        )
        if analysis.scope == "global" and not _GLOBAL_WORDING.search(combined_feedback):
            analysis = FeedbackAnalysis(analysis.understanding, analysis.proposed_rule, analysis.conflict_key, "client", analysis.regenerate_current, analysis.revision_instruction)
        updated = self.store.replace_learning_draft_analysis(draft.id, combined_feedback, analysis)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        logger.info("event=feedback_clarified draft_id=%s", draft.id)
        return self._proposal(updated, recommendation.chat_name)

    def mark_awaiting_clarification(self, draft_id: int, prompt_message_id: int) -> None:
        self.store.mark_draft_awaiting_clarification(draft_id, prompt_message_id)

    async def confirm_learning(self, draft_id: int) -> LearningResult | None:
        draft = self.store.get_learning_draft(draft_id)
        if draft is None or draft.status != "pending":
            return None
        recommendation = self.store.get_recommendation(draft.recommendation_id)
        if recommendation is None:
            return None
        if not self.store.confirm_draft(draft_id):
            return None
        logger.info("event=learning_confirmed draft_id=%s recommendation_id=%s rule_saved=%s", draft_id, recommendation.id, bool(draft.proposed_rule))
        revised = None
        current_suppressed = False
        if draft.regenerate_current:
            chat = self.registry.get(recommendation.telegram_chat_id)
            thread_id = self.store.get_thread_id(recommendation.telegram_chat_id)
            if chat is not None and thread_id is not None:
                rules = self.store.active_rule_texts(recommendation.telegram_chat_id)
                reply = await self.provider.revise(
                    feedback=draft.revision_instruction or draft.feedback,
                    message=recommendation.original_message, sender_name=recommendation.sender_name,
                    chat_name=chat.name, wiki=chat.wiki, rules=rules, thread_id=thread_id,
                )
                self.store.save_thread(chat.telegram_chat_id, chat.name, reply.thread_id, chat.agent_provider)
                if reply.should_notify:
                    new_id = self.store.create_recommendation(
                        chat.telegram_chat_id, chat.name, recommendation.sender_name,
                        recommendation.original_message, reply.situation, reply.suggested_reply,
                        self.owner_chat_id,
                    )
                    revised = Suggestion(chat.name, recommendation.sender_name, recommendation.original_message, reply.situation, reply.suggested_reply, new_id, chat.telegram_chat_id)
                    logger.info("event=recommendation_revised draft_id=%s recommendation_id=%s", draft_id, new_id)
                else:
                    current_suppressed = True
                    logger.info("event=recommendation_revision_suppressed draft_id=%s", draft_id)
        return LearningResult(recommendation.chat_name, bool(draft.proposed_rule), revised, current_suppressed)

    def list_rules(self) -> list[RuleRecord]:
        return self.store.list_active_rules()

    def undo_latest_rule(self) -> RuleRecord | None:
        rule = self.store.undo_latest_rule()
        if rule:
            logger.info("event=learning_rule_undone rule_id=%s scope=%s", rule.id, rule.scope)
        return rule

    @staticmethod
    def _proposal(draft: LearningDraft, chat_name: str) -> LearningProposal:
        return LearningProposal(draft.id, chat_name, draft.understanding, draft.proposed_rule, draft.scope, draft.regenerate_current)
