from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import json
import re
import logging
from pathlib import Path

from .agents.base import AgentAction, AgentProvider, ChatOnboardingDraft, FeedbackAnalysis, GeneralTaskPlan, MediaAttachment, OwnerQueryAnswer
from .chats.loader import ChatConfig, ChatRegistry, slugify_chat_name, write_new_chat
from .knowledge import load_knowledge_pack, load_knowledge_pack_documents
from .media import delete_media_file, display_message_text, has_message_content, media_file_ready, media_label
from .owner_query import OwnerQueryIntent, OwnerQueryScope, PortfolioChatSummary, parse_owner_time_phrase
from .storage.sqlite import ChatOnboarding, ChatThreadStore, DEFAULT_CHAT_STATE, LearningDraft, ReminderRecord, RuleRecord, StoredMessage

logger = logging.getLogger(__name__)
_GLOBAL_WORDING = re.compile(r"\b(для\s+всех|всем\s+клиент|глобальн)", re.IGNORECASE)
_MEMORY_PREFIXES = (
    ("общий контекст:", "global"),
    ("запомни для всех чатов:", "global"),
    ("контекст проекта:", "project"),
    ("запомни для проекта:", "project"),
    ("контекст:", "chat"),
    ("запомни для этого чата:", "chat"),
)
def _reminder_author(user_id: int | None, username: str | None, name: str | None) -> dict:
    username = (username or "").strip().lstrip("@") or None
    name = (name or "").strip() or None
    return {"created_by_user_id": user_id, "created_by_username": username, "created_by_name": name}


def _author_from_selection(selection) -> dict:
    return _reminder_author(selection.created_by_user_id, selection.created_by_username, selection.created_by_name)


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
    media_kind: str = ""
    media_path: str = ""
    telegram_file_id: str = ""
    media_mime: str = ""
    media_filename: str = ""
    media_group_id: str = ""


@dataclass(frozen=True)
class LearningProposal:
    draft_id: int
    chat_name: str
    understanding: str
    proposed_rule: str | None
    scope: str
    regenerate_current: bool
    memory_proposal: "MemoryProposal | None" = None


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
    global_allowed: bool = True


@dataclass(frozen=True)
class QuestionReplyResult:
    suggestion: Suggestion | None
    memory_proposal: MemoryProposal | None


@dataclass(frozen=True)
class OnboardingNotice:
    onboarding_id: int
    telegram_chat_id: int
    chat_title: str
    added_by_name: str
    needs_delivery: bool


@dataclass(frozen=True)
class OnboardingDraftProposal:
    onboarding_id: int
    telegram_chat_id: int
    chat_title: str
    name: str
    wiki: str


@dataclass(frozen=True)
class OwnerQueryResult:
    text: str
    prompt_id: int | None = None
    delivery_id: int | None = None
    selection_id: int | None = None
    general_task_id: int | None = None
    restart_marker_id: int | None = None


