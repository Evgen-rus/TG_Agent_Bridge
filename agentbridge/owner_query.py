"""Small value objects and deterministic helpers for portfolio owner queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class OwnerQueryScope:
    mode: str
    chat_ids: tuple[int, ...]
    question: str
    time_from_utc: str | None = None
    time_to_utc: str | None = None
    time_label: str = "текущее состояние и недавняя история"
    detail_level: str = "short"


@dataclass(frozen=True)
class OwnerQueryIntent:
    mode: str
    selected_names: tuple[str, ...] = ()
    time_phrase: str = ""
    detail_level: str = "short"


@dataclass(frozen=True)
class PortfolioChatSummary:
    chat_name: str
    period: str
    current_status: str = ""
    events: str = ""
    problems: str = ""
    waiting_us: str = ""
    waiting_client: str = ""
    next_step: str = ""
    metrics: str = ""
    uncertainties: str = ""


def parse_owner_time_phrase(
    phrase: str,
    *,
    timezone_name: str = "Asia/Novosibirsk",
    now: datetime | None = None,
) -> tuple[str | None, str | None, str]:
    """Return a half-open UTC range for the small set of supported phrases.

    An absent or unrecognised phrase deliberately means no range: the caller
    then uses current state and bounded recent history rather than guessing.
    """
    text = " ".join((phrase or "").casefold().split())
    if not text:
        return None, None, "текущее состояние и недавняя история"
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        # Windows installations without the optional tzdata wheel still need
        # the project's fixed operational zone. Other invalid zones remain an
        # explicit configuration error.
        if timezone_name != "Asia/Novosibirsk":
            raise
        zone = timezone(timedelta(hours=7), name=timezone_name)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(zone)
    start: datetime | None = None
    end: datetime | None = None
    label = phrase.strip()
    if re.search(r"\b(сегодня|today)\b", text):
        start = datetime.combine(local.date(), time.min, zone)
        end = start + timedelta(days=1)
        label = "сегодня"
    elif re.search(r"\b(вчера|yesterday)\b", text):
        end = datetime.combine(local.date(), time.min, zone)
        start = end - timedelta(days=1)
        label = "вчера"
    else:
        match = re.search(r"(?:последн(?:ие|их)|last)\s+(\d{1,2})\s*(?:дн(?:я|ей)?|days?)", text)
        if match:
            days = int(match.group(1))
            if not 1 <= days <= 31:
                raise ValueError("owner time range must contain 1..31 days")
            end = datetime.combine(local.date() + timedelta(days=1), time.min, zone)
            start = end - timedelta(days=days)
            label = f"последние {days} дн."
        elif re.search(r"\b(за неделю|последние 7 дней|last 7 days)\b", text):
            end = datetime.combine(local.date() + timedelta(days=1), time.min, zone)
            start = end - timedelta(days=7)
            label = "последние 7 дней"
        elif re.search(r"\b(прошлая календарная неделя|за прошлую неделю|previous calendar week)\b", text):
            monday = local.date() - timedelta(days=local.weekday() + 7)
            start = datetime.combine(monday, time.min, zone)
            end = start + timedelta(days=7)
            label = "прошлая календарная неделя"
        elif re.search(r"\b(эта неделя|this week|current week)\b", text):
            monday = local.date() - timedelta(days=local.weekday())
            start = datetime.combine(monday, time.min, zone)
            end = start + timedelta(days=7)
            label = "эта неделя"
        elif re.search(r"\b(с начала месяца|с начала текущего месяца|since start of month)\b", text):
            start = datetime.combine(local.date().replace(day=1), time.min, zone)
            end = current.astimezone(zone)
            label = "с начала месяца"
        elif re.search(r"\b(за последний месяц|последние 30 дней|last month|last 30 days)\b", text):
            end = current.astimezone(zone)
            start = end - timedelta(days=30)
            label = "последние 30 дней"
        elif re.search(r"\b(последний календарный месяц|прошлый месяц|last calendar month)\b", text):
            this_month = local.date().replace(day=1)
            end = datetime.combine(this_month, time.min, zone)
            previous_month = (this_month - timedelta(days=1)).replace(day=1)
            start = datetime.combine(previous_month, time.min, zone)
            label = "последний календарный месяц"
        elif re.search(r"\b(этот месяц|за месяц|this month|month)\b", text):
            start = datetime.combine(local.date().replace(day=1), time.min, zone)
            next_month = (start.date().replace(day=28) + timedelta(days=4)).replace(day=1)
            end = datetime.combine(next_month, time.min, zone)
            label = "этот месяц"
        else:
            match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
            if match:
                day = date.fromisoformat(match.group(1))
                start = datetime.combine(day, time.min, zone)
                end = start + timedelta(days=1)
                label = match.group(1)
    if start is None or end is None:
        return None, None, "текущее состояние и недавняя история"
    return (
        start.astimezone(timezone.utc).isoformat(),
        end.astimezone(timezone.utc).isoformat(),
        label,
    )
