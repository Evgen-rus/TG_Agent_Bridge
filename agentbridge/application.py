from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
import re

from .agents.base import AgentAction, AgentProvider, FeedbackAnalysis
from .chats.loader import ChatConfig, ChatRegistry
from .storage.sqlite import ChatThreadStore, DEFAULT_CHAT_STATE, LearningDraft, RuleRecord, StoredMessage

logger = logging.getLogger(__name__)
_GLOBAL_WORDING = re.compile(r"\b(для\s+всех|всем\s+клиент|глобальн)", re.IGNORECASE)
_INTERNAL_PARTICIPANTS = frozenset({"евгений расюк", "евгений росюк", "дмитрий смагин"})
_MEMORY_PREFIXES = (
    ("общий контекст:", "global"),
    ("запомни для всех чатов:", "global"),
    ("контекст проекта:", "project"),
    ("запомни для проекта:", "project"),
    ("контекст:", "chat"),
    ("запомни для этого чата:", "chat"),
)
_ACTION_LABELS = {
    AgentAction.REPLY: "ответить клиенту",
    AgentAction.ASK_OWNER: "спросить нас",
    AgentAction.OBSERVE: "просто заметить",
    AgentAction.NO_ACTION: "ничего не делать",
}


@dataclass(frozen=True)
class Suggestion:
    chat_name: str
    sender_name: str
    original_message: str
    situation: str
    suggested_reply: str
    recommendation_id: int = 0
    telegram_chat_id: int = 0
    action: str = AgentAction.REPLY
    observation: str = ""
    unknowns: str = ""
    owner_question: str = ""


@dataclass(frozen=True)
class IncomingMessage:
    sender_name: str
    text: str
    update_id: int | None = None
    message_id: int | None = None
    sender_id: int | None = None
    telegram_date: str | None = None
    reply_to_message_id: int | None = None


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


@dataclass(frozen=True)
class MemoryProposal:
    draft_id: int
    chat_name: str
    content: str
    scope: str


@dataclass(frozen=True)
class QuestionReplyResult:
    suggestion: Suggestion | None
    memory_proposal: MemoryProposal | None