class AgentBridgeApplication:
    def __init__(
        self,
        registry: ChatRegistry,
        store: ChatThreadStore,
        provider: AgentProvider,
        owner_chat_id: int | None = None,
        episode_size: int = 40,
        chats_dir: Path | None = None,
        knowledge_dir: Path | None = None,
        owner_provider: AgentProvider | None = None,
        owner_timezone: str = "Asia/Novosibirsk",
    ):
        self.registry = registry
        self.store = store
        self.provider = provider
        self.owner_provider = owner_provider or provider
        self.owner_chat_id = owner_chat_id
        self.owner_timezone = owner_timezone
        self.episode_size = max(1, episode_size)
        self.chats_dir = chats_dir
        if knowledge_dir is not None:
            self.knowledge_dir = knowledge_dir
        elif chats_dir is not None:
            self.knowledge_dir = chats_dir.parent / "knowledge"
        else:
            self.knowledge_dir = Path.cwd() / "knowledge"
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def is_monitored_chat(self, telegram_chat_id: int) -> bool:
        return self.registry.get(telegram_chat_id) is not None

    def is_update_processed(self, telegram_update_id: int) -> bool:
        return self.store.is_update_processed(telegram_update_id)

    def mark_update_processed(self, telegram_update_id: int) -> None:
        self.store.mark_update_processed(telegram_update_id)

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
        media_kind: str = "",
        media_path: str = "",
        telegram_file_id: str = "",
        media_mime: str = "",
        media_filename: str = "",
        media_group_id: str = "",
    ) -> bool:
        if is_owner_chat:
            role = "owner"
            status = "ignored"
        else:
            chat = self.registry.get(chat_id)
            if chat is None:
                if not self.store.has_open_onboarding(chat_id):
                    return False
                role = "internal" if self._is_internal_sender(chat_id, sender_name) else "client"
                status = "held"
            else:
                role = "internal" if self._is_internal_sender(chat_id, sender_name) else "client"
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
            media_kind=media_kind,
            media_path=media_path,
            telegram_file_id=telegram_file_id,
            media_mime=media_mime,
            media_filename=media_filename,
            media_group_id=media_group_id,
        )

    def pending_media_downloads(self, telegram_chat_id: int) -> list[StoredMessage]:
        return [
            item for item in self.store.pending_messages(telegram_chat_id)
            if item.telegram_file_id and not media_file_ready(item.media_path)
        ]

    def set_message_media_path(self, message_id: int, media_path: str) -> None:
        self.store.set_media_path(message_id, media_path)

    def pending_voice_messages(self, telegram_chat_id: int) -> list[StoredMessage]:
        return [
            item for item in self.store.pending_messages(telegram_chat_id)
            if (item.media_kind or "").casefold() == "voice" and item.telegram_file_id
            and not (item.text or "").strip()
        ]

    def save_voice_transcript(self, update_id: int, transcript: str) -> None:
        """Пишет чистый транскрипт; метку [голосовое] добавит _episode_text/media_label."""
        self.store.set_message_text(update_id, transcript.strip())

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
                    self._discard_local_media(claimed)
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
                if has_message_content(item.text, item.media_kind, item.telegram_file_id)
                and (item.update_id is None or not self.store.is_update_processed(item.update_id))
            ]
            if not pending:
                logger.info("event=client_batch_ignored reason=empty_or_duplicate chat_id=%s", telegram_chat_id)
                return None
            claimed = self._claim_incoming(telegram_chat_id, pending)
            try:
                result = await self._run_episode(chat, pending, notify=True)
                if claimed:
                    self.store.mark_messages_processed(claimed)
                    self._discard_local_media(claimed)
                else:
                    for item in pending:
                        if item.update_id is not None:
                            self.store.mark_update_processed(item.update_id)
                    self._discard_incoming_media(pending)
            except Exception:
                if claimed:
                    self.store.release_messages([item.id for item in claimed])
                raise
            return result

    def _ingest_incoming(self, telegram_chat_id: int, item: IncomingMessage) -> None:
        if item.update_id is None or not has_message_content(item.text, item.media_kind, item.telegram_file_id):
            return
        role = "internal" if self._is_internal_sender(telegram_chat_id, item.sender_name) else "client"
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
            media_kind=item.media_kind,
            media_path=item.media_path,
            telegram_file_id=item.telegram_file_id,
            media_mime=item.media_mime,
            media_filename=item.media_filename,
            media_group_id=item.media_group_id,
        )

    def _claim_incoming(self, telegram_chat_id: int, messages: list[IncomingMessage]) -> list[StoredMessage]:
        update_ids = {item.update_id for item in messages if item.update_id is not None}
        if not update_ids:
            return []
        pending = [item for item in self.store.pending_messages(telegram_chat_id) if item.update_id in update_ids]
        return self.store.claim_messages([item.id for item in pending])

    async def _run_episode(
        self,
        chat: ChatConfig,
        messages: list[IncomingMessage],
        *,
        notify: bool,
        ignore_internal_filter: bool = False,
    ) -> Suggestion | None:
        if ignore_internal_filter:
            internal, external = [], messages
        else:
            internal, external = self._split_internal_messages(chat.telegram_chat_id, messages)
        for item in internal:
            self.store.record_internal_context(
                chat.telegram_chat_id, chat.name, item.sender_name.strip() or "Внутренний участник",
                _episode_text(item),
            )
        if not external:
            logger.info("event=client_batch_context_only chat_id=%s count=%d", chat.telegram_chat_id, len(internal))
            return None
        sender_names = list(dict.fromkeys(item.sender_name.strip() or "Неизвестный отправитель" for item in external))
        sender_name = sender_names[0] if len(sender_names) == 1 else ", ".join(sender_names)
        if len(messages) == 1:
            combined_message = _episode_text(messages[0])
        else:
            combined_message = "\n".join(
                f"{item.sender_name.strip() or 'Неизвестный отправитель'}: {_episode_text(item)}" for item in messages
            )
        rules = self.store.active_rule_texts(chat.telegram_chat_id)
        thread_id = self._thread_id_for_provider(chat.telegram_chat_id)
        exclude_ids = {item.update_id for item in messages if item.update_id is not None}
        context_pack = self._context_pack(chat, episode=combined_message, exclude_update_ids=exclude_ids)
        attachments = _episode_attachments(messages)
        logger.info(
            "event=codex_suggest_start chat_id=%s chat=%r batch_size=%d rule_count=%d thread=%s notify=%s attachments=%d",
            chat.telegram_chat_id, chat.name, len(messages), len(rules), "resume" if thread_id else "new", notify,
            len(attachments),
        )
        suggest_kwargs = {
            "message": combined_message,
            "sender_name": sender_name,
            "chat_name": chat.name,
            "wiki": self._contextual_wiki(chat),
            "rules": rules,
            "thread_id": thread_id,
            "context_pack": context_pack,
            "attachments": attachments,
        }
        reply = await self.provider.suggest(**suggest_kwargs)
        reply = await self._maybe_critique(reply, suggest_kwargs)
        refactor = getattr(self.provider, "refactor_reply", None)
        if refactor is not None:
            try:
                reply, sepia_thread_id = await refactor(
                    reply,
                    thread_id=self._sepia_thread_id_for_provider(chat.telegram_chat_id),
                )
                if sepia_thread_id:
                    self.store.save_sepia_thread(
                        chat.telegram_chat_id,
                        chat.name,
                        sepia_thread_id,
                        chat.agent_provider,
                        prompt_version=getattr(self.provider, "prompt_version", None),
                    )
            except Exception:
                logger.exception("event=sepia_refactor_failed fallback=rick_draft")
        self._persist_thread(chat, reply.thread_id)
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
        corrected = await critic(previous=reply, **suggest_kwargs)
        return replace(corrected, thread_id=reply.thread_id)

    def _thread_id_for_provider(self, telegram_chat_id: int) -> str | None:
        thread_id = self.store.get_thread_id(telegram_chat_id)
        if not thread_id:
            return None
        current = getattr(self.provider, "prompt_version", None)
        if current is None:
            return thread_id
        saved = self.store.get_thread_prompt_version(telegram_chat_id)
        if saved == current:
            return thread_id
        logger.info(
            "event=codex_thread_reset chat_id=%s reason=prompt_version saved=%s current=%s",
            telegram_chat_id, saved, current,
        )
        return None

    def _persist_thread(self, chat: ChatConfig, thread_id: str) -> None:
        self.store.save_thread(
            chat.telegram_chat_id,
            chat.name,
            thread_id,
            chat.agent_provider,
            prompt_version=getattr(self.provider, "prompt_version", None),
        )

    def _sepia_thread_id_for_provider(self, telegram_chat_id: int) -> str | None:
        thread_id = self.store.get_sepia_thread_id(telegram_chat_id)
        if not thread_id:
            return None
        current = getattr(self.provider, "prompt_version", None)
        if current is None or self.store.get_sepia_thread_prompt_version(telegram_chat_id) == current:
            return thread_id
        logger.info("event=sepia_thread_reset chat_id=%s reason=prompt_version", telegram_chat_id)
        return None

    def _discard_local_media(self, rows: list[StoredMessage]) -> None:
        for row in rows:
            delete_media_file(row.media_path)
        self.store.clear_media_paths([row.id for row in rows])

    def _discard_incoming_media(self, messages: list[IncomingMessage]) -> None:
        for item in messages:
            delete_media_file(item.media_path)

    def _apply_state_update(self, telegram_chat_id: int, candidate_state: dict | None, situation: str) -> None:
        current = self.store.get_chat_state(telegram_chat_id)
        merged = dict(current)
        if isinstance(candidate_state, dict):
            for key, value in candidate_state.items():
                # null в схеме значит «поле не менялось», пустой список/строка — явная очистка.
                if key in DEFAULT_CHAT_STATE and value is not None:
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

    def _context_pack(
        self,
        chat: ChatConfig,
        *,
        episode: str = "",
        exclude_update_ids: set[int] | None = None,
        time_from_utc: str | None = None,
        time_to_utc: str | None = None,
        time_label: str = "",
    ) -> str:
        state = self.store.get_chat_state(chat.telegram_chat_id)
        skipped = exclude_update_ids or set()
        if time_from_utc is not None or time_to_utc is not None:
            recent, history_total, history_truncated = self.store.portfolio_messages(
                chat.telegram_chat_id, limit=200,
                time_from_utc=time_from_utc, time_to_utc=time_to_utc,
            )
        else:
            recent = self.store.recent_messages(chat.telegram_chat_id)
            history_total = len(recent)
            history_truncated = False
        recent = [item for item in recent if item.update_id not in skipped]
        experience = self.store.recent_experience(chat.telegram_chat_id)
        shared = self._shared_knowledge(chat)
        parts = [
            f"Wiki:\n{chat.wiki or '(пусто)'}",
        ]
        if time_label:
            parts.append(f"Период запроса: {time_label}")
        if shared:
            parts.append(shared)
        parts.append("Текущее состояние чата:\n" + json.dumps(state, ensure_ascii=False, indent=2))
        if recent:
            history = "\n".join(
                f"{item.sender_name}: {display_message_text(item.text, item.media_kind, item.media_filename)}"
                for item in recent
            )
            if time_from_utc is not None or time_to_utc is not None:
                marker = f"показано {len(recent)} из {history_total}"
                if history_truncated:
                    marker += "; обрезано до 200 сообщений"
                parts.append(f"История за период ({marker}):\n" + history)
            else:
                parts.append("Недавняя история:\n" + history)
        elif time_from_utc is not None or time_to_utc is not None:
            parts.append("История за период (показано 0 из 0):\n(нет сообщений)")
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

    def _shared_knowledge(self, chat: ChatConfig) -> str:
        # Только compact core и список файлов, не вся база. Wiki чата важнее общей методики.
        return load_knowledge_pack(self.knowledge_dir, chat.knowledge_pack)

    def prepare_owner_delivery_parts(self, owner_chat_id: int, delivery_key: str, texts: list[str]) -> list[tuple[str, int | None]]:
        return self.store.prepare_owner_delivery_parts(owner_chat_id, delivery_key, texts)

    def record_owner_delivery_part(self, owner_chat_id: int, delivery_key: str, part_index: int, owner_message_id: int) -> None:
        self.store.record_owner_delivery_part(owner_chat_id, delivery_key, part_index, owner_message_id)

    def resolve_owner_reply(self, owner_chat_id: int, owner_message_id: int) -> int:
        return self.store.resolve_owner_reply(owner_chat_id, owner_message_id)

    def record_owner_delivery(self, recommendation_id: int, owner_chat_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_message(recommendation_id, owner_chat_id, owner_message_id)
        logger.info("event=owner_delivery_linked recommendation_id=%s owner_message_id=%s", recommendation_id, owner_message_id)

    def pending_suggestions(self, owner_chat_id: int) -> list[Suggestion]:
        self.store.assign_unowned_pending_recommendations(owner_chat_id)
        return [self._suggestion_from_record(record) for record in self.store.pending_recommendations(owner_chat_id)]

    def pending_onboarding_notices(self) -> list[OnboardingNotice]:
        return [
            OnboardingNotice(item.id, item.telegram_chat_id, item.chat_title, item.added_by_name, True)
            for item in self.store.pending_onboarding_notices()
        ]

    def record_onboarding_notice(self, onboarding_id: int, owner_message_id: int) -> None:
        self.store.attach_onboarding_notice(onboarding_id, owner_message_id)

    def record_onboarding_draft_message(self, onboarding_id: int, owner_message_id: int) -> None:
        self.store.attach_onboarding_draft_message(onboarding_id, owner_message_id)

    def begin_unconfigured_chat(
        self,
        telegram_chat_id: int,
        chat_title: str,
        added_by_name: str = "",
        added_by_id: int | None = None,
        update_id: int | None = None,
    ) -> OnboardingNotice | None:
        if self.owner_chat_id is not None and telegram_chat_id == self.owner_chat_id:
            return None
        if self.registry.get(telegram_chat_id) is not None:
            return None
        existing = self.store.get_onboarding(telegram_chat_id)
        record = self.store.ensure_onboarding(telegram_chat_id, chat_title, added_by_name, added_by_id)
        if record.status == "confirmed":
            return None
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        needs_delivery = record.owner_notice_message_id is None
        if existing is not None and existing.status not in {"cancelled"} and not needs_delivery:
            return OnboardingNotice(record.id, record.telegram_chat_id, record.chat_title, record.added_by_name, False)
        logger.info(
            "event=chat_onboarding_started chat_id=%s title=%r needs_delivery=%s",
            telegram_chat_id, record.chat_title, needs_delivery,
        )
        return OnboardingNotice(record.id, record.telegram_chat_id, record.chat_title, record.added_by_name, needs_delivery)

    def cancel_unconfigured_chat(self, telegram_chat_id: int) -> None:
        self.store.cancel_onboarding(telegram_chat_id)

    async def handle_onboarding_brief(
        self, owner_chat_id: int, reply_to_message_id: int, author_name: str, brief: str, update_id: int | None = None,
    ) -> OnboardingDraftProposal | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        record = self.store.get_onboarding_by_owner_message(reply_to_message_id)
        if record is None:
            return None
        text = brief.strip()
        if not text:
            return None
        drafter = getattr(self.owner_provider, "draft_chat_onboarding", None)
        if drafter is not None:
            draft = await drafter(
                group_title=record.chat_title, owner_brief=text, telegram_chat_id=record.telegram_chat_id,
            )
        else:
            draft = _local_onboarding_draft(record.chat_title, text, record.telegram_chat_id)
        name = draft.name.strip() or _name_from_brief(text) or record.chat_title
        wiki = draft.wiki.strip() or text
        slug = slugify_chat_name(draft.directory_slug or name, record.telegram_chat_id)
        saved = self.store.save_onboarding_draft(
            record.id, owner_brief=text, draft_name=name, draft_wiki=wiki, draft_directory=slug,
        )
        if saved is None:
            return None
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        logger.info("event=chat_onboarding_drafted onboarding_id=%s name=%r", saved.id, saved.draft_name)
        return OnboardingDraftProposal(saved.id, saved.telegram_chat_id, saved.chat_title, saved.draft_name, saved.draft_wiki)

    def confirm_onboarding(self, onboarding_id: int) -> ChatConfig | None:
        record = self.store.get_onboarding_by_id(onboarding_id)
        if record is None or record.status != "pending_draft" or not record.draft_name.strip() or not record.draft_wiki.strip():
            return None
        if self.chats_dir is None:
            logger.warning("event=chat_onboarding_blocked reason=no_chats_dir onboarding_id=%s", onboarding_id)
            return None
        existing = self.registry.get(record.telegram_chat_id)
        if existing is None:
            try:
                existing = write_new_chat(
                    self.chats_dir,
                    telegram_chat_id=record.telegram_chat_id,
                    name=record.draft_name,
                    wiki=record.draft_wiki,
                    directory_name=record.draft_directory,
                )
            except Exception:
                logger.exception("event=chat_onboarding_write_failed onboarding_id=%s", onboarding_id)
                return None
        confirmed = self.store.confirm_onboarding(onboarding_id)
        if confirmed is None:
            return None
        chat = self.registry.add(existing)
        self.store.release_held_messages(chat.telegram_chat_id)
        logger.info(
            "event=chat_onboarding_confirmed chat_id=%s name=%r directory=%s",
            chat.telegram_chat_id, chat.name, chat.directory,
        )
        return chat

    def reject_onboarding(self, onboarding_id: int) -> ChatOnboarding | None:
        record = self.store.get_onboarding_by_id(onboarding_id)
        if record is None or record.status not in {"pending_brief", "pending_draft"}:
            return None
        return record

    def mark_onboarding_clarification(self, onboarding_id: int, prompt_message_id: int) -> None:
        self.store.mark_onboarding_clarification(onboarding_id, prompt_message_id)

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
        analysis = await self.owner_provider.analyze_feedback(
            feedback=feedback, chat_name=recommendation.chat_name, original_message=recommendation.original_message,
            situation=recommendation.situation, suggested_reply=recommendation.suggested_reply, rules=rules,
        )
        if analysis.scope == "global" and not _GLOBAL_WORDING.search(feedback):
            analysis = FeedbackAnalysis(
                analysis.understanding, analysis.proposed_rule, analysis.conflict_key, "client",
                analysis.regenerate_current, analysis.revision_instruction,
                analysis.candidate_memory, analysis.candidate_memory_scope,
            )
        draft = self.store.create_learning_draft(recommendation.id, author_user_id, author_name, feedback, analysis)
        memory_proposal = self._create_feedback_memory_candidate(
            recommendation, author_user_id, author_name, analysis,
        )
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        logger.info("event=feedback_analysis_done draft_id=%s recommendation_id=%s scope=%s regenerate=%s", draft.id, recommendation.id, draft.scope, draft.regenerate_current)
        return self._proposal(draft, recommendation.chat_name, memory_proposal)

    @staticmethod
    def is_memory_context_command(text: str) -> bool:
        lowered = text.strip().casefold()
        return any(lowered.startswith(prefix) for prefix, _ in _MEMORY_PREFIXES)

    @staticmethod
    def is_global_memory_command(text: str) -> bool:
        lowered = text.strip().casefold()
        return any(lowered.startswith(prefix) for prefix, scope in _MEMORY_PREFIXES if scope == "global")

    async def handle_owner_context(
        self, owner_chat_id: int, reply_to_message_id: int | None, author_user_id: int,
        author_name: str, text: str, update_id: int | None = None,
    ) -> MemoryProposal | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        command = self._parse_memory_command(text)
        if command is None:
            return None
        scope, content = command
        recommendation = (
            self.store.get_recommendation_by_owner_message(owner_chat_id, reply_to_message_id)
            if reply_to_message_id is not None else None
        )
        if recommendation is None:
            if scope != "global":
                return None
            draft = self.store.create_memory_draft(
                None, author_user_id, author_name, content, "global", None, global_allowed=True,
            )
            if update_id is not None:
                self.store.mark_update_processed(update_id)
            return MemoryProposal(draft.id, "все чаты", draft.content, draft.scope, draft.global_allowed)
        chat = self.registry.get(recommendation.telegram_chat_id)
        project_key = chat.memory_project if chat is not None else None
        if scope == "project" and not project_key:
            return None
        draft = self.store.create_memory_draft(
            recommendation.id, author_user_id, author_name, content, scope,
            project_key if scope == "project" else None, global_allowed=False,
        )
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return MemoryProposal(draft.id, recommendation.chat_name, draft.content, draft.scope, draft.global_allowed)

    @staticmethod
    def _parse_memory_command(text: str) -> tuple[str, str] | None:
        lowered = text.strip().casefold()
        for prefix, scope in _MEMORY_PREFIXES:
            if lowered.startswith(prefix):
                content = text.strip()[len(prefix):].strip()
                return (scope, content) if content else None
        return None

    def confirm_memory(self, draft_id: int, scope: str | None = None) -> MemoryProposal | None:
        draft = self.store.confirm_memory_draft(draft_id, scope)
        if draft is None:
            return None
        if draft.recommendation_id is None:
            return MemoryProposal(draft.id, "все чаты", draft.content, draft.scope, draft.global_allowed)
        recommendation = self.store.get_recommendation(draft.recommendation_id)
        return None if recommendation is None else MemoryProposal(
            draft.id, recommendation.chat_name, draft.content, draft.scope, draft.global_allowed,
        )

    def reject_memory(self, draft_id: int) -> bool:
        return self.store.reject_memory_draft(draft_id)

    async def handle_owner_query(
        self, text: str, *, reply_to_message_id: int | None = None, update_id: int | None = None,
        author_user_id: int | None = None, author_username: str | None = None, author_name: str | None = None,
    ) -> OwnerQueryResult | str | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        if reply_to_message_id is not None:
            continued = await self.continue_owner_query(reply_to_message_id, text, update_id)
            if continued is not None:
                return continued
        chat = None
        scope_for_chat: OwnerQueryScope | None = None
        named_in_question = False
        author = _reminder_author(author_user_id, author_username, author_name)
        if reply_to_message_id is not None and self.owner_chat_id is not None:
            recommendation = self.store.get_recommendation_by_owner_message(self.owner_chat_id, reply_to_message_id)
            if recommendation is not None:
                chat = self.registry.get(recommendation.telegram_chat_id)
        if chat is None:
            chat = self.registry.find_by_name(text)
            named_in_question = chat is not None
        if chat is None and len(self.registry) == 1:
            only = self.registry.all_chats()[0]
            words = {word for word in re.findall(r"[\w-]+", only.name.casefold()) if len(word) >= 4}
            if words.intersection(re.findall(r"[\w-]+", text.casefold())):
                chat = only
                named_in_question = True
        all_requested = any(phrase in " ".join(text.casefold().split()) for phrase in ("все чаты", "все клиенты", "все проекты", "по всем", "all chats", "all clients", "all projects"))
        if chat is None and reply_to_message_id is None:
            scope = await self._resolve_owner_query_scope(text)
            if scope is not None:
                if len(scope.chat_ids) == 1 and scope.mode == "single":
                    chat = self.registry.get(scope.chat_ids[0])
                    scope_for_chat = scope
                    named_in_question = chat is not None
                elif scope.chat_ids:
                    return await self._answer_owner_portfolio(scope, update_id=update_id)
                else:
                    if update_id is not None and not self.store.claim_update_processed(update_id):
                        return None
                    return self._new_owner_query_selection(text, scope, author)
        if chat is None:
            names = "\n\n".join(item.name for item in self.registry.all_chats()) or "нет подключённых чатов"
            prompt_id = self.store.create_owner_query_prompt(text)
            if update_id is not None:
                self.store.mark_update_processed(update_id)
            return OwnerQueryResult(
                f"Уточните, о каком чате речь. Сейчас подключены:\n\n{names}",
                prompt_id,
            )
        if scope_for_chat is None:
            from_utc, to_utc, label = parse_owner_time_phrase(text, timezone_name=self.owner_timezone)
            if from_utc is not None or to_utc is not None:
                scope_for_chat = OwnerQueryScope("single", (chat.telegram_chat_id,), text, from_utc, to_utc, label)
        if named_in_question:
            planned = await self._prepare_general_task(text, related_chat=chat, author=author, reminder_only=True)
            if planned is not None:
                if update_id is not None:
                    self.store.mark_update_processed(update_id)
                return planned
        answer = await self._answer_owner_query_for_chat(chat, text, scope=scope_for_chat)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return self._follow_up_query_result(chat, text, answer, scope=scope_for_chat)

    async def _resolve_owner_query_scope(self, text: str) -> OwnerQueryScope | None:
        chats = self.registry.all_chats()
        lower = " ".join(text.casefold().split())
        all_phrases = ("все чаты", "все клиенты", "все проекты", "по всем", "all chats", "all clients", "all projects")
        if any(phrase in lower for phrase in all_phrases):
            from_utc, to_utc, label = parse_owner_time_phrase(text, timezone_name=self.owner_timezone)
            return OwnerQueryScope("all", tuple(item.telegram_chat_id for item in chats), text, from_utc, to_utc, label)
        exact = [item for item in chats if item.name.casefold() in lower or item.directory.name.casefold() in lower]
        if len(exact) == 1:
            from_utc, to_utc, label = parse_owner_time_phrase(text, timezone_name=self.owner_timezone)
            return OwnerQueryScope("single", (exact[0].telegram_chat_id,), text, from_utc, to_utc, label)
        if len(exact) > 1:
            from_utc, to_utc, label = parse_owner_time_phrase(text, timezone_name=self.owner_timezone)
            return OwnerQueryScope("multiple", tuple(item.telegram_chat_id for item in exact), text, from_utc, to_utc, label)
        if len(chats) == 1:
            return OwnerQueryScope("ambiguous", (), text)
        resolver = getattr(self.owner_provider, "resolve_owner_query_scope", None)
        if resolver is None:
            return None
        known = [{"name": item.name, "slug": item.directory.name} for item in chats]
        try:
            intent = await resolver(question=text, known_chats=known)
        except Exception:
            logger.exception("event=owner_query_scope_resolution_failed")
            return None
        if isinstance(intent, dict):
            intent = OwnerQueryIntent(
                str(intent.get("mode") or "ambiguous"), tuple(intent.get("selected_names") or ()),
                str(intent.get("time_phrase") or ""), str(intent.get("detail_level") or "short"),
            )
        selected: list[ChatConfig] = []
        for name in getattr(intent, "selected_names", ()):
            selected.extend(item for item in chats if name.casefold() in {item.name.casefold(), item.directory.name.casefold()})
        unique = {item.telegram_chat_id: item for item in selected}
        from_utc, to_utc, label = parse_owner_time_phrase(getattr(intent, "time_phrase", ""), timezone_name=self.owner_timezone)
        mode = str(getattr(intent, "mode", "ambiguous") or "ambiguous")
        if mode == "all":
            ids = tuple(item.telegram_chat_id for item in chats)
        else:
            ids = tuple(unique)
        if mode == "single" and len(ids) != 1:
            return OwnerQueryScope("ambiguous", (), text, from_utc, to_utc, label, getattr(intent, "detail_level", "short"))
        return OwnerQueryScope(mode, ids, text, from_utc, to_utc, label, getattr(intent, "detail_level", "short"))

    def _new_owner_query_selection(self, question: str, scope: OwnerQueryScope, author: dict | None = None) -> OwnerQueryResult:
        author = author or {}
        selection_id = self.store.create_owner_query_selection(
            question, self.owner_chat_id or 0, self.registry.known_ids(),
            time_from_utc=scope.time_from_utc, time_to_utc=scope.time_to_utc,
            time_label=scope.time_label, detail_level=scope.detail_level,
            created_by_user_id=author.get("created_by_user_id"),
            created_by_username=author.get("created_by_username"),
            created_by_name=author.get("created_by_name"),
        )
        return OwnerQueryResult(
            "Уточните охват вопроса:", selection_id=selection_id,
        )

    def owner_query_selection(self, selection_id: int):
        return self.store.get_owner_query_selection(selection_id)

    def attach_owner_query_selection(self, selection_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_query_selection(selection_id, owner_message_id)

    def owner_query_selection_options(self, selection_id: int) -> list[tuple[int, str, bool]]:
        selection = self.store.get_owner_query_selection(selection_id)
        if selection is None:
            return []
        chosen = set(selection.selected_chat_ids)
        return [(index, self.registry.get(chat_id).name if self.registry.get(chat_id) else f"чат {chat_id}", chat_id in chosen)
                for index, chat_id in enumerate(selection.available_chat_ids)]

    async def handle_owner_query_selection(
        self, selection_id: int, action: str, index: int | None = None, owner_chat_id: int | None = None,
    ) -> OwnerQueryResult | None:
        selection = self.store.get_owner_query_selection(selection_id)
        if selection is None or selection.status != "selecting":
            return OwnerQueryResult("Этот выбор уже обработан или больше недоступен.")
        if owner_chat_id is not None and selection.owner_chat_id != owner_chat_id:
            return OwnerQueryResult("Этот выбор недоступен в данном чате.")
        if action == "general":
            self.store.cancel_owner_query_selection(selection_id)
            return await self._prepare_general_task(selection.question, author=_author_from_selection(selection))
        if action == "cancel":
            self.store.cancel_owner_query_selection(selection_id)
            return OwnerQueryResult("Запрос отменён.")
        if action == "reset":
            self.store.update_owner_query_selection(selection_id, selected_chat_ids=(), mode="ambiguous")
            return OwnerQueryResult("Уточните охват вопроса:", selection_id=selection_id)
        selected = list(selection.selected_chat_ids)
        if action == "choose" and index is not None and 0 <= index < len(selection.available_chat_ids):
            self.store.update_owner_query_selection(selection_id, mode="single", selected_chat_ids=(selection.available_chat_ids[index],))
            action = "done"
        if action in {"item", "item_add", "item_remove"} and index is not None and 0 <= index < len(selection.available_chat_ids):
            chat_id = selection.available_chat_ids[index]
            if action == "item_add":
                selected = [chat_id] if selection.mode == "single" else list(dict.fromkeys([*selected, chat_id]))
            elif action == "item_remove":
                selected = [item for item in selected if item != chat_id]
            elif selection.mode == "single":
                selected = [] if selected == [chat_id] else [chat_id]
            else:
                selected.remove(chat_id) if chat_id in selected else selected.append(chat_id)
            self.store.update_owner_query_selection(selection_id, selected_chat_ids=selected)
            return OwnerQueryResult("Выберите проекты и нажмите «Готово».", selection_id=selection_id)
        if action in {"all", "multi", "single"}:
            if action == "all":
                selected = list(selection.available_chat_ids)
            self.store.update_owner_query_selection(selection_id, mode=action, selected_chat_ids=selected)
            if action != "all":
                return OwnerQueryResult("Выберите проекты и нажмите «Готово».", selection_id=selection_id)
        if action == "done" or action == "all":
            selection = self.store.claim_owner_query_selection(selection_id)
            if selection is None:
                return OwnerQueryResult("Этот выбор уже обрабатывается или обработан.")
            ids = selection.selected_chat_ids
            if not ids:
                self.store.reset_owner_query_selection(selection_id)
                return OwnerQueryResult("Выберите хотя бы один проект.", selection_id=selection_id)
            scope = OwnerQueryScope(
                "single" if len(ids) == 1 else "multiple", ids, selection.question,
                selection.time_from_utc, selection.time_to_utc, selection.time_label, selection.detail_level,
            )
            if len(ids) == 1:
                chat = self.registry.get(ids[0])
                if chat is None:
                    self.store.fail_owner_query_selection(selection_id)
                    return OwnerQueryResult("Выбранный чат больше не подключён.")
                planned = await self._prepare_general_task(
                    scope.question, related_chat=chat, author=_author_from_selection(selection), reminder_only=True,
                )
                if planned is not None:
                    self.store.finish_owner_query_selection(selection_id)
                    return planned
                try:
                    answer = await self._answer_owner_query_for_chat(chat, scope.question, scope=scope)
                    result = self._follow_up_query_result(chat, scope.question, answer, scope=scope)
                except Exception:
                    logger.exception("event=owner_query_selection_single_failed selection_id=%s", selection_id)
                    self.store.fail_owner_query_selection(selection_id)
                    return OwnerQueryResult("Не удалось обработать выбранный проект.")
                self.store.finish_owner_query_selection(selection_id)
                return result
            try:
                return await self._answer_owner_portfolio(scope, selection_id=selection_id)
            except Exception:
                logger.exception("event=owner_query_selection_portfolio_failed selection_id=%s", selection_id)
                self.store.fail_owner_query_selection(selection_id)
                return OwnerQueryResult("Не удалось обработать выбранные проекты.")
        return OwnerQueryResult("Выберите действие.", selection_id=selection_id)

    async def _prepare_general_task(
        self, request: str, task_id: int | None = None, *,
        related_chat: ChatConfig | None = None, author: dict | None = None, reminder_only: bool = False,
    ) -> OwnerQueryResult | None:
        planner = getattr(self.owner_provider, "plan_general_task", None)
        if planner is None:
            return None if reminder_only else OwnerQueryResult("Общие задачи в этом режиме недоступны.")
        from datetime import datetime, timedelta, timezone
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(self.owner_timezone)
        except Exception:
            zone = timezone(timedelta(hours=7), name=self.owner_timezone)
        planner_request = request
        if related_chat is not None:
            planner_request = f"Выбранный клиент: {related_chat.name}\n\nИсходный запрос:\n{request}"
        thread_id = self._owner_query_thread_id_for_provider(0)
        try:
            plan = await planner(
                request=planner_request, timezone_name=self.owner_timezone,
                now_local=datetime.now(zone).isoformat(timespec="minutes"), thread_id=thread_id,
            )
        except Exception:
            logger.exception("event=general_task_plan_failed")
            return None if reminder_only else OwnerQueryResult("Не удалось подготовить понимание задачи.")
        if not isinstance(plan, GeneralTaskPlan) or (reminder_only and plan.kind != "reminder"):
            return None if reminder_only else OwnerQueryResult("Не удалось подготовить понимание задачи.")
        self.store.save_owner_query_thread(0, "Общие задачи", plan.thread_id,
            prompt_version=getattr(self.owner_provider, "prompt_version", None))
        payload = {"remind_at_utc": plan.remind_at_utc, "local_label": plan.local_label, "reminder_text": plan.reminder_text}
        if related_chat is not None:
            payload["related_chat_id"] = related_chat.telegram_chat_id
            payload["related_chat_name"] = related_chat.name
        if author:
            payload.update(author)
        if task_id is None:
            task_id = self.store.create_general_task(self.owner_chat_id or 0, request, plan.understanding, plan.kind, payload)
        elif not self.store.revise_general_task(task_id, request, plan.understanding, plan.kind, payload):
            return OwnerQueryResult("Эта задача уже обработана или отменена.")
        return OwnerQueryResult(f"Я понял задачу так:\n\n{plan.understanding}", general_task_id=task_id)

    def attach_general_task(self, task_id: int, owner_message_id: int) -> None:
        self.store.attach_general_task_message(task_id, owner_message_id)

    def mark_general_task_clarification(self, task_id: int, owner_message_id: int) -> bool:
        return self.store.mark_general_task_clarification(task_id, owner_message_id)

    def general_task_needs_confirmation(self, task_id: int) -> bool:
        task = self.store.get_general_task(task_id)
        return task is not None and task.status == "confirming"

    async def handle_general_task_clarification(
        self, owner_chat_id: int, reply_to_message_id: int, text: str, update_id: int | None = None,
    ) -> OwnerQueryResult | None:
        task = self.store.general_task_by_clarification(owner_chat_id, reply_to_message_id)
        if task is None:
            return None
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        result = await self._prepare_general_task(task.request_text + "\n\nУточнение владельца: " + text, task.id)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return result

    async def handle_general_task_followup(
        self, owner_chat_id: int, reply_to_message_id: int, text: str, update_id: int | None = None,
    ) -> OwnerQueryResult | None:
        source = self.store.general_task_result_by_message(owner_chat_id, reply_to_message_id)
        if source is None or (update_id is not None and self.store.is_update_processed(update_id)):
            return None
        task, answer = source
        request = (
            f"Продолжение выполненной общей задачи.\n\nИсходная задача:\n{task.request_text}"
            f"\n\nПредыдущий результат:\n{answer}\n\nНовый запрос владельца:\n{text}"
        )
        result = await self._prepare_general_task(request)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return result

    async def handle_general_task_action(self, task_id: int, action: str, owner_chat_id: int) -> OwnerQueryResult:
        task = self.store.get_general_task(task_id)
        if task is None or task.owner_chat_id != owner_chat_id or task.status != "confirming":
            return OwnerQueryResult("Эта задача уже обработана или больше недоступна.")
        if action == "cancel":
            self.store.set_general_task_status(task_id, "confirming", "cancelled")
            return OwnerQueryResult("Кладу задачу на полку. Отменено.")
        if action == "refine":
            return OwnerQueryResult("Напишите или наговорите нюанс ответом на это сообщение.", general_task_id=task_id)
        if action != "confirm" or not self.store.set_general_task_status(task_id, "confirming", "executing"):
            return OwnerQueryResult("Эта задача уже обрабатывается или обработана.")
        try:
            if task.kind == "reminder":
                remind_at = str(task.payload.get("remind_at_utc") or "")
                reminder_text = str(task.payload.get("reminder_text") or "")
                from datetime import datetime, timezone
                try:
                    future = datetime.fromisoformat(remind_at) > datetime.now(timezone.utc)
                except (TypeError, ValueError):
                    future = False
                if not future or not reminder_text:
                    self.store.set_general_task_status(task_id, "executing", "failed")
                    return OwnerQueryResult("Не хватает точного времени или текста напоминания. Уточните задачу заново.")
                reminder_id = self.create_reminder(
                    remind_at, reminder_text,
                    related_chat_id=task.payload.get("related_chat_id"),
                    related_chat_name=task.payload.get("related_chat_name"),
                    created_by_user_id=task.payload.get("created_by_user_id"),
                    created_by_username=task.payload.get("created_by_username"),
                    created_by_name=task.payload.get("created_by_name"),
                )
                self.store.set_general_task_status(task_id, "executing", "done")
                return OwnerQueryResult(
                    f"Напоминание #{reminder_id} сохранено на {task.payload.get('local_label') or remind_at} ({self.owner_timezone}).",
                    general_task_id=task_id,
                )
            if task.kind == "restart":
                import os
                marker_id = self.store.create_self_restart(task.id, owner_chat_id, os.getpid(), task.request_text)
                return OwnerQueryResult(
                    "Перезапускаюсь. Сейчас проверим, переживу ли я собственную операцию на мозге.",
                    restart_marker_id=marker_id,
                )
            runner = getattr(self.owner_provider, "run_general_task", None)
            thread_id = self._owner_query_thread_id_for_provider(0)
            if runner is None or not thread_id:
                raise RuntimeError("general task runner unavailable")
            result = await runner(request=task.request_text, thread_id=thread_id)
            if isinstance(result, OwnerQueryAnswer):
                self.store.save_owner_query_thread(0, "Общие задачи", result.thread_id,
                    prompt_version=getattr(self.owner_provider, "prompt_version", None))
                answer = result.answer
            else:
                answer = str(result)
            self.store.set_general_task_status(task_id, "executing", "done")
            return OwnerQueryResult(answer, general_task_id=task_id)
        except Exception:
            logger.exception("event=general_task_failed task_id=%s", task_id)
            self.store.set_general_task_status(task_id, "executing", "failed")
            return OwnerQueryResult("Не удалось выполнить общую задачу. Изменения автоматически не повторяю.")

    def attach_owner_query_prompt(self, prompt_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_query_prompt(prompt_id, owner_message_id)

    def save_pending_owner_query_delivery(self, text: str, prompt_id: int | None, selection_id: int | None = None, general_task_id: int | None = None) -> int:
        return self.store.create_owner_query_delivery(text, prompt_id, selection_id, general_task_id)

    def pending_owner_query_deliveries(self) -> list[OwnerQueryResult]:
        return [
            OwnerQueryResult(text=text, prompt_id=prompt_id, delivery_id=delivery_id, selection_id=selection_id, general_task_id=general_task_id)
            for delivery_id, text, prompt_id, selection_id, general_task_id in self.store.pending_owner_query_deliveries()
        ]

    def record_owner_query_delivery(self, delivery_id: int, owner_message_id: int) -> None:
        self.store.attach_owner_query_delivery(delivery_id, owner_message_id)

    def create_reminder(
        self, remind_at_utc: str, text: str, *,
        related_chat_id: int | None = None, related_chat_name: str | None = None,
        created_by_user_id: int | None = None, created_by_username: str | None = None,
        created_by_name: str | None = None,
    ) -> int:
        return self.store.create_reminder(
            self.owner_chat_id, remind_at_utc, text,
            related_chat_id=related_chat_id, related_chat_name=related_chat_name,
            created_by_user_id=created_by_user_id, created_by_username=created_by_username,
            created_by_name=created_by_name,
        )

    def pending_self_restart(self, current_pid: int):
        return self.store.pending_self_restart(current_pid)

    def finish_self_restart(self, restart_id: int, *, launched: bool) -> bool:
        return self.store.finish_self_restart(restart_id, launched=launched)

    def acknowledge_self_restart(self, restart_id: int) -> bool:
        return self.store.acknowledge_self_restart(restart_id)

    def pending_due_reminders(self) -> list[ReminderRecord]:
        return self.store.pending_due_reminders(self.owner_chat_id)

    def pending_reminders(self) -> list[ReminderRecord]:
        return self.store.pending_reminders(self.owner_chat_id)

    def mark_reminder_sent(self, reminder_id: int) -> bool:
        return self.store.mark_reminder_sent(reminder_id)

    async def continue_owner_query(
        self, owner_message_id: int, text: str, update_id: int | None = None,
    ) -> OwnerQueryResult | str | None:
        if update_id is not None and self.store.is_update_processed(update_id):
            return None
        prompt = self.store.get_owner_query_prompt_by_message(owner_message_id)
        if prompt is None:
            return None
        if prompt.target_chat_ids:
            scope = OwnerQueryScope(
                "multiple" if len(prompt.target_chat_ids) > 1 else "single", prompt.target_chat_ids, text,
                prompt.time_from_utc, prompt.time_to_utc, prompt.time_label or "текущее состояние и недавняя история",
                prompt.detail_level,
            )
            if not self.store.answer_owner_query_prompt(prompt.id):
                return None
            if len(scope.chat_ids) == 1:
                chat = self.registry.get(scope.chat_ids[0])
                if chat is None:
                    if update_id is not None:
                        self.store.mark_update_processed(update_id)
                    return OwnerQueryResult("Выбранный проект больше не подключён.")
                answer = await self._answer_owner_query_for_chat(chat, text, scope=scope)
                result = self._follow_up_query_result(chat, text, answer, scope=scope)
            else:
                try:
                    result = await self._answer_owner_portfolio(scope)
                except LookupError:
                    result = OwnerQueryResult("Выбранные проекты больше не подключены.")
            if update_id is not None:
                self.store.mark_update_processed(update_id)
            return result
        if prompt.telegram_chat_id is not None:
            chat = self.registry.get(prompt.telegram_chat_id)
            if chat is None:
                return None
            self.store.answer_owner_query_prompt(prompt.id)
            answer = await self._answer_owner_query_for_chat(chat, text)
            if update_id is not None:
                self.store.mark_update_processed(update_id)
            return self._follow_up_query_result(chat, text, answer)
        chat = self.registry.find_by_name(text)
        if chat is None:
            names = "\n\n".join(item.name for item in self.registry.all_chats()) or "нет подключённых чатов"
            if update_id is not None:
                self.store.mark_update_processed(update_id)
            return OwnerQueryResult(
                f"Не нашёл такой чат. Напишите имя ещё раз. Сейчас подключены:\n\n{names}",
                prompt.id,
            )
        self.store.answer_owner_query_prompt(prompt.id)
        answer = await self._answer_owner_query_for_chat(chat, prompt.question)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        return self._follow_up_query_result(chat, prompt.question, answer)

    def _follow_up_query_result(
        self, chat: ChatConfig, question: str, answer: str, *, scope: OwnerQueryScope | None = None,
    ) -> OwnerQueryResult:
        prompt_id = self.store.create_owner_query_prompt(
            question, chat.telegram_chat_id,
            target_chat_ids=scope.chat_ids if scope else (),
            time_from_utc=scope.time_from_utc if scope else None,
            time_to_utc=scope.time_to_utc if scope else None,
            time_label=scope.time_label if scope else "",
            detail_level=scope.detail_level if scope else "short",
        )
        return OwnerQueryResult(answer, prompt_id)

    async def _answer_owner_portfolio(
        self, scope: OwnerQueryScope, *, update_id: int | None = None, selection_id: int | None = None,
    ) -> OwnerQueryResult:
        chats = [self.registry.get(chat_id) for chat_id in scope.chat_ids]
        chats = [chat for chat in chats if chat is not None]
        if not chats:
            raise LookupError("no selected chats are still configured")
        semaphore = asyncio.Semaphore(4)

        async def summarize(chat: ChatConfig) -> dict[str, object]:
            async with semaphore:
                context, shown, total, truncated = self._portfolio_context(chat, scope)
                method = getattr(self.provider, "summarize_portfolio_chat", None)
                if method is None:
                    raise RuntimeError("provider does not support portfolio summaries")
                result = await method(
                    question=scope.question, chat_name=chat.name, period=scope.time_label,
                    context_pack=context, detail_level=scope.detail_level,
                )
                if isinstance(result, dict):
                    result = PortfolioChatSummary(chat.name, scope.time_label, **{
                        key: str(result.get(key) or "") for key in (
                            "current_status", "events", "problems", "waiting_us", "waiting_client",
                            "next_step", "metrics", "uncertainties",
                        )
                    })
                return {"chat_name": chat.name, "summary": result, "shown": shown, "total": total, "truncated": truncated}

        results = await asyncio.gather(*(summarize(chat) for chat in chats), return_exceptions=True)
        compact: list[dict[str, object]] = []
        for chat, result in zip(chats, results):
            if isinstance(result, Exception):
                compact.append({"chat_name": chat.name, "failure": "не удалось получить сводку"})
            else:
                summary = result["summary"]
                if isinstance(summary, PortfolioChatSummary):
                    compact.append({"chat_name": chat.name, "summary": {
                        key: getattr(summary, key) for key in (
                            "period", "current_status", "events", "problems", "waiting_us", "waiting_client",
                            "next_step", "metrics", "uncertainties",
                        )
                    }, "history": f"показано {result['shown']} из {result['total']}" + ("; обрезано" if result["truncated"] else "")})
        aggregate = getattr(self.owner_provider, "aggregate_owner_portfolio", None)
        if aggregate is not None:
            try:
                answer = await aggregate(
                    question=scope.question, period=scope.time_label, detail_level=scope.detail_level, summaries=compact,
                )
            except Exception:
                logger.exception("event=owner_portfolio_aggregate_failed")
                answer = "Не удалось собрать общий итог; отдельные сводки временно недоступны."
        else:
            answer = "\n\n".join(
                f"{item['chat_name']}: {item.get('failure') or (item.get('summary') or {}).get('current_status', '')}"
                for item in compact
            )
        prompt_id = self.store.create_owner_query_prompt(
            scope.question, self.owner_chat_id,
            target_chat_ids=scope.chat_ids, time_from_utc=scope.time_from_utc,
            time_to_utc=scope.time_to_utc, time_label=scope.time_label, detail_level=scope.detail_level,
        )
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        if selection_id is not None:
            self.store.finish_owner_query_selection(selection_id)
        return OwnerQueryResult(str(answer), prompt_id)

    def _portfolio_context(self, chat: ChatConfig, scope: OwnerQueryScope) -> tuple[str, int, int, bool]:
        messages, total, truncated = self.store.portfolio_messages(
            chat.telegram_chat_id, time_from_utc=scope.time_from_utc, time_to_utc=scope.time_to_utc, limit=200,
        )
        state = self.store.get_chat_state(chat.telegram_chat_id)
        history = "\n".join(f"{item.sender_name}: {display_message_text(item.text, item.media_kind, item.media_filename)}" for item in messages)
        parts = [f"Клиент: {chat.name}", f"Период: {scope.time_label}", f"Wiki:\n{chat.wiki or '(пусто)'}"]
        shared = self._shared_knowledge(chat)
        if shared:
            parts.append(shared)
        parts.append("Текущее состояние чата:\n" + json.dumps(state, ensure_ascii=False))
        parts.append(f"История ({'показано ' + str(len(messages)) + ' из ' + str(total) + (', обрезано' if truncated else '')}):\n" + (history or "(нет сообщений)"))
        memories = self.store.active_memory_entries(chat.telegram_chat_id, chat.memory_project)
        if memories:
            parts.append("Подтверждённая память:\n" + "\n".join(f"- {item.content}" for item in memories))
        parts.append("Правила:\n" + ("\n".join(f"- {rule}" for rule in self.store.active_rule_texts(chat.telegram_chat_id)) or "(нет)"))
        return "\n\n".join(parts), len(messages), total, truncated

    async def _answer_owner_query_for_chat(
        self, chat: ChatConfig, question: str, *, scope: OwnerQueryScope | None = None,
    ) -> str:
        answerer = getattr(self.owner_provider, "answer_owner_query", None)
        pack = self._context_pack(
            chat,
            time_from_utc=scope.time_from_utc if scope else None,
            time_to_utc=scope.time_to_utc if scope else None,
            time_label=scope.time_label if scope else "",
        )
        if answerer is not None:
            thread_id = self._owner_query_thread_id_for_provider(chat.telegram_chat_id)
            result = await answerer(
                question=question, chat_name=chat.name, context_pack=pack, thread_id=thread_id,
            )
            if isinstance(result, OwnerQueryAnswer):
                self.store.save_owner_query_thread(
                    chat.telegram_chat_id,
                    chat.name,
                    result.thread_id,
                    chat.agent_provider,
                    prompt_version=getattr(self.owner_provider, "prompt_version", None),
                )
                return result.answer
            return str(result)
        return self._local_status(chat)

    def _owner_query_thread_id_for_provider(self, telegram_chat_id: int) -> str | None:
        thread_id = self.store.get_owner_query_thread_id(telegram_chat_id)
        if not thread_id:
            return None
        current = getattr(self.owner_provider, "prompt_version", None)
        if current is None:
            return thread_id
        saved = self.store.get_owner_query_thread_prompt_version(telegram_chat_id)
        if saved == current:
            return thread_id
        logger.info(
            "event=owner_query_thread_reset chat_id=%s reason=prompt_version saved=%s current=%s",
            telegram_chat_id, saved, current,
        )
        return None

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
        state = self.store.get_chat_state(chat.telegram_chat_id)
        facts = list(state.get("facts") or [])
        facts.append(f"Уточнение владельца: {answer.strip()}")
        state["facts"] = facts
        self.store.save_chat_state(chat.telegram_chat_id, state)
        lock = self._chat_locks.setdefault(chat.telegram_chat_id, asyncio.Lock())
        async with lock:
            suggestion = await self._run_episode(
                chat,
                [IncomingMessage(
                    "Владелец",
                    f"Уточнение владельца по вопросу «{question.question}»: {answer.strip()}",
                )],
                notify=True,
                ignore_internal_filter=True,
            )
        self.store.answer_owner_question(question.id)
        if update_id is not None:
            self.store.mark_update_processed(update_id)
        memory_proposal = None
        if question.recommendation_id is not None:
            draft = self.store.create_memory_draft(
                question.recommendation_id, author_user_id, author_name, answer.strip(), "chat", None,
                global_allowed=False,
            )
            memory_proposal = MemoryProposal(draft.id, chat.name, draft.content, draft.scope, draft.global_allowed)
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
        analysis = await self.owner_provider.analyze_feedback(
            feedback=combined_feedback, chat_name=recommendation.chat_name, original_message=recommendation.original_message,
            situation=recommendation.situation, suggested_reply=recommendation.suggested_reply,
            rules=self.store.active_rule_texts(recommendation.telegram_chat_id),
        )
        if analysis.scope == "global" and not _GLOBAL_WORDING.search(combined_feedback):
            analysis = FeedbackAnalysis(
                analysis.understanding, analysis.proposed_rule, analysis.conflict_key, "client",
                analysis.regenerate_current, analysis.revision_instruction,
                analysis.candidate_memory, analysis.candidate_memory_scope,
            )
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
            thread_id = self._thread_id_for_provider(recommendation.telegram_chat_id)
            if chat is not None:
                rules = self.store.active_rule_texts(recommendation.telegram_chat_id)
                reply = await self.provider.revise(
                    feedback=draft.revision_instruction or draft.feedback,
                    message=recommendation.original_message, sender_name=recommendation.sender_name,
                    chat_name=chat.name, wiki=self._contextual_wiki(chat), rules=rules, thread_id=thread_id or "",
                    context_pack=self._context_pack(chat, episode=recommendation.original_message),
                )
                self._persist_thread(chat, reply.thread_id)
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

    def _create_feedback_memory_candidate(
        self, recommendation, author_user_id: int, author_name: str, analysis: FeedbackAnalysis,
    ) -> MemoryProposal | None:
        content = (analysis.candidate_memory or "").strip()
        if len(content) < 12:
            return None
        scope = analysis.candidate_memory_scope if analysis.candidate_memory_scope in {"global", "chat"} else "chat"
        if scope == "global" and self._candidate_mentions_client(content, recommendation):
            return None
        chat = self.registry.get(recommendation.telegram_chat_id)
        if chat is None or self._memory_candidate_is_duplicate(chat, content):
            return None
        draft = self.store.create_memory_draft(
            recommendation.id, author_user_id, author_name, content, scope, None,
            global_allowed=scope == "global",
        )
        return MemoryProposal(draft.id, recommendation.chat_name, draft.content, draft.scope, draft.global_allowed)

    def _memory_candidate_is_duplicate(self, chat: ChatConfig, content: str) -> bool:
        candidate = _normalise_memory_text(content)
        if not candidate:
            return True
        sources = [item.content for item in self.store.active_memory_entries(chat.telegram_chat_id, chat.memory_project)]
        sources.extend(self.store.active_rule_texts(chat.telegram_chat_id))
        for document in load_knowledge_pack_documents(self.knowledge_dir, chat.knowledge_pack):
            sources.extend(line for line in document.splitlines() if line.strip())
        for source in sources:
            normalized = _normalise_memory_text(source)
            if not normalized:
                continue
            if candidate == normalized:
                return True
            if len(candidate) >= 40 and (candidate in normalized or normalized in candidate):
                return True
        return False

    @staticmethod
    def _candidate_mentions_client(content: str, recommendation) -> bool:
        normalized = _normalise_memory_text(content)
        for value in (recommendation.chat_name, recommendation.sender_name):
            marker = _normalise_memory_text(value)
            if marker and marker in normalized:
                return True
        return False

    @staticmethod
    def _proposal(
        draft: LearningDraft, chat_name: str, memory_proposal: MemoryProposal | None = None,
    ) -> LearningProposal:
        return LearningProposal(
            draft.id, chat_name, draft.understanding, draft.proposed_rule, draft.scope,
            draft.regenerate_current, memory_proposal,
        )

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

    def _is_internal_sender(self, telegram_chat_id: int, sender_name: str) -> bool:
        name_tokens = re.findall(r"[a-zа-яё0-9]+", sender_name.casefold())
        if not name_tokens:
            return False
        for rule in self.store.active_rule_texts(telegram_chat_id, include_global=False):
            rule_tokens = re.findall(r"[a-zа-яё0-9]+", rule.casefold())
            # Learning rules are chat-scoped. Prefix matching handles the
            # ordinary Russian case ending ("Евгений Расюк" / "Евгения
            # Расюка") without introducing a global participant registry.
            if all(
                any(
                    len(token) >= 4
                    and (candidate.startswith(token[:5]) or token.startswith(candidate[:5]))
                    for candidate in rule_tokens
                )
                for token in name_tokens
            ):
                return True
        return False

    def _split_internal_messages(
        self, telegram_chat_id: int, messages: list[IncomingMessage],
    ) -> tuple[list[IncomingMessage], list[IncomingMessage]]:
        internal = [item for item in messages if self._is_internal_sender(telegram_chat_id, item.sender_name)]
        return internal, [item for item in messages if item not in internal]


def _name_from_brief(brief: str) -> str:
    first = next((line.strip(" :-—") for line in brief.splitlines() if line.strip()), "")
    return first[:80]


def _local_onboarding_draft(group_title: str, owner_brief: str, telegram_chat_id: int) -> ChatOnboardingDraft:
    name = _name_from_brief(owner_brief) or group_title or f"Чат {telegram_chat_id}"
    wiki = "# Wiki клиентского чата\n\n## О чате\n\n" + owner_brief.strip() + "\n"
    return ChatOnboardingDraft(name=name, wiki=wiki, directory_slug=slugify_chat_name(name, telegram_chat_id))


def _from_stored(item: StoredMessage) -> IncomingMessage:
    return IncomingMessage(
        sender_name=item.sender_name,
        text=item.text,
        update_id=item.update_id,
        message_id=item.message_id,
        sender_id=item.sender_id,
        telegram_date=item.telegram_date,
        reply_to_message_id=item.reply_to_message_id,
        media_kind=item.media_kind,
        media_path=item.media_path,
        telegram_file_id=item.telegram_file_id,
        media_mime=item.media_mime,
        media_filename=item.media_filename,
        media_group_id=item.media_group_id,
    )


def _episode_text(item: IncomingMessage) -> str:
    body = (item.text or "").strip()
    unavailable = bool(item.telegram_file_id or item.media_kind) and not media_file_ready(item.media_path)
    if item.media_kind or item.media_filename:
        label = media_label(item.media_kind, item.media_filename, unavailable=unavailable)
        if body and label:
            return f"{label} {body}"
        return body or label
    return body


def _episode_attachments(messages: list[IncomingMessage]) -> tuple[MediaAttachment, ...]:
    attachments = []
    for item in messages:
        if not media_file_ready(item.media_path):
            continue
        # Голосовой передаём транскриптом в тексте; аудиофайл модели не нужен.
        if (item.media_kind or "").casefold() == "voice":
            continue
        attachments.append(
            MediaAttachment(
                path=item.media_path,
                kind=item.media_kind,
                mime=item.media_mime,
                filename=item.media_filename,
            )
        )
    return tuple(attachments)


def action_label(action: str) -> str:
    return _ACTION_LABELS.get(action, action)


def _normalise_memory_text(text: str) -> str:
    return re.sub(r"[^\w\s]+", " ", text.casefold(), flags=re.UNICODE).strip()
