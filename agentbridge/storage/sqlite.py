from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

DEFAULT_CHAT_STATE = {
    "participants": [],
    "summary": "",
    "stage": "",
    "facts": [],
    "decisions": [],
    "agreements": [],
    "commitments": [],
    "waiting_from_client": [],
    "waiting_from_us": [],
    "open_questions": [],
    "risks": [],
    "unknowns": [],
    "next_step": "",
    "updated_at": "",
}

MEMORY_KINDS = (
    "fact",
    "decision",
    "commitment",
    "preference",
    "open_question",
    "rule",
    "assumption",
    "experience",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _turn_can_see_limit(turn_started_at: str, limit_seen_at: str) -> bool:
    """Мог ли turn, начавшийся в `turn_started_at`, знать о текущем лимите.

    Ответ положительный, только если turn начался не раньше последнего
    зафиксированного отказа по лимиту. Всё, что не разобралось, считаем
    «может знать»: лишняя проверка привела бы к вечно висящему лимиту, что
    хуже одного лишнего подтверждения восстановления.
    """
    try:
        started = datetime.fromisoformat(turn_started_at)
    except (TypeError, ValueError):
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    try:
        seen = datetime.fromisoformat(limit_seen_at) if limit_seen_at else None
    except (TypeError, ValueError):
        return True
    if seen is None:
        return True
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return started >= seen


# Ключ пары уведомлений об исчерпанном лимите Codex: пока он есть в
# operational_state, владельцу не отправляются повторные сообщения о том же.
_CODEX_LIMIT_KEY = "codex_usage_limit:notice"
_CODEX_LIMIT_REASON_KEY = "codex_usage_limit:reset_hint"
# Момент, когда владельцу показано время сброса лимита. По нему считается,
# когда следующий запрос уже можно рискнуть отдать в Codex: до сброса
# попытка заведомо отказная, после сброса — обычный рабочий запрос.
_CODEX_LIMIT_RESET_AT_KEY = "codex_usage_limit:reset_at_utc"
# Когда лимит заметили в последний раз. Служит запасным интервалом ожидания,
# когда время сброса от Codex не пришло.
_CODEX_LIMIT_SEEN_AT_KEY = "codex_usage_limit:seen_at"
# Парная метка уведомления о восстановлении. Живёт в том же состоянии, что и
# метка лимита, поэтому и решение «уже восстанавливались?» принимает storage.
_CODEX_RECOVERED_KEY = "codex_usage_limit:recovered"

# Codex не печатает зону времени сброса: в тексте ошибки есть только «11:27 AM».
# Поэтому часы нельзя выдавать за достоверные — зона приходит сверху, из
# CODEX_SESSION_TIMEZONE, и подписывается как предположение.

# Без известного времени сброса ждать нечего: ждём не меньше этого интервала
# перед следующей попыткой, чтобы восстановление не превратилось в опрос.
# Пауза отсчитывается от последнего отказа Codex, поэтому серия неудачных
# попыток не может обнулить таймер и заставить долбить модель чаще.
CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN = timedelta(minutes=30)

CODEX_LIMIT_NOTICE = (
    "Лимит Codex исчерпан. Я продолжаю принимать сообщения и распознавать голос, "
    "но не могу анализировать чаты и отвечать по существу.{reset}"
    " Пока лимит не восстановится, полезных подсказок не будет. "
    "Он восстановится автоматически: как только первая попытка пройдёт, я сообщу."
)

CODEX_RECOVERED_NOTICE = (
    "Codex снова отвечает — лимит восстановился, я снова могу анализировать чаты "
    "и предлагать ответы."
)


def codex_limit_notice(reset_hint: str) -> str:
    """Текст уведомления о лимите.

    reset_hint — уже переведённое в зону владельца время, либо пустая строка,
    если Codex не назвал время сброса."""
    if not reset_hint:
        return CODEX_LIMIT_NOTICE.format(reset="")
    # «Около», а не «в»: зона времени в ошибке Codex не указана, поэтому
    # показанный момент — пересчёт по CODEX_SESSION_TIMEZONE, а не факт.
    return CODEX_LIMIT_NOTICE.format(reset=f" По данным Codex восстановление около {reset_hint}.")


def codex_limit_reset_local(reset_hint: str, timezone_name: str, *, session_timezone_name: str) -> str:
    """Показать время сброса в зоне владельца, честно пометив источник.

    В тексте ошибки Codex зоны нет: только «try again at 11:27 AM». Значит,
    перевод держится на предположении, что Codex печатает время в зоне своей
    сессии, а сессия на этом VPS живёт по Europe/Moscow (`/etc/timezone`).
    Поэтому результат подписан и исходными часами, и используемой зоной:
    вручную владельцу спорить со временем не с чем, а автоматика опирается
    на тот же CODEX_SESSION_TIMEZONE. Если зона неизвестна, исходная подсказка
    возвращается как есть — выдумывать пересчёт нельзя."""
    if not reset_hint:
        return ""
    source_label = _zone_label(session_timezone_name)
    if source_label is None:
        return f"{reset_hint} — Codex не указал зону, время сверяется по настройкам Codex"
    parts = reset_hint.replace("UTC", "").strip().split(":")
    if len(parts) < 2:
        return reset_hint
    owner_label = _zone_label(timezone_name)
    if owner_label is None:
        return f"{reset_hint} — не удалось перевести в зону {timezone_name}, время сверяется по настройкам Codex"
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return reset_hint
        today = datetime.now(timezone.utc).astimezone(ZoneInfo(session_timezone_name))
        moment = today.replace(hour=hour, minute=minute, second=0, microsecond=0)
        local = moment.astimezone(ZoneInfo(timezone_name))
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return reset_hint
    # Здесь важно не выдать пересчёт за факт: часы пришли без зоны, поэтому
    # подпись говорит и зону владельца, и исходное время с предположением.
    return f"{local:%H:%M} {owner_label} ({_zone_offset_label(local)}, исходное «{reset_hint}» в зоне {session_timezone_name})"


def _zone_label(timezone_name: str) -> str | None:
    """Человекочитаемое имя зоны, либо None, если зона неизвестна системе."""
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None
    return timezone_name.split("/")[-1].replace("_", " ")


def _zone_offset_label(moment: datetime) -> str:
    total_minutes = int((moment.utcoffset() or timedelta(0)).total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    return f"UTC{sign}{abs(total_minutes) // 60:02d}:{abs(total_minutes) % 60:02d}"


def codex_reset_moment_utc(reset_hint: str, source_timezone_name: str, *, now: datetime | None = None) -> datetime | None:
    """Момент сброса в UTC из часов Codex, взятых в предполагаемой зоне сессии.

    Возвращает None, если разобрать нельзя, зона неизвестна или момент уже
    прошёл: в последнем случае ждать бессмысленно, и лимит проверяется сразу
    по общей паузе, а не зависает до утра."""
    if not reset_hint:
        return None
    if _zone_label(source_timezone_name) is None:
        return None
    parts = reset_hint.replace("UTC", "").strip().split(":")
    if len(parts) < 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            return None
        current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(source_timezone_name))
        moment = current.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)
    except (ValueError, ZoneInfoNotFoundError, TypeError):
        return None
    return moment if moment > (now or datetime.now(timezone.utc)) else None


def codex_limit_retry(reset_at: datetime | None, seen_at: datetime | None, *, now: datetime | None = None) -> CodexLimitRetry:
    """Можно ли уже рискнуть обычной попыткой Codex.

    Пока лимит активен, владельцу отвечают без обращения к модели. Но и навсегда
    блокировать нельзя: если лимит восстановился, Рик обязан это заметить.
    Поэтому решение принимается по сохранённым в SQLite меткам времени, а фонового
    опроса модели нет — попытка делается только на реальном запросе владельца.

    Известен момент сброса: ждём его, не тратя заведомо отказные запросы.
    Момент неизвестен: ждём ограниченную паузу, чтобы восстановление не
    превратилось в долбёжку модели."""
    moment = now or datetime.now(timezone.utc)
    if reset_at is not None:
        if moment >= reset_at:
            return CodexLimitRetry(True, "reset_time_reached", 0)
        return CodexLimitRetry(False, "before_reset_time", int((reset_at - moment).total_seconds()))
    if seen_at is None:
        return CodexLimitRetry(True, "no_probe_time_known", 0)
    wait_left = seen_at + CODEX_LIMIT_UNKNOWN_RESET_COOLDOWN - moment
    if wait_left <= timedelta(0):
        return CodexLimitRetry(True, "cooldown_elapsed", 0)
    return CodexLimitRetry(False, "cooldown", int(wait_left.total_seconds()))


@dataclass(frozen=True)
class CodexLimitRetry:
    """Решение по контрольной попытке: ждать или рискнуть.

    `reason` — короткая метка для лога и `scripts/diagnose.py`:
    reset_time_reached | before_reset_time | cooldown_elapsed | cooldown |
    no_probe_time_known."""

    allowed: bool
    reason: str
    wait_seconds: int = 0


@dataclass(frozen=True)
class CodexLimitProbeState:
    """Снимок сохранённого состояния лимита Codex из SQLite.

    `active` — владельцу уже сообщали о лимите в этом процессе или раньше.
    `reset_hint` — подпись времени для владельца. `reset_at_utc` — тот же
    момент в UTC из предполагаемой зоны сессии Codex, либо None, если время
    не названо или уже прошло. `seen_at` — когда лимит заметили в последний раз."""

    active: bool
    reset_hint: str = ""
    reset_at_utc: datetime | None = None
    seen_at: datetime | None = None


@dataclass(frozen=True)
class RecommendationRecord:
    id: int
    telegram_chat_id: int
    chat_name: str
    sender_name: str
    original_message: str
    situation: str
    suggested_reply: str
    owner_chat_id: int | None
    owner_message_id: int | None
    action: str = "reply"
    observation: str = ""
    unknowns: str = ""
    owner_question: str = ""


@dataclass(frozen=True)
class StoredMessage:
    id: int
    update_id: int
    chat_id: int
    message_id: int
    sender_id: int | None
    sender_name: str
    telegram_date: str
    text: str
    reply_to_message_id: int | None
    role: str
    processing_status: str
    media_kind: str = ""
    media_path: str = ""
    telegram_file_id: str = ""
    media_mime: str = ""
    media_filename: str = ""
    media_group_id: str = ""
    media_file_unique_id: str = ""
    forward_origin: str = ""
    download_status: str = ""
    download_error: str = ""


@dataclass(frozen=True)
class MemoryEntry:
    id: int
    content: str
    scope: str
    kind: str


@dataclass(frozen=True)
class OwnerQuestion:
    id: int
    telegram_chat_id: int
    recommendation_id: int | None
    question: str
    owner_message_id: int | None
    status: str


@dataclass(frozen=True)
class OwnerQueryPrompt:
    id: int
    question: str
    owner_message_id: int | None
    status: str
    telegram_chat_id: int | None = None
    target_chat_ids: tuple[int, ...] = ()
    time_from_utc: str | None = None
    time_to_utc: str | None = None
    time_label: str = ""
    detail_level: str = "short"