class AgentBridgeApplication:
    def __init__(
        self,
        registry: ChatRegistry,
        store: ChatThreadStore,
        provider: AgentProvider,
        owner_chat_id: int | None = None,
        episode_size: int = 40,
    ):
        self.registry = registry
        self.store = store
        self.provider = provider
        self.owner_chat_id = owner_chat_id
        self.episode_size = max(1, episode_size)
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def ingest_telegram_message(
        self,
        *,
        update_id: int,
        chat_id: int,
        message_id: int,
        sender_id: int | None,
        sender_name: str,
        telegram_date: str,
        text: str,
        reply_to_message_id: int | None,
        is_owner_chat: bool,
    ) -> bool:
        if is_owner_chat:
            role = "owner"
            status = "ignored"
        else:
            chat = self.registry.get(chat_id)
            if chat is None:
                return False
            role = "internal" if sender_name.strip().casefold() in _INTERNAL_PARTICIPANTS else "client"
            status = "pending"
        return self.store.ingest_telegram_message(
            update_id=update_id,
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name.strip() or "Неизвестный отправитель",
            telegram_date=telegram_date,
            text=text,
            reply_to_message_id=reply_to_message_id,
            role=role,
            processing_status=status,
        )

    def pending_client_chat_ids(self) -> list[int]:
        return self.store.pending_chat_ids(self.registry.known_ids())

    async def catch_up(self) -> list[Suggestion]:
        self.store.reset_stale_processing()
        suggestions: list[Suggestion] = []
        for chat_id in self.store.pending_chat_ids(self.registry.known_ids()):
            result = await self.process_pending_chat(chat_id, mode="catchup")
            if result is not None:
                suggestions.append(result)
        return suggestions

    async def process_pending_chat(self, telegram_chat_id: int, *, mode: str = "live") -> Suggestion | None:
        chat = self.registry.get(telegram_chat_id)
        if chat is None:
            return None
        lock = self._chat_locks.setdefault(telegram_chat_id, asyncio.Lock())
        last: Suggestion | None = None
        async with lock:
            while True:
                pending = self.store.pending_messages(telegram_chat_id, limit=self.episode_size)
                if not pending:
                    break
                claimed = self.store.claim_messages([item.id for item in pending])
                if not claimed:
                    break
                remaining = self.store.pending_messages(telegram_chat_id, limit=1)
                notify = mode == "live" or not remaining
                try:
                    result = await self._run_episode(chat, [_from_stored(item) for item in claimed], notify=notify)
                    self.store.mark_messages_processed(claimed)
                except Exception:
                    self.store.release_messages([item.id for item in claimed])
                    raise
                if result is not None:
                    last = result
        return last

    async def handle_message(self, telegram_chat_id: int, sender_name: str, message: str, update_id: int | None = None) -> Suggestion | None:
        return await self.handle_messages(telegram_chat_id, [IncomingMessage(sender_name, message, update_id)])

    async def handle_messages(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> Suggestion | None:
        chat = self.registry.get(telegram_chat_id)
        if chat is None:
            logger.info("event=client_batch_ignored reason=unknown_chat chat_id=%s", telegram_chat_id)
            return None
        lock = self._chat_locks.setdefault(telegram_chat_id, asyncio.Lock())
        async with lock:
            for item in messages:
                self._ingest_incoming(telegram_chat_id, item)
            pending = [
                item for item in messages
                if item.text.strip() and (item.update_id is None or not self.store.is_update_processed(item.update_id))
            ]
            if not pending:
                logger.info("event=client_batch_ignored reason=empty_or_duplicate chat_id=%s", telegram_chat_id)
                return None
            claimed = self._claim_incoming(telegram_chat_id, pending)
            try:
                result = await self._run_episode(chat, pending, notify=True)
                if claimed:
                    self.store.mark_messages_processed(claimed)
                else:
                    for item in pending:
                        if item.update_id is not None:
                            self.store.mark_update_processed(item.update_id)
            except Exception:
                if claimed:
                    self.store.release_messages([item.id for item in claimed])
                raise
            return result

    def _ingest_incoming(self, telegram_chat_id: int, item: IncomingMessage) -> None:
        if item.update_id is None or not item.text.strip():
            return
        role = "internal" if item.sender_name.strip().casefold() in _INTERNAL_PARTICIPANTS else "client"
        self.store.ingest_telegram_message(
            update_id=item.update_id,
            chat_id=telegram_chat_id,
            message_id=item.message_id if item.message_id is not None else item.update_id,
            sender_id=item.sender_id,
            sender_name=item.sender_name.strip() or "Неизвестный отправитель",
            telegram_date=item.telegram_date or "",
            text=item.text.strip(),
            reply_to_message_id=item.reply_to_message_id,
            role=role,
            processing_status="pending",
        )

    def _claim_incoming(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> list[StoredMessage]:
        update_ids = {item.update_id for item in messages if item.update_id is not None}
        if not update_ids:
            return []
        pending = [item for item in self.store.pending_messages(telegram_chat_id) if item.update_id in update_ids]
        return self.store.claim_messages([item.id for item in pending])

    async def _run_episode(self, chat: ChatConfig, messages: list[IncomingMessage], *, notify: bool) -> Suggestion | None:
        internal, external = self._split_internal_messages(messages)
        for item in internal:
            self.store.record_internal_context(
                chat.telegram_chat_id, chat.name, item.sender_name.strip() or "Внутренний участник", item.text.strip(),
            )
        if not external:
            logger.info("event=client_batch_context_only chat_id=%s count=%d", chat.telegram_chat_id, len(internal))
            return None
        sender_names = list(dict.fromkeys(item.sender_name.strip() or "Неизвестный отправитель" for item in external))
        sender_name = sender_names[0] if len(sender_names) == 1 else ", ".join(sender_names)
        if len(messages) == 1:
            combined_message = messages[0].text.strip()
        else:
            combined_message = "\n".join(
                f"{item.sender_name.strip() or 'Неизвестный отправитель'}: {item.text.strip()}" for item in messages
            )
        rules = self.store.active_rule_texts(chat.telegram_chat_id)
        thread_id = self.store.get_thread_id(chat.telegram_chat_id)
        context_pack = self._context_pack(chat, episode=combined_message)
        logger.info(
            "event=codex_suggest_start chat_id=%s chat=%r batch_size=%d rule_count=%d thread=%s notify=%s",
            chat.telegram_chat_id, chat.name, len(messages), len(rules), "resume" if thread_id else "new", notify,
        )
        suggest_kwargs = {
            "message": combined_message,
            "sender_name": sender_name,
            "chat_name": chat.name,
            "wiki": self._contextual_wiki(chat),
            "rules": rules,
            "thread_id": thread_id,
            "context_pack": context_pack,
        }
        reply = await self.provider.suggest(**suggest_kwargs)
        reply = await self._maybe_critique(reply, suggest_kwargs)
        self.store.save_thread(chat.telegram_chat_id, chat.name, reply.thread_id, chat.agent_provider)
        self._apply_state_update(chat.telegram_chat_id, reply.candidate_state, reply.situation)
        action = reply.resolved_action()
        if not notify or not reply.notifies_owner():
            logger.info("event=codex_suggestion_suppressed chat_id=%s action=%s notify=%s", chat.telegram_chat_id, action, notify)
            return None
        recommendation_id = self.store.create_recommendation(
            chat.telegram_chat_id, chat.name, sender_name, combined_message, reply.situation,
            reply.suggested_reply, self.owner_chat_id, action, reply.observation, reply.unknowns, reply.owner_question,
        )
        if action == AgentAction.ASK_OWNER:
            question = (reply.owner_question or reply.unknowns or reply.situation).strip()
            self.store.create_owner_question(chat.telegram_chat_id, question, recommendation_id)
        logger.info("event=codex_suggest_done chat_id=%s recommendation_id=%s action=%s", chat.telegram_chat_id, recommendation_id, action)
        return Suggestion(
            chat.name, sender_name, combined_message, reply.situation, reply.suggested_reply,
            recommendation_id, chat.telegram_chat_id, action, reply.observation, reply.unknowns, reply.owner_question,
        )

    async def _maybe_critique(self, reply, suggest_kwargs: dict) -> object:
        action = reply.resolved_action()
        low_confidence = reply.confidence is not None and reply.confidence < 0.4
        if not reply.needs_critique and not (low_confidence and action in {AgentAction.REPLY, AgentAction.ASK_OWNER}):
            return reply
        critic = getattr(self.provider, "critique", None)
        if critic is None:
            return reply
        logger.info("event=codex_critique_start action=%s confidence=%s", action, reply.confidence)
        return await critic(previous=reply, **suggest_kwargs)

    def _apply_state_update(self, telegram_chat_id: int, candidate_state: dict | None, situation: str) -> None:
        current = self.store.get_chat_state(telegram_chat_id)
        merged = dict(current)
        if isinstance(candidate_state, dict):
            for key, value in candidate_state.items():
                if key in DEFAULT_CHAT_STATE:
                    merged[key] = value
        if situation.strip() and not (isinstance(candidate_state, dict) and candidate_state.get("summary")):
            merged["summary"] = situation.strip()
        self.store.save_chat_state(telegram_chat_id, merged)

    def _contextual_wiki(self, chat: ChatConfig) -> str:
        sections = [chat.wiki]
        memories = self.store.active_memory_entries(chat.telegram_chat_id, chat.memory_project)
        if memories:
            sections.append(
                "Подтверждённая память:\n" + "\n".join(f"- [{item.kind}/{item.scope}] {item.content}" for item in memories)
            )
        internal = self.store.recent_internal_context(chat.telegram_chat_id)
        if internal:
            sections.append("Недавний внутренний контекст (это не сообщение клиента):\n" + "\n".join(f"- {item}" for item in internal))
        return "\n\n".join(sections)

    def _context_pack(self, chat: ChatConfig, *, episode: str = "") -> str:
        state = self.store.get_chat_state(chat.telegram_chat_id)
        recent = self.store.recent_messages(chat.telegram_chat_id)
        experience = self.store.recent_experience(chat.telegram_chat_id)
        parts = [
            f"Wiki:\n{chat.wiki or '(пусто)'}",
            "Текущее состояние чата:\n" + json.dumps(state, ensure_ascii=False, indent=2),
        ]
        if recent:
            history = "\n".join(f"{item.sender_name}: {item.text}" for item in recent)
            parts.append("Недавняя история:\n" + history)
        if episode:
            parts.append("Текущий эпизод:\n" + episode)
        memories = self.store.active_memory_entries(chat.telegram_chat_id, chat.memory_project)
        if memories:
            parts.append(
                "Подтверждённая память:\n" + "\n".join(f"- [{item.kind}/{item.scope}] {item.content}" for item in memories)
            )
        rules = self.store.active_rule_texts(chat.telegram_chat_id)
        parts.append("Правила:\n" + ("\n".join(f"- {rule}" for rule in rules) or "(нет)"))
        if experience:
            parts.append("Похожий подтверждённый опыт:\n" + "\n".join(f"- {item}" for item in experience))
        return "\n\n".join(parts)

    def record_owner_delivery(self, recommendation_id: int, owner_chat_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_message(recommendation_id, owner_chat_id, owner_message_id)
        logger.info("event=owner_delivery_linked recommendation_id=%s owner_message_id=%s", recommendation_id, owner_message_id)

    def pending_suggestions(self, owner_chat_id: int) -> list[Suggestion]:
        self.store.assign_unowned_pending_recommendations(owner_chat_id)
        return [self._suggestion_from_record(record) for record in self.store.pending_recommendations(owner_chat_id)]

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

    @staticmethod
    def is_memory_context_command(text: str) -> bool:
        lowered = text.strip().casefold()
        return any(lowered.startswith(prefix) for prefix, _ in _MEMORY_PREFIXES)

    async def handle_owner_context(
        self, owner_chat_id: int, reply_to_message_id: int, author_user_id: int,
        author_name: str, text: str, update_id: int | None = None,
    ) -> MemoryProposal | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        command = self._parse_memory_command(text)
        if command is None:
            return None
        scope, content = command
        recommendation = self.store.get_recommendation_by_owner_message(owner_chat_id, reply_to_message_id)
        if recommendation is None:
            return None
        chat = self.registry.get(recommendation.telegram_chat_id)
        project_key = chat.memory_project if chat is not None else None
        if scope == "project" and not project_key:
            return None
        draft = self.store.create_memory_draft(
            recommendation.id, author_user_id, author_name, content, scope, project_key if scope == "project" else None,
        )
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return MemoryProposal(draft.id, recommendation.chat_name, draft.content, draft.scope)

    @staticmethod
    def _parse_memory_command(text: str) -> tuple[str, str] | None:
        lowered = text.strip().casefold()
        for prefix, scope in _MEMORY_PREFIXES:
            if lowered.startswith(prefix):
                content = text.strip()[len(prefix):].strip()
                return (scope, content) if content else None
        return None

    def confirm_memory(self, draft_id: int) -> MemoryProposal | None:
        draft = self.store.confirm_memory_draft(draft_id)
        if draft is None:
            return None
        recommendation = self.store.get_recommendation(draft.recommendation_id)
        return None if recommendation is None else MemoryProposal(draft.id, recommendation.chat_name, draft.content, draft.scope)

    def reject_memory(self, draft_id: int) -> bool:
        return self.store.reject_memory_draft(draft_id)

    async def handle_owner_query(
        self, text: str, *, reply_to_message_id: int | None = None, update_id: int | None = None,
    ) -> str | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        chat = None
        if reply_to_message_id is not None and self.owner_chat_id is not None:
            recommendation = self.store.get_recommendation_by_owner_message(self.owner_chat_id, reply_to_message_id)
            if recommendation is not None:
                chat = self.registry.get(recommendation.telegram_chat_id)
        if chat is None:
            chat = self.registry.find_by_name(text)
        if chat is None and len(self.registry) == 1:
            chat = self.registry.all_chats()[0]
        if chat is None:
            names = ", ".join(item.name for item in self.registry.all_chats()) or "нет подключённых чатов"
            return f"Уточните, о каком чате речь. Сейчас подключены: {names}."
        answerer = getattr(self.provider, "answer_owner_query", None)
        pack = self._context_pack(chat)
        if answerer is not None:
            answer = await answerer(question=text, chat_name=chat.name, context_pack=pack)
        else:
            answer = self._local_status(chat)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return answer

    def _local_status(self, chat: ChatConfig) -> str:
        state = self.store.get_chat_state(chat.telegram_chat_id)
        summary = str(state.get("summary") or "Пока нет сжатого состояния.").strip()
        next_step = str(state.get("next_step") or "").strip()
        waiting = state.get("waiting_from_us") or []
        lines = [f"Чат: {chat.name}", f"Сейчас: {summary}"]
        if waiting:
            lines.append("Ждём от нас: " + "; ".join(str(item) for item in waiting))
        if next_step:
            lines.append("Следующий шаг: " + next_step)
        return "\n".join(lines)

    async def handle_owner_question_reply(
        self, owner_chat_id: int, reply_to_message_id: int, author_user_id: int,
        author_name: str, answer: str, update_id: int | None = None,
    ) -> QuestionReplyResult | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        question = self.store.get_owner_question_by_message(reply_to_message_id)
        if question is None:
            return None
        chat = self.registry.get(question.telegram_chat_id)
        if chat is None:
            return None
        self.store.answer_owner_question(question.id)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        state = self.store.get_chat_state(chat.telegram_chat_id)
        facts = list(state.get("facts") or [])
        facts.append(f"Уточнение владельца: {answer.strip()}")
        state["facts"] = facts
        self.store.save_chat_state(chat.telegram_chat_id, state)
        memory_proposal = None
        if question.recommendation_id is not None:
            draft = self.store.create_memory_draft(
                question.recommendation_id, author_user_id, author_name, answer.strip(), "chat", None,
            )
            memory_proposal = MemoryProposal(draft.id, chat.name, draft.content, draft.scope)
        lock = self._chat_locks.setdefault(chat.telegram_chat_id, asyncio.Lock())
        async with lock:
            suggestion = await self._run_episode(
                chat,
                [IncomingMessage(author_name, f"Уточнение владельца по вопросу «{question.question}»: {answer.strip()}")],
                notify=True,
            )
        return QuestionReplyResult(suggestion, memory_proposal)

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
        lesson = draft.proposed_rule or draft.understanding
        if lesson:
            self.store.record_experience(
                telegram_chat_id=None if draft.scope == "global" else recommendation.telegram_chat_id,
                chat_name=recommendation.chat_name,
                situation=recommendation.situation,
                lesson=lesson,
                source_draft_id=draft.id,
            )
        logger.info("event=learning_confirmed draft_id=%s recommendation_id=%s rule_saved=%s", draft.id, recommendation.id, bool(draft.proposed_rule))
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
                    chat_name=chat.name, wiki=self._contextual_wiki(chat), rules=rules, thread_id=thread_id,
                )
                self.store.save_thread(chat.telegram_chat_id, chat.name, reply.thread_id, chat.agent_provider)
                self._apply_state_update(chat.telegram_chat_id, reply.candidate_state, reply.situation)
                if reply.notifies_owner():
                    new_id = self.store.create_recommendation(
                        chat.telegram_chat_id, chat.name, recommendation.sender_name,
                        recommendation.original_message, reply.situation, reply.suggested_reply,
                        self.owner_chat_id, reply.resolved_action(), reply.observation, reply.unknowns, reply.owner_question,
                    )
                    revised = Suggestion(
                        chat.name, recommendation.sender_name, recommendation.original_message, reply.situation,
                        reply.suggested_reply, new_id, chat.telegram_chat_id, reply.resolved_action(),
                        reply.observation, reply.unknowns, reply.owner_question,
                    )
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

    @staticmethod
    def _suggestion_from_record(record) -> Suggestion:
        return Suggestion(
            chat_name=record.chat_name,
            sender_name=record.sender_name,
            original_message=record.original_message,
            situation=record.situation,
            suggested_reply=record.suggested_reply,
            recommendation_id=record.id,
            telegram_chat_id=record.telegram_chat_id,
            action=getattr(record, "action", AgentAction.REPLY) or AgentAction.REPLY,
            observation=getattr(record, "observation", "") or "",
            unknowns=getattr(record, "unknowns", "") or "",
            owner_question=getattr(record, "owner_question", "") or "",
        )

    @staticmethod
    def _split_internal_messages(messages: list[IncomingMessage]) -> tuple[list[IncomingMessage], list[IncomingMessage]]:
        internal = [item for item in messages if item.sender_name.strip().casefold() in _INTERNAL_PARTICIPANTS]
        return internal, [item for item in messages if item not in internal]


def _from_stored(item: StoredMessage) -> IncomingMessage:
    return IncomingMessage(
        sender_name=item.sender_name,
        text=item.text,
        update_id=item.update_id,
        message_id=item.message_id,
        sender_id=item.sender_id,
        telegram_date=item.telegram_date,
        reply_to_message_id=item.reply_to_message_id,
    )


def action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)