@dataclass(frozen=True)
class OwnerQuerySelection:
    id: int
    question: str
    owner_chat_id: int
    available_chat_ids: tuple[int, ...]
    selected_chat_ids: tuple[int, ...]
    mode: str
    status: str
    time_from_utc: str | None
    time_to_utc: str | None
    time_label: str
    detail_level: str
    owner_message_id: int | None = None
    created_by_user_id: int | None = None
    created_by_username: str | None = None
    created_by_name: str | None = None


@dataclass(frozen=True)
class ReminderRecord:
    id: int
    owner_chat_id: int
    remind_at_utc: str
    text: str
    related_chat_id: int | None = None
    related_chat_name: str | None = None
    created_by_user_id: int | None = None
    created_by_username: str | None = None
    created_by_name: str | None = None


@dataclass(frozen=True)
class GeneralTaskRecord:
    id: int
    owner_chat_id: int
    request_text: str
    understanding: str
    kind: str
    payload: dict
    status: str
    owner_message_id: int | None = None
    clarification_message_id: int | None = None


@dataclass(frozen=True)
class SelfRestartRecord:
    id: int
    general_task_id: int
    owner_chat_id: int
    old_pid: int
    reason: str


@dataclass(frozen=True)
class LearningDraft:
    id: int
    recommendation_id: int
    author_user_id: int
    author_name: str
    feedback: str
    understanding: str
    proposed_rule: str | None
    conflict_key: str | None
    scope: str
    regenerate_current: bool
    revision_instruction: str | None
    status: str


@dataclass(frozen=True)
class RuleRecord:
    id: int
    telegram_chat_id: int | None
    chat_name: str
    rule_text: str
    scope: str
    author_name: str
    created_at: str


@dataclass(frozen=True)
class ChatOnboarding:
    id: int
    telegram_chat_id: int
    chat_title: str
    added_by_name: str
    added_by_id: int | None
    status: str
    owner_notice_message_id: int | None = None
    owner_brief: str = ""
    draft_name: str = ""
    draft_wiki: str = ""
    draft_directory: str = ""
    draft_message_id: int | None = None
    clarification_prompt_message_id: int | None = None


@dataclass(frozen=True)
class MemoryDraft:
    id: int
    recommendation_id: int | None
    author_user_id: int
    author_name: str
    content: str
    scope: str
    project_key: str | None
    status: str
    kind: str = "fact"
    global_allowed: bool = True


class ChatThreadStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_locked(self) -> Iterator[sqlite3.Connection]:
        """Открыть транзакцию, взяв блокировку записи сразу.

        Обычная `_connect` в Python не начинает транзакцию на чтении, поэтому
        два потока успевают прочитать одно и то же состояние до записи. Здесь
        блокировка берётся до первого SELECT: без неё решение «новый лимит или
        продолжение» вычислялось бы по снимку, который второй поток уже успел
        испортить. Единственное место, где это действительно нужно, — атомарные
        проверка-и-запись метки лимита."""
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                with connection:
                    yield connection
            except BaseException:
                # `with connection` при исключении в теле блока сам откатывает
                # транзакцию, но не закрывает соединение, поэтому явный откат
                # оставлен как страховка: он безвреден, а при непривычной
                # редакции stdlib не оставит транзакцию висеть.
                connection.rollback()
                raise
        finally:
            connection.close()

    def start_run(self) -> str | None:
        """Return the previous unclean run's start time, if known."""
        with self._connect() as connection:
            previous = connection.execute("SELECT value FROM operational_state WHERE key='run_status'").fetchone()
            started = connection.execute("SELECT value FROM operational_state WHERE key='run_started_at'").fetchone()
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES('run_status', 'running') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES('run_started_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),),
            )
            return started[0] if previous is not None and previous[0] == "running" and started else None

    def stop_run(self) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE operational_state SET value='stopped' WHERE key='run_status'")
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES('run_stopped_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),),
            )

    def record_poll_success(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES('poll_success_at', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_now(),),
            )

    def queue_operational_notice(self, key: str, text: str) -> int:
        with self._connect() as connection:
            existing = connection.execute("SELECT value FROM operational_state WHERE key=?", (key,)).fetchone()
            if existing:
                return int(existing[0])
            cursor = connection.execute(
                "INSERT INTO owner_query_deliveries(text, created_at) VALUES(?, ?)", (text, _now()),
            )
            connection.execute("INSERT INTO operational_state(key, value) VALUES(?, ?)", (key, str(cursor.lastrowid)))
            logger.info("event=delivery_queued component=storage delivery_id=%s operation=operational_notice", cursor.lastrowid)
            return cursor.lastrowid

    def note_codex_usage_limit(self, reset_hint: str, *, source_timezone_name: str, local_hint: str) -> None:
        """Запомнить активный лимит и время сброса, не отправляя сообщение.

        Вызывается на каждый отказ: сам текст уведомления уходит только
        один раз, а момент сброса нужен приложению, чтобы отвечать внятно
        дальше и не повторять заведомо отказный запрос раньше времени.

        `reset_hint` — исходные часы из ошибки Codex, `source_timezone_name` —
        зона, в которой эти часы предполагаются, `local_hint` — уже
        пересчитанное время для владельца (может быть пустым, если Codex
        времени не назвал)."""
        with self._connect() as connection:
            self._write_limit_state(connection, reset_hint, source_timezone_name, local_hint)

    def claim_codex_usage_limit(
        self, reset_hint: str, notice_text: str, *, source_timezone_name: str, local_hint: str,
    ) -> bool:
        """Создать лимит и уведомить владельца, если его ещё нет.

        Возвращает True, если это новый лимит и владельцу нужно сообщение.
        Проверка метки и её запись идут в одной транзакции: два провайдера
        (клиентский и owner-контур) работают в разных потоках, и раздельные
        чтение с записью дали бы два уведомления об одном лимите.

        Локальное состояние провайдера в решение не входит намеренно: SQLite
        остаётся единственным источником истины, поэтому provider, который
        ещё помнит старый лимит, не может подавить уведомление о новом.

        Переход в новый лимит целиком атомарен: метка предыдущего
        восстановления, доставка владельцу, метка активного лимита и метки
        времени сброса либо появляются вместе, либо не появляются вовсе. Раньше
        сброс метки восстановления жил в вызывающем коде и выполнялся до
        создания лимита, поэтому падение между двумя шагами оставляло базу в
        состоянии, где следующий лимит уже не смог бы сообщить о своём
        восстановлении. При уже активном лимите метка восстановления не
        трогается: этот лимит ещё не завершился, и рано открывать ему новое
        окно для уведомления о восстановлении."""
        with self._write_locked() as connection:
            already_active = connection.execute(
                "SELECT 1 FROM operational_state WHERE key=? LIMIT 1", (_CODEX_LIMIT_KEY,),
            ).fetchone() is not None
            if not already_active:
                connection.execute("DELETE FROM operational_state WHERE key=?", (_CODEX_RECOVERED_KEY,))
                cursor = connection.execute(
                    "INSERT INTO owner_query_deliveries(text, created_at) VALUES(?, ?)", (notice_text, _now()),
                )
                connection.execute(
                    "INSERT INTO operational_state(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (_CODEX_LIMIT_KEY, str(cursor.lastrowid)),
                )
                logger.info("event=delivery_queued component=storage delivery_id=%s operation=operational_notice", cursor.lastrowid)
            self._write_limit_state(connection, reset_hint, source_timezone_name, local_hint)
            return not already_active

    def claim_codex_usage_recovered(self, notice_text: str, *, turn_started_at: str | None = None) -> bool:
        """Снять лимит и уведомить о восстановлении, если он активен.

        Возвращает True ровно один раз за лимит: проверка и снятие метки идут
        в одной транзакции, поэтому второй провайдер, который помнит старый
        лимит и позже успешно отвечает, не пришлёт дубликат и не тронет уже
        созданное новое состояние лимита.

        `turn_started_at` — момент, когда провайдер начал turn, в UTC. Это
        причинная граница, а не время завершения: turn, который стартовал до
        того как лимит возник, ничего не говорит о текущем лимите. Он мог
        уйти в модель на текущем остатке и вернуться с успехом уже после
        отказа по лимиту у соседнего turn. Снимать по такому успеху лимит
        нельзя, иначе настоящий лимит был бы стёрт успехом, который к нему
        отношения не имеет.

        Если момент старта неизвестен, причинность не проверяется: старые
        вызовы и ручные проверки не должны ломаться, а лимит снимает только
        успешный turn того же провайдера, который его и создал.
        """
        with self._write_locked() as connection:
            rows = dict(connection.execute(
                "SELECT key, value FROM operational_state WHERE key IN (?, ?)",
                (_CODEX_LIMIT_KEY, _CODEX_LIMIT_SEEN_AT_KEY),
            ))
            if _CODEX_LIMIT_KEY not in rows:
                return False
            if turn_started_at is not None and not _turn_can_see_limit(turn_started_at, rows.get(_CODEX_LIMIT_SEEN_AT_KEY, "")):
                logger.warning(
                    "event=codex_usage_limit_recovery_ignored component=storage reason=turn_started_before_limit "
                    "turn_started_at=%s limit_seen_at=%s", turn_started_at, rows.get(_CODEX_LIMIT_SEEN_AT_KEY, "UNKNOWN"),
                )
                return False
            connection.execute("DELETE FROM operational_state WHERE key=?", (_CODEX_LIMIT_KEY,))
            cursor = connection.execute(
                "INSERT INTO owner_query_deliveries(text, created_at) VALUES(?, ?)", (notice_text, _now()),
            )
            connection.execute(
                "INSERT INTO operational_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_CODEX_RECOVERED_KEY, str(cursor.lastrowid)),
            )
            for key in (_CODEX_LIMIT_REASON_KEY, _CODEX_LIMIT_RESET_AT_KEY, _CODEX_LIMIT_SEEN_AT_KEY):
                connection.execute("DELETE FROM operational_state WHERE key=?", (key,))
            logger.info("event=delivery_queued component=storage delivery_id=%s operation=operational_notice", cursor.lastrowid)
            return True

    @staticmethod
    def _write_limit_state(
        connection: sqlite3.Connection, reset_hint: str, source_timezone_name: str, local_hint: str,
    ) -> None:
        """Обновить метки лимита в уже открытой транзакции.

        Отсчёт паузы идёт от последнего отказа: сдвинутый вперёд момент сброса
        всё равно заставит ждать до него, а если время сброса неизвестно, пауза
        ограничит частоту попыток."""
        reset_at = codex_reset_moment_utc(reset_hint, source_timezone_name)
        connection.executemany(
            "INSERT INTO operational_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                (_CODEX_LIMIT_REASON_KEY, local_hint or reset_hint or ""),
                (_CODEX_LIMIT_RESET_AT_KEY, reset_at.isoformat() if reset_at else ""),
                (_CODEX_LIMIT_SEEN_AT_KEY, _now()),
            ],
        )

    def clear_operational_state(self, key: str) -> None:
        """Снять метку, чтобы следующий такой же случай снова сработал.

        Используется для парных уведомлений: восстановился лимит — и ключ
        снят, чтобы следующий лимит снова привёл ровно одно сообщение."""
        with self._connect() as connection:
            connection.execute("DELETE FROM operational_state WHERE key=?", (key,))

    def clear_codex_usage_limit(self) -> None:
        """Снять всё состояние исчерпанного лимита разом.

        Метка времени сброса и метка «когда заметили» относятся к тому же
        случаю, поэтому при восстановлении их нельзя оставлять: иначе после
        следующего перезапуска Рик решит, что лимит всё ещё действует."""
        for key in (_CODEX_LIMIT_KEY, _CODEX_LIMIT_REASON_KEY, _CODEX_LIMIT_RESET_AT_KEY, _CODEX_LIMIT_SEEN_AT_KEY):
            self.clear_operational_state(key)

    def codex_usage_limit_active(self) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM operational_state WHERE key=? LIMIT 1", (_CODEX_LIMIT_KEY,),
            ).fetchone() is not None

    def codex_usage_limit_reason(self) -> str:
        """Время сброса лимита, сохранённое вместе с уведомлением.

        Нужно, чтобы внятное объяснение не зависело от того, успеет ли
        пройти новая попытка: при уже известном лимите повторный запрос
        к Codex не делаем, а отвечаем сразу."""
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM operational_state WHERE key=?", (_CODEX_LIMIT_REASON_KEY,)).fetchone()
        return row[0] if row else ""

    def codex_usage_limit_probe_state(self) -> CodexLimitProbeState:
        """Всё сохранённое состояние лимита: для решения о попытке и диагностики.

        Возвращается целиком, чтобы решение о контрольной попытке принималось
        по одному снимку SQLite, а не по нескольким рассинхронизированным
        чтениям."""
        with self._connect() as connection:
            rows = dict(connection.execute(
                "SELECT key, value FROM operational_state WHERE key IN (?, ?, ?)",
                (_CODEX_LIMIT_KEY, _CODEX_LIMIT_RESET_AT_KEY, _CODEX_LIMIT_SEEN_AT_KEY),
            ))

        def moment(key: str) -> datetime | None:
            value = rows.get(key) or ""
            try:
                return datetime.fromisoformat(value) if value else None
            except ValueError:
                return None

        return CodexLimitProbeState(
            active=_CODEX_LIMIT_KEY in rows,
            reset_hint=self.codex_usage_limit_reason(),
            reset_at_utc=moment(_CODEX_LIMIT_RESET_AT_KEY),
            seen_at=moment(_CODEX_LIMIT_SEEN_AT_KEY),
        )

    def codex_usage_limit_retry(self, *, now: datetime | None = None) -> CodexLimitRetry:
        """Можно ли уже рискнуть обычной попыткой Codex при активном лимите.

        Таймеры сохранены в SQLite, поэтому перезапуск процесса не обнуляет
        паузу и не превращает её в поток заведомо отказных запросов."""
        state = self.codex_usage_limit_probe_state()
        return codex_limit_retry(state.reset_at_utc, state.seen_at, now=now)

    def record_operational_event(self, event: str, level: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO operational_events(event, level, created_at) VALUES(?, ?, ?)",
                (event, level, _now()),
            )
            connection.execute("DELETE FROM operational_events WHERE created_at<?",
                               ((datetime.now(timezone.utc) - timedelta(days=7)).isoformat(),))

    def queue_daily_report(self, report_date: str, start_utc: str, end_utc: str) -> int:
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT delivery_id FROM daily_reports WHERE report_date=?", (report_date,),
            ).fetchone()
            if existing:
                return existing[0]
            def count(sql: str) -> int:
                return connection.execute(sql, (start_utc, end_utc)).fetchone()[0]
            # Считаем всю подтверждённую память, а не только kind='fact':
            # факт — лишь один из видов (decision, commitment, preference,
            # open_question, rule, assumption, experience), и подтверждённое
            # решение или договорённость для владельца так же ценны, как факт.
            # Черновики отсеиваются статусом: в memory_entries попадает только
            # подтверждённая запись, отклонённые остаются в memory_drafts.
            memory = count("SELECT count(*) FROM memory_entries WHERE status='active' AND created_at>=? AND created_at<?")
            rules = count("SELECT count(*) FROM learning_rules WHERE status='active' AND created_at>=? AND created_at<?")
            experience = count("SELECT count(*) FROM experience_entries WHERE status='active' AND created_at>=? AND created_at<?")
            errors = count("SELECT count(*) FROM operational_events WHERE level IN ('ERROR','CRITICAL') AND created_at>=? AND created_at<?")
            restarts = count("SELECT count(*) FROM operational_events WHERE event='previous_run_unclean' AND created_at>=? AND created_at<?")
            pending = connection.execute("SELECT count(*) FROM telegram_messages WHERE processing_status IN ('pending','processing')").fetchone()[0]
            pending += connection.execute("SELECT count(*) FROM owner_query_deliveries WHERE owner_message_id IS NULL").fetchone()[0]
            text = (f"Рик на связи. Отчёт за {report_date}.\n"
                    f"Новых записей памяти: {memory}; правил: {rules}; опыта: {experience}.\n"
                    f"Техника: ошибок {errors}; восстановлений после сбоя {restarts}; сейчас pending {pending}." +
                    ("\nЗа вчера нового ничего не узнал." if not any((memory, rules, experience)) else ""))
            cursor = connection.execute("INSERT INTO owner_query_deliveries(text, created_at) VALUES(?, ?)", (text, _now()))
            connection.execute("INSERT INTO daily_reports(report_date, delivery_id, created_at) VALUES(?, ?, ?)",
                               (report_date, cursor.lastrowid, _now()))
            logger.info("event=daily_report_queued component=storage report_date=%s delivery_id=%s", report_date, cursor.lastrowid)
            return cursor.lastrowid

    def last_daily_report_date(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT max(report_date) FROM daily_reports").fetchone()
            return row[0]

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_threads (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    codex_thread_id TEXT,
                    agent_provider TEXT NOT NULL,
                    prompt_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_query_threads (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    codex_thread_id TEXT NOT NULL,
                    agent_provider TEXT NOT NULL,
                    prompt_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sepia_threads (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    logical_name TEXT NOT NULL,
                    codex_thread_id TEXT NOT NULL,
                    agent_provider TEXT NOT NULL,
                    prompt_version INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS processed_updates (
                    telegram_update_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recommendations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    original_message TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    suggested_reply TEXT NOT NULL,
                    owner_chat_id INTEGER,
                    owner_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner_chat_id, owner_message_id)
                );
                CREATE TABLE IF NOT EXISTS learning_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id INTEGER NOT NULL,
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    feedback TEXT NOT NULL,
                    understanding TEXT NOT NULL,
                    proposed_rule TEXT,
                    conflict_key TEXT,
                    scope TEXT NOT NULL CHECK(scope IN ('client', 'global')),
                    regenerate_current INTEGER NOT NULL,
                    revision_instruction TEXT,
                    status TEXT NOT NULL,
                    clarification_prompt_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE TABLE IF NOT EXISTS learning_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    chat_name TEXT NOT NULL,
                    rule_text TEXT NOT NULL,
                    conflict_key TEXT,
                    scope TEXT NOT NULL CHECK(scope IN ('client', 'global')),
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    source_draft_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    superseded_at TEXT,
                    FOREIGN KEY(source_draft_id) REFERENCES learning_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_recommendation_owner_message
                    ON recommendations(owner_chat_id, owner_message_id);
                CREATE INDEX IF NOT EXISTS idx_active_rules
                    ON learning_rules(status, telegram_chat_id);
                CREATE TABLE IF NOT EXISTS internal_context_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    chat_name TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_internal_context_chat
                    ON internal_context_messages(telegram_chat_id, id DESC);
                CREATE TABLE IF NOT EXISTS memory_drafts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recommendation_id INTEGER,
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
                    project_key TEXT,
                    kind TEXT NOT NULL DEFAULT 'fact',
                    global_allowed INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE TABLE IF NOT EXISTS memory_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    project_key TEXT,
                    content TEXT NOT NULL,
                    scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
                    author_user_id INTEGER NOT NULL,
                    author_name TEXT NOT NULL,
                    source_draft_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_draft_id) REFERENCES memory_drafts(id)
                );
                CREATE INDEX IF NOT EXISTS idx_active_memory
                    ON memory_entries(status, scope, telegram_chat_id, project_key);
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    update_id INTEGER NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    sender_id INTEGER,
                    sender_name TEXT NOT NULL,
                    telegram_date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    reply_to_message_id INTEGER,
                    role TEXT NOT NULL,
                    processing_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(chat_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_pending
                    ON telegram_messages(chat_id, processing_status, id);
                CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_date
                    ON telegram_messages(chat_id, telegram_date, id);
                CREATE TABLE IF NOT EXISTS chat_states (
                    telegram_chat_id INTEGER PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owner_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL,
                    recommendation_id INTEGER,
                    question TEXT NOT NULL,
                    owner_message_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_owner_questions_message
                    ON owner_questions(owner_message_id, status);
                CREATE TABLE IF NOT EXISTS owner_query_prompts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    owner_message_id INTEGER,
                    telegram_chat_id INTEGER,
                    target_chat_ids TEXT NOT NULL DEFAULT '[]',
                    time_from_utc TEXT,
                    time_to_utc TEXT,
                    time_label TEXT NOT NULL DEFAULT '',
                    detail_level TEXT NOT NULL DEFAULT 'short',
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_query_prompts_message
                    ON owner_query_prompts(owner_message_id, status);
                CREATE TABLE IF NOT EXISTS owner_query_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    prompt_id INTEGER,
                    selection_id INTEGER,
                    owner_message_id INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_query_deliveries_pending
                    ON owner_query_deliveries(owner_message_id);
                CREATE TABLE IF NOT EXISTS operational_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operational_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    level TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_reports (
                    report_date TEXT PRIMARY KEY,
                    delivery_id INTEGER NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(delivery_id) REFERENCES owner_query_deliveries(id)
                );
                CREATE TABLE IF NOT EXISTS owner_query_selections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    owner_chat_id INTEGER NOT NULL,
                    available_chat_ids TEXT NOT NULL,
                    selected_chat_ids TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    time_from_utc TEXT,
                    time_to_utc TEXT,
                    time_label TEXT NOT NULL DEFAULT '',
                    detail_level TEXT NOT NULL DEFAULT 'short',
                    owner_message_id INTEGER,
                    created_by_user_id INTEGER,
                    created_by_username TEXT,
                    created_by_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_query_selections_status
                    ON owner_query_selections(owner_chat_id, status);
                CREATE TABLE IF NOT EXISTS owner_delivery_parts (
                    owner_chat_id INTEGER NOT NULL,
                    delivery_key TEXT NOT NULL,
                    part_index INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    owner_message_id INTEGER,
                    PRIMARY KEY(owner_chat_id, delivery_key, part_index),
                    UNIQUE(owner_chat_id, owner_message_id)
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_chat_id INTEGER NOT NULL,
                    remind_at_utc TEXT NOT NULL,
                    text TEXT NOT NULL,
                    sent_at TEXT,
                    related_chat_id INTEGER,
                    related_chat_name TEXT,
                    created_by_user_id INTEGER,
                    created_by_username TEXT,
                    created_by_name TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(owner_chat_id, sent_at, remind_at_utc);
                CREATE TABLE IF NOT EXISTS owner_general_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_chat_id INTEGER NOT NULL,
                    request_text TEXT NOT NULL,
                    understanding TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL,
                    owner_message_id INTEGER,
                    clarification_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_owner_general_tasks_message
                    ON owner_general_tasks(owner_chat_id, owner_message_id, clarification_message_id, status);
                CREATE TABLE IF NOT EXISTS self_restarts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    general_task_id INTEGER NOT NULL UNIQUE,
                    owner_chat_id INTEGER NOT NULL,
                    old_pid INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_self_restarts_pending
                    ON self_restarts(status, old_pid, id);
                CREATE TABLE IF NOT EXISTS experience_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER,
                    chat_name TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'experience',
                    source_draft_id INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experience_chat
                    ON experience_entries(status, telegram_chat_id, id);
                CREATE TABLE IF NOT EXISTS chat_onboardings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_chat_id INTEGER NOT NULL UNIQUE,
                    chat_title TEXT NOT NULL,
                    added_by_name TEXT NOT NULL DEFAULT '',
                    added_by_id INTEGER,
                    status TEXT NOT NULL,
                    owner_notice_message_id INTEGER,
                    owner_brief TEXT NOT NULL DEFAULT '',
                    draft_name TEXT NOT NULL DEFAULT '',
                    draft_wiki TEXT NOT NULL DEFAULT '',
                    draft_directory TEXT NOT NULL DEFAULT '',
                    draft_message_id INTEGER,
                    clarification_prompt_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_onboarding_notice
                    ON chat_onboardings(owner_notice_message_id);
                CREATE INDEX IF NOT EXISTS idx_onboarding_draft_message
                    ON chat_onboardings(draft_message_id);
                """
            )
            self._upgrade_schema(connection)
            connection.execute(
                "UPDATE telegram_messages SET processing_status='pending' WHERE processing_status='processing'"
            )
            connection.execute(
                "UPDATE owner_query_selections SET status='selecting', updated_at=? WHERE status='processing'",
                (_now(),),
            )
            connection.execute(
                "UPDATE owner_general_tasks SET status='failed', updated_at=? WHERE status='executing'",
                (_now(),),
            )

    @staticmethod
    def _upgrade_schema(connection: sqlite3.Connection) -> None:
        columns = {
            "recommendations": (
                ("action", "TEXT NOT NULL DEFAULT 'reply'"),
                ("observation", "TEXT NOT NULL DEFAULT ''"),
                ("unknowns", "TEXT NOT NULL DEFAULT ''"),
                ("owner_question", "TEXT NOT NULL DEFAULT ''"),
            ),
            "memory_drafts": (
                ("kind", "TEXT NOT NULL DEFAULT 'fact'"),
                ("global_allowed", "INTEGER NOT NULL DEFAULT 1"),
            ),
            "memory_entries": (("kind", "TEXT NOT NULL DEFAULT 'fact'"),),
            "chat_threads": (("prompt_version", "INTEGER"),),
            "owner_query_prompts": (
                ("telegram_chat_id", "INTEGER"),
                ("target_chat_ids", "TEXT NOT NULL DEFAULT '[]'"),
                ("time_from_utc", "TEXT"),
                ("time_to_utc", "TEXT"),
                ("time_label", "TEXT NOT NULL DEFAULT ''"),
                ("detail_level", "TEXT NOT NULL DEFAULT 'short'"),
            ),
            "owner_query_deliveries": (("selection_id", "INTEGER"), ("general_task_id", "INTEGER")),
            "reminders": (
                ("related_chat_id", "INTEGER"),
                ("related_chat_name", "TEXT"),
                ("created_by_user_id", "INTEGER"),
                ("created_by_username", "TEXT"),
                ("created_by_name", "TEXT"),
            ),
            "owner_query_selections": (
                ("created_by_user_id", "INTEGER"),
                ("created_by_username", "TEXT"),
                ("created_by_name", "TEXT"),
            ),
            "telegram_messages": (
                ("media_kind", "TEXT NOT NULL DEFAULT ''"),
                ("media_path", "TEXT NOT NULL DEFAULT ''"),
                ("telegram_file_id", "TEXT NOT NULL DEFAULT ''"),
                ("media_mime", "TEXT NOT NULL DEFAULT ''"),
                ("media_filename", "TEXT NOT NULL DEFAULT ''"),
                ("media_group_id", "TEXT NOT NULL DEFAULT ''"),
                ("media_file_unique_id", "TEXT NOT NULL DEFAULT ''"),
                ("forward_origin", "TEXT NOT NULL DEFAULT ''"),
                ("download_status", "TEXT NOT NULL DEFAULT ''"),
                ("download_error", "TEXT NOT NULL DEFAULT ''"),
            ),
        }
        for table, specs in columns.items():
            existing = {
                row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            added: set[str] = set()
            for name, definition in specs:
                if name not in existing:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
                    added.add(name)
            if table == "memory_drafts" and "global_allowed" in added:
                connection.execute(
                    """UPDATE memory_drafts SET global_allowed=
                    CASE WHEN scope='global' THEN 1 ELSE 0 END"""
                )
        ChatThreadStore._ensure_memory_drafts_allow_unlinked(connection)

    @staticmethod
    def _ensure_memory_drafts_allow_unlinked(connection: sqlite3.Connection) -> None:
        info = list(connection.execute("PRAGMA table_info(memory_drafts)"))
        if not info:
            return
        rec = next((row for row in info if row[1] == "recommendation_id"), None)
        if rec is None or int(rec[3]) == 0:
            return
        connection.execute(
            """CREATE TABLE memory_drafts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recommendation_id INTEGER,
                author_user_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                content TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
                project_key TEXT,
                kind TEXT NOT NULL DEFAULT 'fact',
                global_allowed INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(recommendation_id) REFERENCES recommendations(id)
            )"""
        )
        existing = [row[1] for row in info]
        copied = [name for name in existing if name != "id"]
        columns = ", ".join(["id", *copied])
        connection.execute(
            f"INSERT INTO memory_drafts_new ({columns}) SELECT {columns} FROM memory_drafts"
        )
        connection.execute("DROP TABLE memory_drafts")
        connection.execute("ALTER TABLE memory_drafts_new RENAME TO memory_drafts")

    def get_thread_id(self, telegram_chat_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT codex_thread_id FROM chat_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else row["codex_thread_id"]

    def get_thread_prompt_version(self, telegram_chat_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT prompt_version FROM chat_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        if row is None or row["prompt_version"] is None:
            return None
        return int(row["prompt_version"])

    def save_thread(
        self,
        telegram_chat_id: int,
        logical_name: str,
        codex_thread_id: str,
        agent_provider: str = "codex",
        prompt_version: int | None = None,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_threads (
                    telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    logical_name=excluded.logical_name, codex_thread_id=excluded.codex_thread_id,
                    agent_provider=excluded.agent_provider, prompt_version=excluded.prompt_version,
                    updated_at=excluded.updated_at
                """,
                (telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, now, now),
            )

    def get_owner_query_thread_id(self, telegram_chat_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT codex_thread_id FROM owner_query_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else row["codex_thread_id"]

    def get_sepia_thread_id(self, telegram_chat_id: int) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT codex_thread_id FROM sepia_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else row["codex_thread_id"]

    def get_sepia_thread_prompt_version(self, telegram_chat_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT prompt_version FROM sepia_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        if row is None or row["prompt_version"] is None:
            return None
        return int(row["prompt_version"])

    def save_sepia_thread(
        self,
        telegram_chat_id: int,
        logical_name: str,
        codex_thread_id: str,
        agent_provider: str = "codex",
        prompt_version: int | None = None,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sepia_threads (
                    telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    logical_name=excluded.logical_name, codex_thread_id=excluded.codex_thread_id,
                    agent_provider=excluded.agent_provider, prompt_version=excluded.prompt_version,
                    updated_at=excluded.updated_at
                """,
                (telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, now, now),
            )

    def get_owner_query_thread_prompt_version(self, telegram_chat_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT prompt_version FROM owner_query_threads WHERE telegram_chat_id = ?",
                (telegram_chat_id,),
            ).fetchone()
        if row is None or row["prompt_version"] is None:
            return None
        return int(row["prompt_version"])

    def save_owner_query_thread(
        self,
        telegram_chat_id: int,
        logical_name: str,
        codex_thread_id: str,
        agent_provider: str = "codex",
        prompt_version: int | None = None,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO owner_query_threads (
                    telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    logical_name=excluded.logical_name, codex_thread_id=excluded.codex_thread_id,
                    agent_provider=excluded.agent_provider, prompt_version=excluded.prompt_version,
                    updated_at=excluded.updated_at
                """,
                (telegram_chat_id, logical_name, codex_thread_id, agent_provider, prompt_version, now, now),
            )

    def is_update_processed(self, telegram_update_id: int) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM processed_updates WHERE telegram_update_id = ?",
                (telegram_update_id,),
            ).fetchone()
            if row is not None:
                return True
            inbox = connection.execute(
                "SELECT processing_status FROM telegram_messages WHERE update_id=?",
                (telegram_update_id,),
            ).fetchone()
        return inbox is not None and inbox["processing_status"] == "processed"

    def mark_update_processed(self, telegram_update_id: int) -> None:
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO processed_updates VALUES (?, ?)", (telegram_update_id, _now()))
            connection.execute(
                """UPDATE telegram_messages SET processing_status='processed'
                WHERE update_id=? AND processing_status IN ('ignored', 'pending', 'processed')""",
                (telegram_update_id,),
            )

    def claim_update_processed(self, telegram_update_id: int) -> bool:
        """Atomically claim an owner-only update before creating durable UI state."""
        with self._connect() as connection:
            result = connection.execute(
                "INSERT OR IGNORE INTO processed_updates VALUES (?, ?)",
                (telegram_update_id, _now()),
            )
        return result.rowcount == 1

    def record_internal_context(self, telegram_chat_id: int, chat_name: str, sender_name: str, message_text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO internal_context_messages
                (telegram_chat_id, chat_name, sender_name, message_text, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (telegram_chat_id, chat_name, sender_name, message_text, _now()),
            )

    def recent_internal_context(self, telegram_chat_id: int, limit: int = 8) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT sender_name, message_text FROM internal_context_messages
                WHERE telegram_chat_id=? ORDER BY id DESC LIMIT ?""",
                (telegram_chat_id, limit),
            ).fetchall()
        return [f"{row['sender_name']}: {row['message_text']}" for row in reversed(rows)]

    def create_recommendation(
        self,
        telegram_chat_id: int,
        chat_name: str,
        sender_name: str,
        original_message: str,
        situation: str,
        suggested_reply: str,
        owner_chat_id: int | None = None,
        action: str = "reply",
        observation: str = "",
        unknowns: str = "",
        owner_question: str = "",
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO recommendations
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, action, observation, unknowns, owner_question, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (telegram_chat_id, chat_name, sender_name, original_message, situation, suggested_reply,
                 owner_chat_id, action, observation, unknowns, owner_question, _now()),
            )
            return int(cursor.lastrowid)

    def pending_recommendations(self, owner_chat_id: int) -> list[RecommendationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM recommendations
                WHERE owner_chat_id=? AND owner_message_id IS NULL
                ORDER BY id""",
                (owner_chat_id,),
            ).fetchall()
        return [self._recommendation(row) for row in rows]

    def prepare_owner_delivery_parts(
        self, owner_chat_id: int, delivery_key: str, texts: list[str],
    ) -> list[tuple[str, int | None]]:
        """Freeze the original split, including across retries/code upgrades."""
        with self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM owner_delivery_parts WHERE owner_chat_id=? AND delivery_key=? LIMIT 1",
                (owner_chat_id, delivery_key),
            ).fetchone()
            if exists is None:
                connection.executemany(
                    "INSERT INTO owner_delivery_parts (owner_chat_id, delivery_key, part_index, text) VALUES (?, ?, ?, ?)",
                    [(owner_chat_id, delivery_key, index, text) for index, text in enumerate(texts)],
                )
            rows = connection.execute(
                "SELECT text, owner_message_id FROM owner_delivery_parts WHERE owner_chat_id=? AND delivery_key=? ORDER BY part_index",
                (owner_chat_id, delivery_key),
            ).fetchall()
        return [(row["text"], row["owner_message_id"]) for row in rows]

    def record_owner_delivery_part(
        self, owner_chat_id: int, delivery_key: str, part_index: int, owner_message_id: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE owner_delivery_parts SET owner_message_id=?
                WHERE owner_chat_id=? AND delivery_key=? AND part_index=? AND owner_message_id IS NULL""",
                (owner_message_id, owner_chat_id, delivery_key, part_index),
            )

    def resolve_owner_reply(self, owner_chat_id: int, owner_message_id: int) -> int:
        """All parts of a completed send refer to its final, linked message."""
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT owner_message_id FROM owner_delivery_parts
                WHERE owner_chat_id=? AND delivery_key=(
                    SELECT delivery_key FROM owner_delivery_parts
                    WHERE owner_chat_id=? AND owner_message_id=?
                ) ORDER BY part_index""",
                (owner_chat_id, owner_chat_id, owner_message_id),
            ).fetchall()
        if rows and all(row["owner_message_id"] is not None for row in rows):
            return rows[-1]["owner_message_id"]
        return owner_message_id

    def attach_owner_message(self, recommendation_id: int, owner_chat_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE recommendations SET owner_chat_id=?, owner_message_id=? WHERE id=?",
                (owner_chat_id, owner_message_id, recommendation_id),
            )
            connection.execute(
                """UPDATE owner_questions SET owner_message_id=?
                WHERE recommendation_id=? AND owner_message_id IS NULL""",
                (owner_message_id, recommendation_id),
            )

    def assign_unowned_pending_recommendations(self, owner_chat_id: int) -> None:
        """Recover rows created before durable owner delivery was introduced."""
        with self._connect() as connection:
            connection.execute(
                """UPDATE recommendations SET owner_chat_id=?
                WHERE owner_chat_id IS NULL AND owner_message_id IS NULL""",
                (owner_chat_id,),
            )

    def get_recommendation_by_owner_message(self, owner_chat_id: int, owner_message_id: int) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM recommendations WHERE owner_chat_id=? AND owner_message_id=?",
                (owner_chat_id, owner_message_id),
            ).fetchone()
        return None if row is None else self._recommendation(row)

    def get_recommendation(self, recommendation_id: int) -> RecommendationRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recommendations WHERE id=?", (recommendation_id,)).fetchone()
        return None if row is None else self._recommendation(row)

    @staticmethod
    def _recommendation(row: sqlite3.Row) -> RecommendationRecord:
        keys = set(row.keys())
        return RecommendationRecord(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"], chat_name=row["chat_name"],
            sender_name=row["sender_name"], original_message=row["original_message"], situation=row["situation"],
            suggested_reply=row["suggested_reply"], owner_chat_id=row["owner_chat_id"],
            owner_message_id=row["owner_message_id"],
            action=row["action"] if "action" in keys and row["action"] else "reply",
            observation=row["observation"] if "observation" in keys and row["observation"] else "",
            unknowns=row["unknowns"] if "unknowns" in keys and row["unknowns"] else "",
            owner_question=row["owner_question"] if "owner_question" in keys and row["owner_question"] else "",
        )

    def create_learning_draft(self, recommendation_id: int, author_user_id: int, author_name: str, feedback: str, analysis) -> LearningDraft:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO learning_drafts
                (recommendation_id, author_user_id, author_name, feedback, understanding, proposed_rule,
                 conflict_key, scope, regenerate_current, revision_instruction, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (recommendation_id, author_user_id, author_name, feedback, analysis.understanding,
                 analysis.proposed_rule, analysis.conflict_key, analysis.scope, int(analysis.regenerate_current),
                 analysis.revision_instruction, now, now),
            )
            draft_id = int(cursor.lastrowid)
        return self.get_learning_draft(draft_id)  # type: ignore[return-value]

    def get_learning_draft(self, draft_id: int) -> LearningDraft | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM learning_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            return None
        return LearningDraft(
            id=row["id"], recommendation_id=row["recommendation_id"], author_user_id=row["author_user_id"],
            author_name=row["author_name"], feedback=row["feedback"], understanding=row["understanding"],
            proposed_rule=row["proposed_rule"], conflict_key=row["conflict_key"], scope=row["scope"],
            regenerate_current=bool(row["regenerate_current"]), revision_instruction=row["revision_instruction"],
            status=row["status"],
        )

    def replace_learning_draft_analysis(self, draft_id: int, feedback: str, analysis) -> LearningDraft:
        with self._connect() as connection:
            connection.execute(
                """UPDATE learning_drafts SET feedback=?, understanding=?, proposed_rule=?, conflict_key=?,
                scope=?, regenerate_current=?, revision_instruction=?, status='pending',
                clarification_prompt_message_id=NULL, updated_at=? WHERE id=?""",
                (feedback, analysis.understanding, analysis.proposed_rule, analysis.conflict_key, analysis.scope,
                 int(analysis.regenerate_current), analysis.revision_instruction, _now(), draft_id),
            )
        return self.get_learning_draft(draft_id)  # type: ignore[return-value]

    def mark_draft_awaiting_clarification(self, draft_id: int, prompt_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE learning_drafts SET status='clarifying', clarification_prompt_message_id=?, updated_at=? WHERE id=?",
                (prompt_message_id, _now(), draft_id),
            )

    def get_draft_by_clarification_prompt(self, prompt_message_id: int) -> LearningDraft | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM learning_drafts WHERE clarification_prompt_message_id=? AND status='clarifying'",
                (prompt_message_id,),
            ).fetchone()
        return None if row is None else self.get_learning_draft(row["id"])

    def confirm_draft(self, draft_id: int) -> bool:
        draft = self.get_learning_draft(draft_id)
        if draft is None or draft.status != "pending":
            return False
        recommendation = self.get_recommendation(draft.recommendation_id)
        if recommendation is None:
            return False
        now = _now()
        with self._connect() as connection:
            claimed = connection.execute(
                "UPDATE learning_drafts SET status='confirming', updated_at=? WHERE id=? AND status='pending'",
                (now, draft_id),
            )
            if claimed.rowcount != 1:
                return False
            if draft.proposed_rule:
                target_chat_id = None if draft.scope == "global" else recommendation.telegram_chat_id
                if draft.conflict_key:
                    connection.execute(
                        """UPDATE learning_rules SET status='superseded', superseded_at=?
                        WHERE status='active' AND scope=? AND conflict_key=?
                        AND ((telegram_chat_id IS NULL AND ? IS NULL) OR telegram_chat_id=?)""",
                        (now, draft.scope, draft.conflict_key, target_chat_id, target_chat_id),
                    )
                connection.execute(
                    """INSERT INTO learning_rules
                    (telegram_chat_id, chat_name, rule_text, conflict_key, scope, author_user_id,
                     author_name, source_draft_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (target_chat_id, recommendation.chat_name, draft.proposed_rule, draft.conflict_key,
                     draft.scope, draft.author_user_id, draft.author_name, draft.id, now),
                )
            connection.execute("UPDATE learning_drafts SET status='confirmed', updated_at=? WHERE id=?", (now, draft_id))
        return True

    def active_rule_texts(self, telegram_chat_id: int, *, include_global: bool = True) -> list[str]:
        with self._connect() as connection:
            if include_global:
                rows = connection.execute(
                    """SELECT rule_text FROM learning_rules WHERE status='active'
                    AND (scope='global' OR telegram_chat_id=?) ORDER BY id""",
                    (telegram_chat_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT rule_text FROM learning_rules WHERE status='active'
                    AND scope != 'global' AND telegram_chat_id=? ORDER BY id""",
                    (telegram_chat_id,),
                ).fetchall()
        return [row["rule_text"] for row in rows]

    def list_active_rules(self) -> list[RuleRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM learning_rules WHERE status='active' ORDER BY id DESC").fetchall()
        return [RuleRecord(row["id"], row["telegram_chat_id"], row["chat_name"], row["rule_text"], row["scope"], row["author_name"], row["created_at"]) for row in rows]

    def undo_latest_rule(self) -> RuleRecord | None:
        rules = self.list_active_rules()
        if not rules:
            return None
        rule = rules[0]
        with self._connect() as connection:
            connection.execute("UPDATE learning_rules SET status='undone', superseded_at=? WHERE id=?", (_now(), rule.id))
            if rule.scope and rule.id:
                current = connection.execute("SELECT conflict_key FROM learning_rules WHERE id=?", (rule.id,)).fetchone()
                conflict_key = current["conflict_key"] if current else None
                if conflict_key:
                    previous = connection.execute(
                        """SELECT id FROM learning_rules
                        WHERE status='superseded' AND scope=? AND conflict_key=?
                        AND ((telegram_chat_id IS NULL AND ? IS NULL) OR telegram_chat_id=?)
                        ORDER BY id DESC LIMIT 1""",
                        (rule.scope, conflict_key, rule.telegram_chat_id, rule.telegram_chat_id),
                    ).fetchone()
                    if previous:
                        connection.execute(
                            "UPDATE learning_rules SET status='active', superseded_at=NULL WHERE id=?",
                            (previous["id"],),
                        )
        return rule

    def create_memory_draft(
        self, recommendation_id: int | None, author_user_id: int, author_name: str,
        content: str, scope: str, project_key: str | None, kind: str = "fact",
        global_allowed: bool = True,
    ) -> MemoryDraft:
        now = _now()
        kind = kind if kind in MEMORY_KINDS else "fact"
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO memory_drafts
                (recommendation_id, author_user_id, author_name, content, scope, project_key, kind,
                 global_allowed, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (recommendation_id, author_user_id, author_name, content, scope, project_key, kind, int(global_allowed), now, now),
            )
            draft_id = int(cursor.lastrowid)
        return self.get_memory_draft(draft_id)  # type: ignore[return-value]

    def get_memory_draft(self, draft_id: int) -> MemoryDraft | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM memory_drafts WHERE id=?", (draft_id,)).fetchone()
        if row is None:
            return None
        keys = set(row.keys())
        return MemoryDraft(
            id=row["id"], recommendation_id=row["recommendation_id"], author_user_id=row["author_user_id"],
            author_name=row["author_name"], content=row["content"], scope=row["scope"],
            project_key=row["project_key"], status=row["status"],
            kind=row["kind"] if "kind" in keys and row["kind"] else "fact",
            global_allowed=bool(row["global_allowed"]) if "global_allowed" in keys else True,
        )

    def confirm_memory_draft(self, draft_id: int, scope: str | None = None) -> MemoryDraft | None:
        draft = self.get_memory_draft(draft_id)
        if draft is None or draft.status != "pending":
            return None
        target_scope = scope or draft.scope
        if target_scope not in {"chat", "project", "global"}:
            return None
        if target_scope == "global" and not draft.global_allowed:
            return None
        recommendation = (
            self.get_recommendation(draft.recommendation_id)
            if draft.recommendation_id is not None else None
        )
        if recommendation is None and target_scope != "global":
            return None
        if target_scope == "project" and not draft.project_key:
            return None
        with self._connect() as connection:
            claimed = connection.execute(
                """UPDATE memory_drafts SET scope=?, project_key=?, status='confirming', updated_at=?
                WHERE id=? AND status='pending'""",
                (target_scope, draft.project_key if target_scope == "project" else None, _now(), draft_id),
            )
            if claimed.rowcount != 1:
                return None
            chat_id = recommendation.telegram_chat_id if target_scope == "chat" and recommendation is not None else None
            connection.execute(
                """INSERT INTO memory_entries
                (telegram_chat_id, project_key, content, scope, kind, author_user_id, author_name, source_draft_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                (chat_id, draft.project_key if target_scope == "project" else None, draft.content, target_scope, draft.kind, draft.author_user_id,
                 draft.author_name, draft.id, _now()),
            )
            connection.execute("UPDATE memory_drafts SET status='confirmed', updated_at=? WHERE id=?", (_now(), draft_id))
        return self.get_memory_draft(draft_id)

    def reject_memory_draft(self, draft_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE memory_drafts SET status='rejected', updated_at=? WHERE id=? AND status='pending'",
                (_now(), draft_id),
            )
        return result.rowcount == 1

    def active_memory_texts(self, telegram_chat_id: int, project_key: str | None) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT content FROM memory_entries WHERE status='active' AND (
                    scope='global' OR (scope='chat' AND telegram_chat_id=?)
                    OR (scope='project' AND project_key=?)
                ) ORDER BY id""",
                (telegram_chat_id, project_key),
            ).fetchall()
        return [row["content"] for row in rows]

    def active_memory_entries(self, telegram_chat_id: int, project_key: str | None) -> list[MemoryEntry]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, content, scope, kind FROM memory_entries WHERE status='active' AND (
                    scope='global' OR (scope='chat' AND telegram_chat_id=?)
                    OR (scope='project' AND project_key=?)
                ) ORDER BY id""",
                (telegram_chat_id, project_key),
            ).fetchall()
        return [
            MemoryEntry(
                id=row["id"],
                content=row["content"],
                scope=row["scope"],
                kind=row["kind"] if "kind" in row.keys() and row["kind"] else "fact",
            )
            for row in rows
        ]

    def set_memory_entry_kind(self, entry_id: int, kind: str) -> bool:
        if kind not in MEMORY_KINDS:
            return False
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE memory_entries SET kind=? WHERE id=? AND status='active'",
                (kind, entry_id),
            )
        return result.rowcount == 1

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
        role: str,
        processing_status: str,
        media_kind: str = "",
        media_path: str = "",
        telegram_file_id: str = "",
        media_mime: str = "",
        media_filename: str = "",
        media_group_id: str = "",
        media_file_unique_id: str = "",
        forward_origin: str = "",
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO telegram_messages (
                    update_id, chat_id, message_id, sender_id, sender_name, telegram_date,
                    text, reply_to_message_id, role, processing_status, created_at,
                    media_kind, media_path, telegram_file_id, media_mime, media_filename, media_group_id,
                    media_file_unique_id, forward_origin, download_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    update_id, chat_id, message_id, sender_id, sender_name, telegram_date,
                    text, reply_to_message_id, role, processing_status, _now(),
                    media_kind, media_path, telegram_file_id, media_mime, media_filename, media_group_id,
                    media_file_unique_id, forward_origin, "available" if media_path else "pending" if telegram_file_id else "",
                ),
            )
            return cursor.rowcount == 1

    def set_media_path(self, message_id: int, media_path: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_messages SET media_path=?, download_status='available', download_error='' WHERE id=?",
                (media_path, message_id),
            )

    def set_media_download_failure(self, message_id: int, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_messages SET media_path='', download_status='download_failed', download_error=? WHERE id=?",
                (error, message_id),
            )

    def list_chat_attachments(self, chat_id: int) -> list[StoredMessage]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM telegram_messages WHERE chat_id=? AND media_kind='document' ORDER BY telegram_date, id",
                (chat_id,),
            ).fetchall()
        return [self._stored_message(row) for row in rows]

    def retained_document_paths(self) -> set[str]:
        with self._connect() as connection:
            return {str(row[0]) for row in connection.execute(
                "SELECT media_path FROM telegram_messages WHERE media_kind='document' AND media_path<>''"
            )}

    def owner_sender_seen(self, owner_chat_id: int | None, sender_id: int | None) -> bool:
        if owner_chat_id is None or sender_id is None:
            return False
        if owner_chat_id == sender_id:
            return True
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM telegram_messages WHERE chat_id=? AND sender_id=? AND role='owner' LIMIT 1",
                (owner_chat_id, sender_id),
            ).fetchone() is not None

    def set_message_text(self, update_id: int, text: str) -> bool:
        """Записывает распознанный текст голосового; только пока сообщение pending."""
        with self._connect() as connection:
            result = connection.execute(
                """UPDATE telegram_messages SET text=? WHERE update_id=?
                AND processing_status='pending'""",
                (text, update_id),
            )
        return result.rowcount == 1

    def clear_media_paths(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as connection:
            connection.execute(
                f"UPDATE telegram_messages SET media_path='' WHERE id IN ({placeholders})",
                message_ids,
            )

    def reset_stale_processing(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE telegram_messages SET processing_status='pending' WHERE processing_status='processing'"
            )

    def pending_chat_ids(self, known_chat_ids: list[int]) -> list[int]:
        if not known_chat_ids:
            return []
        placeholders = ",".join("?" * len(known_chat_ids))
        with self._connect() as connection:
            rows = connection.execute(
                f"""SELECT DISTINCT chat_id FROM telegram_messages
                WHERE processing_status='pending' AND role IN ('client', 'internal', 'owner')
                AND chat_id IN ({placeholders}) ORDER BY chat_id""",
                known_chat_ids,
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def pending_messages(self, chat_id: int, limit: int | None = None) -> list[StoredMessage]:
        sql = """SELECT * FROM telegram_messages
            WHERE chat_id=? AND processing_status='pending' AND role IN ('client', 'internal', 'owner')
            ORDER BY id"""
        params: tuple[object, ...] = (chat_id,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (chat_id, limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._stored_message(row) for row in rows]

    def claim_messages(self, message_ids: list[int]) -> list[StoredMessage]:
        if not message_ids:
            return []
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE telegram_messages SET processing_status='processing'
                WHERE id IN ({placeholders}) AND processing_status='pending'""",
                message_ids,
            )
            rows = connection.execute(
                f"SELECT * FROM telegram_messages WHERE id IN ({placeholders}) AND processing_status='processing' ORDER BY id",
                message_ids,
            ).fetchall()
        return [self._stored_message(row) for row in rows]

    def release_messages(self, message_ids: list[int]) -> None:
        if not message_ids:
            return
        placeholders = ",".join("?" * len(message_ids))
        with self._connect() as connection:
            connection.execute(
                f"""UPDATE telegram_messages SET processing_status='pending'
                WHERE id IN ({placeholders}) AND processing_status='processing'""",
                message_ids,
            )

    def mark_messages_processed(self, messages: list[StoredMessage]) -> None:
        if not messages:
            return
        placeholders = ",".join("?" * len(messages))
        ids = [item.id for item in messages]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE telegram_messages SET processing_status='processed' WHERE id IN ({placeholders})",
                ids,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO processed_updates VALUES (?, ?)",
                [(item.update_id, _now()) for item in messages],
            )

    def recent_messages(
        self, chat_id: int, limit: int = 20, *,
        time_from_utc: str | None = None, time_to_utc: str | None = None,
    ) -> list[StoredMessage]:
        with self._connect() as connection:
            clauses = ["chat_id=?", "role IN ('client', 'internal', 'owner')"]
            params: list[object] = [chat_id]
            if time_from_utc is not None:
                clauses.append("telegram_date >= ?"); params.append(time_from_utc)
            if time_to_utc is not None:
                clauses.append("telegram_date < ?"); params.append(time_to_utc)
            rows = connection.execute(
                f"SELECT * FROM telegram_messages WHERE {' AND '.join(clauses)} ORDER BY telegram_date DESC, id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._stored_message(row) for row in reversed(rows)]

    @staticmethod
    def _stored_message(row: sqlite3.Row) -> StoredMessage:
        keys = set(row.keys())
        return StoredMessage(
            id=row["id"], update_id=row["update_id"], chat_id=row["chat_id"], message_id=row["message_id"],
            sender_id=row["sender_id"], sender_name=row["sender_name"], telegram_date=row["telegram_date"],
            text=row["text"], reply_to_message_id=row["reply_to_message_id"], role=row["role"],
            processing_status=row["processing_status"],
            media_kind=str(row["media_kind"] or "") if "media_kind" in keys else "",
            media_path=str(row["media_path"] or "") if "media_path" in keys else "",
            telegram_file_id=str(row["telegram_file_id"] or "") if "telegram_file_id" in keys else "",
            media_mime=str(row["media_mime"] or "") if "media_mime" in keys else "",
            media_filename=str(row["media_filename"] or "") if "media_filename" in keys else "",
            media_group_id=str(row["media_group_id"] or "") if "media_group_id" in keys else "",
            media_file_unique_id=str(row["media_file_unique_id"] or "") if "media_file_unique_id" in keys else "",
            forward_origin=str(row["forward_origin"] or "") if "forward_origin" in keys else "",
            download_status=str(row["download_status"] or "") if "download_status" in keys else "",
            download_error=str(row["download_error"] or "") if "download_error" in keys else "",
        )

    def get_chat_state(self, telegram_chat_id: int) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM chat_states WHERE telegram_chat_id=?",
                (telegram_chat_id,),
            ).fetchone()
        if row is None:
            return dict(DEFAULT_CHAT_STATE)
        try:
            payload = json.loads(row["state_json"])
        except (json.JSONDecodeError, TypeError):
            return dict(DEFAULT_CHAT_STATE)
        state = dict(DEFAULT_CHAT_STATE)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key in state:
                    state[key] = value
        return state

    def save_chat_state(self, telegram_chat_id: int, state: dict) -> None:
        merged = dict(DEFAULT_CHAT_STATE)
        for key, value in state.items():
            if key in merged:
                merged[key] = value
        merged["updated_at"] = _now()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_states (telegram_chat_id, state_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (telegram_chat_id, json.dumps(merged, ensure_ascii=False), merged["updated_at"]),
            )

    def create_owner_question(self, telegram_chat_id: int, question: str, recommendation_id: int | None) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_questions
                (telegram_chat_id, recommendation_id, question, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)""",
                (telegram_chat_id, recommendation_id, question, _now()),
            )
            return int(cursor.lastrowid)

    def attach_owner_question_message(self, question_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_questions SET owner_message_id=? WHERE id=?",
                (owner_message_id, question_id),
            )

    def get_owner_question_by_message(self, owner_message_id: int) -> OwnerQuestion | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM owner_questions WHERE owner_message_id=? AND status='pending'",
                (owner_message_id,),
            ).fetchone()
        if row is None:
            return None
        return OwnerQuestion(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"],
            recommendation_id=row["recommendation_id"], question=row["question"],
            owner_message_id=row["owner_message_id"], status=row["status"],
        )

    def create_owner_query_prompt(
        self, question: str, telegram_chat_id: int | None = None, *,
        target_chat_ids: tuple[int, ...] | list[int] = (),
        time_from_utc: str | None = None, time_to_utc: str | None = None,
        time_label: str = "", detail_level: str = "short",
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_query_prompts
                (question, telegram_chat_id, target_chat_ids, time_from_utc, time_to_utc,
                 time_label, detail_level, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (question.strip(), telegram_chat_id, json.dumps(list(target_chat_ids)),
                 time_from_utc, time_to_utc, time_label, detail_level, _now()),
            )
            return int(cursor.lastrowid)

    def attach_owner_query_prompt(self, prompt_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_query_prompts SET owner_message_id=? WHERE id=? AND status='pending'",
                (owner_message_id, prompt_id),
            )

    def get_owner_query_prompt_by_message(self, owner_message_id: int) -> OwnerQueryPrompt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM owner_query_prompts WHERE owner_message_id=? AND status='pending'",
                (owner_message_id,),
            ).fetchone()
        if row is None:
            return None
        keys = row.keys()
        return OwnerQueryPrompt(
            id=row["id"], question=row["question"],
            owner_message_id=row["owner_message_id"], status=row["status"],
            telegram_chat_id=row["telegram_chat_id"] if "telegram_chat_id" in keys else None,
            target_chat_ids=tuple(json.loads(row["target_chat_ids"] or "[]")) if "target_chat_ids" in keys else (),
            time_from_utc=row["time_from_utc"] if "time_from_utc" in keys else None,
            time_to_utc=row["time_to_utc"] if "time_to_utc" in keys else None,
            time_label=row["time_label"] if "time_label" in keys else "",
            detail_level=row["detail_level"] if "detail_level" in keys else "short",
        )

    def create_owner_query_selection(
        self, question: str, owner_chat_id: int, available_chat_ids: list[int] | tuple[int, ...],
        *, time_from_utc: str | None = None, time_to_utc: str | None = None,
        time_label: str = "", detail_level: str = "short",
        created_by_user_id: int | None = None, created_by_username: str | None = None,
        created_by_name: str | None = None,
    ) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_query_selections
                (question, owner_chat_id, available_chat_ids, selected_chat_ids, mode, status,
                 time_from_utc, time_to_utc, time_label, detail_level,
                 created_by_user_id, created_by_username, created_by_name, created_at, updated_at)
                VALUES (?, ?, ?, '[]', 'ambiguous', 'selecting', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (question.strip(), owner_chat_id, json.dumps(list(available_chat_ids)),
                 time_from_utc, time_to_utc, time_label, detail_level,
                 created_by_user_id, created_by_username, created_by_name, now, now),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _owner_query_selection(row: sqlite3.Row) -> OwnerQuerySelection:
        def ids(name: str) -> tuple[int, ...]:
            try:
                return tuple(int(value) for value in json.loads(row[name] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return ()
        return OwnerQuerySelection(
            id=row["id"], question=row["question"], owner_chat_id=row["owner_chat_id"],
            available_chat_ids=ids("available_chat_ids"), selected_chat_ids=ids("selected_chat_ids"),
            mode=row["mode"], status=row["status"], time_from_utc=row["time_from_utc"],
            time_to_utc=row["time_to_utc"], time_label=row["time_label"], detail_level=row["detail_level"],
            owner_message_id=row["owner_message_id"],
            created_by_user_id=row["created_by_user_id"] if "created_by_user_id" in row.keys() else None,
            created_by_username=row["created_by_username"] if "created_by_username" in row.keys() else None,
            created_by_name=row["created_by_name"] if "created_by_name" in row.keys() else None,
        )

    def get_owner_query_selection(self, selection_id: int) -> OwnerQuerySelection | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owner_query_selections WHERE id=?", (selection_id,)).fetchone()
        return None if row is None else self._owner_query_selection(row)

    def attach_owner_query_selection(self, selection_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_query_selections SET owner_message_id=?, updated_at=? WHERE id=?",
                (owner_message_id, _now(), selection_id),
            )

    def update_owner_query_selection(
        self, selection_id: int, *, mode: str | None = None, selected_chat_ids: list[int] | tuple[int, ...] | None = None,
        status: str | None = None,
    ) -> bool:
        values: list[object] = []
        updates: list[str] = []
        if mode is not None:
            updates.append("mode=?"); values.append(mode)
        if selected_chat_ids is not None:
            updates.append("selected_chat_ids=?"); values.append(json.dumps(list(selected_chat_ids)))
        if status is not None:
            updates.append("status=?"); values.append(status)
        if not updates:
            return False
        updates.append("updated_at=?"); values.append(_now()); values.append(selection_id)
        with self._connect() as connection:
            result = connection.execute(
                f"UPDATE owner_query_selections SET {', '.join(updates)} WHERE id=? AND status='selecting'",
                values,
            )
        return result.rowcount == 1

    def claim_owner_query_selection(self, selection_id: int) -> OwnerQuerySelection | None:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_selections SET status='processing', updated_at=? WHERE id=? AND status='selecting'",
                (_now(), selection_id),
            )
            if result.rowcount != 1:
                return None
            row = connection.execute("SELECT * FROM owner_query_selections WHERE id=?", (selection_id,)).fetchone()
        return None if row is None else self._owner_query_selection(row)

    def cancel_owner_query_selection(self, selection_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_selections SET status='cancelled', updated_at=? WHERE id=? AND status='selecting'",
                (_now(), selection_id),
            )
        return result.rowcount == 1

    def finish_owner_query_selection(self, selection_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_selections SET status='answered', updated_at=? WHERE id=? AND status='processing'",
                (_now(), selection_id),
            )
        return result.rowcount == 1

    def reset_owner_query_selection(self, selection_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_selections SET status='selecting', updated_at=? WHERE id=? AND status='processing'",
                (_now(), selection_id),
            )
        return result.rowcount == 1

    def fail_owner_query_selection(self, selection_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_selections SET status='failed', updated_at=? WHERE id=? AND status='processing'",
                (_now(), selection_id),
            )
        return result.rowcount == 1

    def portfolio_messages(
        self, chat_id: int, *, time_from_utc: str | None = None, time_to_utc: str | None = None,
        limit: int = 200,
    ) -> tuple[list[StoredMessage], int, bool]:
        clauses = ["chat_id=?", "role IN ('client', 'internal', 'owner')"]
        params: list[object] = [chat_id]
        if time_from_utc is not None:
            clauses.append("telegram_date >= ?"); params.append(time_from_utc)
        if time_to_utc is not None:
            clauses.append("telegram_date < ?"); params.append(time_to_utc)
        where = " AND ".join(clauses)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM telegram_messages WHERE {where} ORDER BY telegram_date DESC, id DESC LIMIT ?", (*params, limit + 1),
            ).fetchall()
            total = int(connection.execute(f"SELECT COUNT(*) FROM telegram_messages WHERE {where}", params).fetchone()[0])
        shown = [self._stored_message(row) for row in reversed(rows[:limit])]
        return shown, total, total > limit

    def answer_owner_query_prompt(self, prompt_id: int) -> bool:
        with self._connect() as connection:
            result = connection.execute(
                "UPDATE owner_query_prompts SET status='answered' WHERE id=? AND status='pending'",
                (prompt_id,),
            )
        return result.rowcount == 1

    def create_owner_query_delivery(self, text: str, prompt_id: int | None, selection_id: int | None = None, general_task_id: int | None = None) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_query_deliveries (text, prompt_id, selection_id, general_task_id, created_at)
                VALUES (?, ?, ?, ?, ?)""",
                (text, prompt_id, selection_id, general_task_id, _now()),
            )
            return int(cursor.lastrowid)

    def pending_owner_query_deliveries(self) -> list[tuple[int, str, int | None, int | None, int | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, text, prompt_id, selection_id, general_task_id FROM owner_query_deliveries
                WHERE owner_message_id IS NULL ORDER BY id""",
            ).fetchall()
        return [(int(row["id"]), row["text"], row["prompt_id"], row["selection_id"], row["general_task_id"]) for row in rows]

    def create_reminder(
        self, owner_chat_id: int, remind_at_utc: str, text: str, *,
        related_chat_id: int | None = None, related_chat_name: str | None = None,
        created_by_user_id: int | None = None, created_by_username: str | None = None,
        created_by_name: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO reminders
                (owner_chat_id, remind_at_utc, text, related_chat_id, related_chat_name,
                 created_by_user_id, created_by_username, created_by_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (owner_chat_id, remind_at_utc, text, related_chat_id, related_chat_name,
                 created_by_user_id, created_by_username, created_by_name),
            )
            return int(cursor.lastrowid)

    @staticmethod
    def _reminder(row: sqlite3.Row) -> ReminderRecord:
        keys = row.keys()
        return ReminderRecord(
            row["id"], row["owner_chat_id"], row["remind_at_utc"], row["text"],
            row["related_chat_id"] if "related_chat_id" in keys else None,
            row["related_chat_name"] if "related_chat_name" in keys else None,
            row["created_by_user_id"] if "created_by_user_id" in keys else None,
            row["created_by_username"] if "created_by_username" in keys else None,
            row["created_by_name"] if "created_by_name" in keys else None,
        )

    @staticmethod
    def _general_task(row: sqlite3.Row) -> GeneralTaskRecord:
        return GeneralTaskRecord(
            int(row["id"]), int(row["owner_chat_id"]), row["request_text"], row["understanding"],
            row["kind"], json.loads(row["payload_json"] or "{}"), row["status"],
            row["owner_message_id"], row["clarification_message_id"],
        )

    def create_general_task(self, owner_chat_id: int, request_text: str, understanding: str, kind: str, payload: dict) -> int:
        now = _now()
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO owner_general_tasks
                (owner_chat_id, request_text, understanding, kind, payload_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'confirming', ?, ?)""",
                (owner_chat_id, request_text, understanding, kind, json.dumps(payload, ensure_ascii=False), now, now),
            )
            return int(cursor.lastrowid)

    def get_general_task(self, task_id: int) -> GeneralTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owner_general_tasks WHERE id=?", (task_id,)).fetchone()
        return None if row is None else self._general_task(row)

    def general_task_result_by_message(
        self, owner_chat_id: int, owner_message_id: int,
    ) -> tuple[GeneralTaskRecord, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT task.*, delivery.text AS result_text
                FROM owner_query_deliveries AS delivery
                JOIN owner_general_tasks AS task ON task.id=delivery.general_task_id
                WHERE task.owner_chat_id=? AND delivery.owner_message_id=? AND task.status='done'""",
                (owner_chat_id, owner_message_id),
            ).fetchone()
        return None if row is None else (self._general_task(row), str(row["result_text"]))

    def attach_general_task_message(self, task_id: int, message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_general_tasks SET owner_message_id=?, clarification_message_id=NULL, updated_at=? WHERE id=? AND status='confirming'",
                (message_id, _now(), task_id),
            )

    def mark_general_task_clarification(self, task_id: int, message_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE owner_general_tasks SET clarification_message_id=?, updated_at=? WHERE id=? AND status='confirming'",
                (message_id, _now(), task_id),
            )
            return cursor.rowcount == 1

    def general_task_by_clarification(self, owner_chat_id: int, message_id: int) -> GeneralTaskRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM owner_general_tasks WHERE owner_chat_id=? AND clarification_message_id=? AND status='confirming'",
                (owner_chat_id, message_id),
            ).fetchone()
        return None if row is None else self._general_task(row)

    def revise_general_task(self, task_id: int, request_text: str, understanding: str, kind: str, payload: dict) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE owner_general_tasks SET request_text=?, understanding=?, kind=?, payload_json=?,
                owner_message_id=NULL, clarification_message_id=NULL, updated_at=? WHERE id=? AND status='confirming'""",
                (request_text, understanding, kind, json.dumps(payload, ensure_ascii=False), _now(), task_id),
            )
            return cursor.rowcount == 1

    def set_general_task_status(self, task_id: int, expected: str, status: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE owner_general_tasks SET status=?, updated_at=? WHERE id=? AND status=?",
                (status, _now(), task_id, expected),
            )
            return cursor.rowcount == 1

    def create_self_restart(self, task_id: int, owner_chat_id: int, old_pid: int, reason: str) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO self_restarts
                (general_task_id, owner_chat_id, old_pid, reason, status, created_at)
                VALUES (?, ?, ?, ?, 'prepared', ?)""",
                (task_id, owner_chat_id, old_pid, reason, _now()),
            )
            return int(cursor.lastrowid)

    def pending_self_restart(self, current_pid: int) -> SelfRestartRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, general_task_id, owner_chat_id, old_pid, reason FROM self_restarts
                WHERE status='launched' AND old_pid<>? ORDER BY id LIMIT 1""",
                (current_pid,),
            ).fetchone()
        return None if row is None else SelfRestartRecord(*row)

    def finish_self_restart(self, restart_id: int, *, launched: bool) -> bool:
        restart_status, task_status = ("launched", "done") if launched else ("failed", "failed")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT general_task_id FROM self_restarts WHERE id=? AND status='prepared'", (restart_id,),
            ).fetchone()
            if row is None:
                return False
            connection.execute("UPDATE self_restarts SET status=? WHERE id=?", (restart_status, restart_id))
            connection.execute(
                "UPDATE owner_general_tasks SET status=?, updated_at=? WHERE id=? AND status='executing'",
                (task_status, _now(), int(row["general_task_id"])),
            )
            return True

    def acknowledge_self_restart(self, restart_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE self_restarts SET status='acknowledged', completed_at=? WHERE id=? AND status='launched'",
                (_now(), restart_id),
            )
            return cursor.rowcount == 1

    def pending_due_reminders(self, owner_chat_id: int, now_utc: str | None = None) -> list[ReminderRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM reminders
                WHERE owner_chat_id=? AND sent_at IS NULL AND remind_at_utc<=?
                ORDER BY remind_at_utc, id""",
                (owner_chat_id, now_utc or _now()),
            ).fetchall()
        return [self._reminder(row) for row in rows]

    def pending_reminders(self, owner_chat_id: int) -> list[ReminderRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM reminders
                WHERE owner_chat_id=? AND sent_at IS NULL
                ORDER BY remind_at_utc, id""",
                (owner_chat_id,),
            ).fetchall()
        return [self._reminder(row) for row in rows]

    def mark_reminder_sent(self, reminder_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE reminders SET sent_at=? WHERE id=? AND sent_at IS NULL",
                (_now(), reminder_id),
            )
            return cursor.rowcount == 1

    def attach_owner_query_delivery(self, delivery_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_query_deliveries SET owner_message_id=? WHERE id=? AND owner_message_id IS NULL",
                (owner_message_id, delivery_id),
            )

    def answer_owner_question(self, question_id: int) -> OwnerQuestion | None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE owner_questions SET status='answered' WHERE id=? AND status='pending'",
                (question_id,),
            )
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owner_questions WHERE id=?", (question_id,)).fetchone()
        if row is None:
            return None
        return OwnerQuestion(
            id=row["id"], telegram_chat_id=row["telegram_chat_id"],
            recommendation_id=row["recommendation_id"], question=row["question"],
            owner_message_id=row["owner_message_id"], status=row["status"],
        )

    def record_experience(
        self, *, telegram_chat_id: int | None, chat_name: str, situation: str, lesson: str, source_draft_id: int | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO experience_entries
                (telegram_chat_id, chat_name, situation, lesson, kind, source_draft_id, status, created_at)
                VALUES (?, ?, ?, ?, 'experience', ?, 'active', ?)""",
                (telegram_chat_id, chat_name, situation, lesson, source_draft_id, _now()),
            )

    def ensure_onboarding(
        self,
        telegram_chat_id: int,
        chat_title: str,
        added_by_name: str = "",
        added_by_id: int | None = None,
    ) -> ChatOnboarding:
        now = _now()
        title = chat_title.strip() or f"Чат {telegram_chat_id}"
        added_name = added_by_name.strip()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO chat_onboardings
                (telegram_chat_id, chat_title, added_by_name, added_by_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending_brief', ?, ?)
                ON CONFLICT(telegram_chat_id) DO UPDATE SET
                    chat_title=excluded.chat_title,
                    added_by_name=CASE WHEN excluded.added_by_name='' THEN chat_onboardings.added_by_name ELSE excluded.added_by_name END,
                    added_by_id=COALESCE(excluded.added_by_id, chat_onboardings.added_by_id),
                    status=CASE WHEN chat_onboardings.status='confirmed' THEN chat_onboardings.status
                                WHEN chat_onboardings.status='cancelled' THEN 'pending_brief'
                                ELSE chat_onboardings.status END,
                    owner_notice_message_id=CASE WHEN chat_onboardings.status='cancelled' THEN NULL
                                                 ELSE chat_onboardings.owner_notice_message_id END,
                    updated_at=excluded.updated_at""",
                (telegram_chat_id, title, added_name, added_by_id, now, now),
            )
        record = self.get_onboarding(telegram_chat_id)
        if record is None:
            raise RuntimeError("Failed to persist chat onboarding")
        return record

    def get_onboarding(self, telegram_chat_id: int) -> ChatOnboarding | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM chat_onboardings WHERE telegram_chat_id=?",
                (telegram_chat_id,),
            ).fetchone()
        return None if row is None else self._onboarding(row)

    def get_onboarding_by_id(self, onboarding_id: int) -> ChatOnboarding | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM chat_onboardings WHERE id=?", (onboarding_id,)).fetchone()
        return None if row is None else self._onboarding(row)

    def get_onboarding_by_owner_message(self, owner_message_id: int) -> ChatOnboarding | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT * FROM chat_onboardings
                WHERE status IN ('pending_brief', 'pending_draft')
                  AND (owner_notice_message_id=? OR draft_message_id=? OR clarification_prompt_message_id=?)
                ORDER BY id DESC LIMIT 1""",
                (owner_message_id, owner_message_id, owner_message_id),
            ).fetchone()
        return None if row is None else self._onboarding(row)

    def has_open_onboarding(self, telegram_chat_id: int) -> bool:
        record = self.get_onboarding(telegram_chat_id)
        return record is not None and record.status in {"pending_brief", "pending_draft"}

    def pending_onboarding_notices(self) -> list[ChatOnboarding]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM chat_onboardings
                WHERE status IN ('pending_brief', 'pending_draft') AND owner_notice_message_id IS NULL
                ORDER BY id""",
            ).fetchall()
        return [self._onboarding(row) for row in rows]

    def attach_onboarding_notice(self, onboarding_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE chat_onboardings SET owner_notice_message_id=?, updated_at=? WHERE id=?",
                (owner_message_id, _now(), onboarding_id),
            )

    def attach_onboarding_draft_message(self, onboarding_id: int, owner_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE chat_onboardings SET draft_message_id=?, status='pending_draft', updated_at=?
                WHERE id=? AND status IN ('pending_brief', 'pending_draft')""",
                (owner_message_id, _now(), onboarding_id),
            )

    def save_onboarding_draft(
        self, onboarding_id: int, *, owner_brief: str, draft_name: str, draft_wiki: str, draft_directory: str,
    ) -> ChatOnboarding | None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE chat_onboardings
                SET owner_brief=?, draft_name=?, draft_wiki=?, draft_directory=?, status='pending_draft', updated_at=?
                WHERE id=? AND status IN ('pending_brief', 'pending_draft')""",
                (owner_brief.strip(), draft_name.strip(), draft_wiki.strip(), draft_directory.strip(), _now(), onboarding_id),
            )
        return self.get_onboarding_by_id(onboarding_id)

    def mark_onboarding_clarification(self, onboarding_id: int, prompt_message_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE chat_onboardings
                SET clarification_prompt_message_id=?, status='pending_brief', updated_at=?
                WHERE id=? AND status IN ('pending_brief', 'pending_draft')""",
                (prompt_message_id, _now(), onboarding_id),
            )

    def confirm_onboarding(self, onboarding_id: int) -> ChatOnboarding | None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE chat_onboardings SET status='confirmed', updated_at=?
                WHERE id=? AND status='pending_draft'""",
                (_now(), onboarding_id),
            )
        record = self.get_onboarding_by_id(onboarding_id)
        if record is None or record.status != "confirmed":
            return None
        return record

    def cancel_onboarding(self, telegram_chat_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE chat_onboardings SET status='cancelled', updated_at=?
                WHERE telegram_chat_id=? AND status IN ('pending_brief', 'pending_draft')""",
                (_now(), telegram_chat_id),
            )

    def release_held_messages(self, chat_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE telegram_messages SET processing_status='pending'
                WHERE chat_id=? AND processing_status='held'""",
                (chat_id,),
            )

    @staticmethod
    def _onboarding(row: sqlite3.Row) -> ChatOnboarding:
        return ChatOnboarding(
            id=row["id"],
            telegram_chat_id=row["telegram_chat_id"],
            chat_title=row["chat_title"],
            added_by_name=row["added_by_name"] or "",
            added_by_id=row["added_by_id"],
            status=row["status"],
            owner_notice_message_id=row["owner_notice_message_id"],
            owner_brief=row["owner_brief"] or "",
            draft_name=row["draft_name"] or "",
            draft_wiki=row["draft_wiki"] or "",
            draft_directory=row["draft_directory"] or "",
            draft_message_id=row["draft_message_id"],
            clarification_prompt_message_id=row["clarification_prompt_message_id"],
        )

    def recent_experience(self, telegram_chat_id: int, limit: int = 3) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT situation, lesson FROM experience_entries
                WHERE status='active' AND (telegram_chat_id=? OR telegram_chat_id IS NULL)
                ORDER BY id DESC LIMIT ?""",
                (telegram_chat_id, limit),
            ).fetchall()
        return [f"{row['situation']} → {row['lesson']}" for row in rows]
