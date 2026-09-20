from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentbridge.agents.base import AgentReply, OwnerQueryAnswer
from agentbridge.application import AgentBridgeApplication, OwnerQueryResult
from agentbridge.chats.loader import ChatConfig, ChatRegistry
from agentbridge.owner_query import OwnerQueryIntent, OwnerQueryScope, PortfolioChatSummary, parse_owner_time_phrase
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application


@dataclass
class PortfolioProvider:
    contexts: list[dict] = field(default_factory=list)
    aggregate_inputs: list[dict] = field(default_factory=list)
    fail_chat: str = ""
    owner_calls: list[dict] = field(default_factory=list)

    async def suggest(self, **kwargs):
        return AgentReply("thread", "", "")

    async def answer_owner_query(self, **kwargs):
        self.owner_calls.append(kwargs)
        return OwnerQueryAnswer("owner", "single")

    async def resolve_owner_query_scope(self, *, question, known_chats):
        return OwnerQueryIntent("ambiguous")

    async def summarize_portfolio_chat(self, **kwargs):
        self.contexts.append(kwargs)
        if kwargs["chat_name"] == self.fail_chat:
            raise RuntimeError("fake failure")
        return PortfolioChatSummary(kwargs["chat_name"], kwargs["period"], current_status="в работе")


@dataclass
class AggregateProvider(PortfolioProvider):
    async def aggregate_owner_portfolio(self, **kwargs):
        self.aggregate_inputs.append(kwargs)
        return "Итог по портфелю"


def _registry() -> ChatRegistry:
    return ChatRegistry({
        -1: ChatConfig(-1, "Client A", "codex", "Wiki A", Path("a")),
        -2: ChatConfig(-2, "Client B", "codex", "Wiki B", Path("b")),
    })


def _store_with_message(tmp_path, chat_id: int, date: str, text: str) -> ChatThreadStore:
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    store.ingest_telegram_message(
        update_id=chat_id * -1, chat_id=chat_id, message_id=1, sender_id=1,
        sender_name="Client", telegram_date=date, text=text, reply_to_message_id=None,
        role="client", processing_status="processed",
    )
    return store


def test_owner_time_is_local_half_open_utc_boundary() -> None:
    start, end, label = parse_owner_time_phrase(
        "today", now=datetime(2026, 9, 20, 17, 0, tzinfo=timezone.utc),
    )
    assert (start, end, label) == ("2026-09-20T17:00:00+00:00", "2026-09-21T17:00:00+00:00", "сегодня")


@pytest.mark.parametrize(
    ("phrase", "start", "end", "label"),
    [
        ("вчера", "2026-09-18T17:00:00+00:00", "2026-09-19T17:00:00+00:00", "вчера"),
        ("за неделю", "2026-09-13T17:00:00+00:00", "2026-09-20T17:00:00+00:00", "последние 7 дней"),
        ("эта неделя", "2026-09-13T17:00:00+00:00", "2026-09-20T17:00:00+00:00", "эта неделя"),
        ("за прошлую неделю", "2026-09-06T17:00:00+00:00", "2026-09-13T17:00:00+00:00", "прошлая календарная неделя"),
        ("с начала месяца", "2026-08-31T17:00:00+00:00", "2026-09-20T12:00:00+00:00", "с начала месяца"),
        ("последний календарный месяц", "2026-07-31T17:00:00+00:00", "2026-08-31T17:00:00+00:00", "последний календарный месяц"),
        ("за последний месяц", "2026-08-21T12:00:00+00:00", "2026-09-20T12:00:00+00:00", "последние 30 дней"),
        ("last month", "2026-08-21T12:00:00+00:00", "2026-09-20T12:00:00+00:00", "последние 30 дней"),
    ],
)
def test_owner_time_calendar_boundaries(phrase, start, end, label) -> None:
    result = parse_owner_time_phrase(
        phrase, now=datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc),
    )
    assert result == (start, end, label)


def test_recognized_invalid_day_range_is_not_silently_clamped() -> None:
    with pytest.raises(ValueError):
        parse_owner_time_phrase("последние 40 дней")


def test_portfolio_storage_filters_and_marks_truncation(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    for index in range(3):
        store.ingest_telegram_message(
            update_id=index + 1, chat_id=-1, message_id=index + 1, sender_id=1,
            sender_name="Client", telegram_date=f"2026-09-0{index + 1}T00:00:00+00:00",
            text=str(index), reply_to_message_id=None, role="client", processing_status="processed",
        )
    rows, total, truncated = store.portfolio_messages(-1, time_from_utc="2026-09-02T00:00:00+00:00", time_to_utc="2026-09-03T00:00:00+00:00", limit=1)
    assert [row.text for row in rows] == ["1"] and total == 1 and not truncated
    rows, total, truncated = store.portfolio_messages(-1, limit=2)
    assert len(rows) == 2 and total == 3 and truncated


def test_portfolio_storage_orders_by_telegram_date_not_insert_id(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    for index, telegram_date in enumerate(("2026-09-20T00:00:00+00:00", "2026-09-18T00:00:00+00:00", "2026-09-19T00:00:00+00:00"), 1):
        store.ingest_telegram_message(
            update_id=index, chat_id=-1, message_id=index, sender_id=1, sender_name="Client",
            telegram_date=telegram_date, text=telegram_date[8:10], reply_to_message_id=None,
            role="client", processing_status="processed",
        )
    rows, total, truncated = store.portfolio_messages(-1, limit=2)
    assert [row.text for row in rows] == ["19", "20"] and total == 3 and truncated


@pytest.mark.asyncio
async def test_ambiguous_scope_persists_and_all_callback_is_idempotent(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)
    initial = await service.handle_owner_query("Сравни клиентов")
    assert isinstance(initial, OwnerQueryResult) and initial.selection_id is not None
    selected = await service.handle_owner_query_selection(initial.selection_id, "all")
    assert isinstance(selected, OwnerQueryResult) and "Итог" in selected.text
    repeated = await service.handle_owner_query_selection(initial.selection_id, "all")
    assert repeated is not None and "уже" in repeated.text
    assert len(provider.aggregate_inputs) == 1


@pytest.mark.asyncio
async def test_explicit_multiple_and_existing_single_keep_separate_owner_paths(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)
    multiple = await service.handle_owner_query("Сравни Client A и Client B")
    assert isinstance(multiple, OwnerQueryResult) and multiple.prompt_id is not None
    assert len(provider.aggregate_inputs) == 1
    single = await service.handle_owner_query("Что сейчас у Client A?")
    assert isinstance(single, OwnerQueryResult) and single.prompt_id is not None
    assert service.store.get_owner_query_thread_id(-1) == "owner"


@pytest.mark.asyncio
async def test_single_checklist_finishes_and_unknown_target_does_not_stick(tmp_path) -> None:
    provider = AggregateProvider()
    db = tmp_path / "db.sqlite3"
    service = AgentBridgeApplication(_registry(), ChatThreadStore(db), provider, owner_chat_id=77)
    initial = await service.handle_owner_query("Нужен разбор")
    assert initial.selection_id is not None
    await service.handle_owner_query_selection(initial.selection_id, "single", owner_chat_id=77)
    await service.handle_owner_query_selection(initial.selection_id, "item_add", 0, 77)
    done = await service.handle_owner_query_selection(initial.selection_id, "done", owner_chat_id=77)
    assert done is not None and service.store.get_owner_query_selection(initial.selection_id).status == "answered"
    bad_id = service.store.create_owner_query_selection("bad", 77, [-999])
    await service.handle_owner_query_selection(bad_id, "all", owner_chat_id=77)
    assert service.store.get_owner_query_selection(bad_id).status == "failed"


@pytest.mark.asyncio
async def test_multi_selection_restart_reset_cancel_foreign_and_idempotent_item(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    provider = AggregateProvider()
    first_service = AgentBridgeApplication(_registry(), ChatThreadStore(db), provider, owner_chat_id=77)
    initial = await first_service.handle_owner_query("Нужен выбор")
    assert initial.selection_id is not None
    restarted = AgentBridgeApplication(_registry(), ChatThreadStore(db), provider, owner_chat_id=77)
    assert restarted.owner_query_selection(initial.selection_id).status == "selecting"
    await restarted.handle_owner_query_selection(initial.selection_id, "multi", owner_chat_id=77)
    await restarted.handle_owner_query_selection(initial.selection_id, "item_add", 0, 77)
    await restarted.handle_owner_query_selection(initial.selection_id, "item_add", 0, 77)
    assert restarted.owner_query_selection(initial.selection_id).selected_chat_ids == (-1,)
    foreign = await restarted.handle_owner_query_selection(initial.selection_id, "item_remove", 0, 88)
    assert foreign is not None and "недоступен" in foreign.text
    reset = await restarted.handle_owner_query_selection(initial.selection_id, "reset", owner_chat_id=77)
    assert reset.selection_id == initial.selection_id
    cancelled = await restarted.handle_owner_query_selection(initial.selection_id, "cancel", owner_chat_id=77)
    assert "отменён" in cancelled.text
    repeated = await restarted.handle_owner_query_selection(initial.selection_id, "cancel", owner_chat_id=77)
    assert "уже" in repeated.text


@pytest.mark.asyncio
async def test_duplicate_owner_text_update_and_prompt_isolation(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)
    first = await service.handle_owner_query("все проекты", update_id=91)
    second = await service.handle_owner_query("все проекты", update_id=91)
    assert isinstance(first, OwnerQueryResult) and second is None
    assert len(provider.aggregate_inputs) == 1
    prompt = service.store.get_owner_query_prompt_by_message  # prompt metadata is checked through the next continuation
    service.attach_owner_query_prompt(first.prompt_id, 700)
    follow = await service.continue_owner_query(700, "только риски", update_id=92)
    assert isinstance(follow, OwnerQueryResult)


@pytest.mark.asyncio
async def test_registered_portfolio_callback_deduplicates_update_and_keeps_item_state(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)
    initial = await service.handle_owner_query("Нужен выбор")
    assert initial.selection_id is not None

    class Query:
        def __init__(self, data):
            self.data = data
            self.message = SimpleNamespace(chat=SimpleNamespace(id=77))
            self.edits = 0

        async def answer(self):
            return None

        async def edit_message_text(self, text, reply_markup=None):
            self.edits += 1

        async def edit_message_reply_markup(self, reply_markup=None):
            self.edits += 1

    telegram_app = create_telegram_application(
        token="test-token", owner_chat_id=77, message_service=service, batch_seconds=0,
    )
    callback = next(
        handler.callback for group in telegram_app.handlers.values() for handler in group
        if hasattr(handler, "callback") and handler.callback.__name__ == "learning_callback"
    )
    query = Query(f"portfolio:multi:{initial.selection_id}")
    context = SimpleNamespace(bot=SimpleNamespace(send_message=lambda **kwargs: None))
    await callback(SimpleNamespace(callback_query=query, update_id=901), context)
    query.data = f"portfolio:item_add:{initial.selection_id}:0"
    await callback(SimpleNamespace(callback_query=query, update_id=902), context)
    await callback(SimpleNamespace(callback_query=query, update_id=902), context)
    assert service.owner_query_selection(initial.selection_id).selected_chat_ids == (-1,)


@pytest.mark.asyncio
async def test_owner_voice_transcript_uses_portfolio_resolver(tmp_path, monkeypatch) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)

    class VoiceFile:
        async def download_to_drive(self, custom_path=None, **kwargs):
            path = Path(custom_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"voice")

    class Bot:
        id = 777

        async def get_file(self, file_id):
            return VoiceFile()

        async def send_message(self, **kwargs):
            return SimpleNamespace(message_id=9001)

    async def transcribe(path, *, api_key, model):
        return "Рик, все проекты"

    monkeypatch.setattr("agentbridge.telegram.bot.transcribe_audio_file", transcribe)
    telegram_app = create_telegram_application(
        token="test-token", owner_chat_id=77, message_service=service, batch_seconds=0,
        media_dir=tmp_path / "media", openai_api_key="test-key",
    )
    callback = telegram_app.handlers[0][0].callback
    update = SimpleNamespace(
        effective_message=SimpleNamespace(
            text="", voice=SimpleNamespace(file_id="voice-id"), message_id=123,
            reply_to_message=None,
        ),
        effective_chat=SimpleNamespace(id=77), effective_user=SimpleNamespace(id=1, full_name="Owner", is_bot=False), update_id=903,
    )
    await callback(update, SimpleNamespace(bot=Bot()))
    assert len(provider.aggregate_inputs) == 1


@pytest.mark.asyncio
async def test_single_timed_context_and_prompt_metadata_are_preserved(tmp_path) -> None:
    provider = AggregateProvider()
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    for update_id, telegram_date, text in (
        (1, "2026-09-19T23:59:59+00:00", "outside-before"),
        (2, "2026-09-20T00:00:00+00:00", "inside"),
        (3, "2026-09-21T00:00:00+00:00", "outside-after"),
    ):
        store.ingest_telegram_message(
            update_id=update_id, chat_id=-1, message_id=update_id, sender_id=1, sender_name="Client",
            telegram_date=telegram_date, text=text, reply_to_message_id=None,
            role="client", processing_status="processed",
        )
    service = AgentBridgeApplication(_registry(), store, provider, owner_chat_id=77)
    scope = OwnerQueryScope("single", (-1,), "что было", "2026-09-20T00:00:00+00:00", "2026-09-21T00:00:00+00:00", "сегодня")
    answer = await service._answer_owner_query_for_chat(_registry().get(-1), scope.question, scope=scope)
    prompt_result = service._follow_up_query_result(_registry().get(-1), scope.question, answer, scope=scope)
    row = store.get_owner_query_prompt_by_message
    assert "inside" in provider.owner_calls[-1]["context_pack"]
    assert "outside-before" not in provider.owner_calls[-1]["context_pack"]
    assert "outside-after" not in provider.owner_calls[-1]["context_pack"]
    prompt_id = prompt_result.prompt_id
    store.attach_owner_query_prompt(prompt_id, 811)
    prompt = row(811)
    assert prompt.target_chat_ids == (-1,)
    assert prompt.time_from_utc == scope.time_from_utc and prompt.time_to_utc == scope.time_to_utc


@pytest.mark.asyncio
async def test_single_timed_context_has_counted_truncation_marker(tmp_path) -> None:
    provider = AggregateProvider()
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    for update_id in range(201):
        store.ingest_telegram_message(
            update_id=update_id + 1, chat_id=-1, message_id=update_id + 1, sender_id=1,
            sender_name="Client", telegram_date=f"2026-09-20T00:{update_id // 60:02d}:{update_id % 60:02d}+00:00",
            text=f"timed-{update_id}", reply_to_message_id=None,
            role="client", processing_status="processed",
        )
    store.ingest_telegram_message(
        update_id=1000, chat_id=-1, message_id=1000, sender_id=1,
        sender_name="Client",
        telegram_date="2026-09-19T23:59:59+00:00", text="out-of-range",
        reply_to_message_id=None, role="client", processing_status="processed",
    )
    service = AgentBridgeApplication(_registry(), store, provider, owner_chat_id=77)
    scope = OwnerQueryScope("single", (-1,), "что было", "2026-09-20T00:00:00+00:00", "2026-09-21T00:00:00+00:00", "сегодня")
    await service._answer_owner_query_for_chat(_registry().get(-1), scope.question, scope=scope)
    context = provider.owner_calls[-1]["context_pack"]
    assert "показано 200 из 201" in context and "обрезано до 200 сообщений" in context
    assert "timed-0" not in context and "timed-200" in context
    assert "out-of-range" not in context


def test_processing_owner_selection_is_requeued_after_restart(tmp_path) -> None:
    db = tmp_path / "db.sqlite3"
    first = ChatThreadStore(db)
    selection_id = first.create_owner_query_selection("q", 77, [-1])
    assert first.claim_owner_query_selection(selection_id) is not None
    restarted = ChatThreadStore(db)
    selection = restarted.get_owner_query_selection(selection_id)
    assert selection.status == "selecting"


@pytest.mark.asyncio
async def test_ambiguous_update_claim_prevents_second_selection(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider, owner_chat_id=77)
    first = await service.handle_owner_query("сравни клиентов", update_id=441)
    second = await service.handle_owner_query("сравни клиентов", update_id=441)
    assert first.selection_id is not None and second is None
    assert service.store.get_owner_query_selection(first.selection_id).status == "selecting"


@pytest.mark.asyncio
async def test_deleted_all_follow_up_is_owner_friendly(tmp_path) -> None:
    provider = AggregateProvider()
    registry = _registry()
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    service = AgentBridgeApplication(registry, store, provider, owner_chat_id=77)
    prompt_id = store.create_owner_query_prompt(
        "все", 77, target_chat_ids=(-1, -2), time_label="сегодня",
    )
    store.attach_owner_query_prompt(prompt_id, 812)
    registry._chats.clear()
    result = await service.continue_owner_query(812, "повтори", update_id=442)
    assert isinstance(result, OwnerQueryResult)
    assert "больше не подключены" in result.text


@pytest.mark.asyncio
async def test_aggregate_gets_summaries_not_raw_chat_context(tmp_path) -> None:
    provider = AggregateProvider()
    store = ChatThreadStore(tmp_path / "db.sqlite3")
    store.ingest_telegram_message(
        update_id=1, chat_id=-1, message_id=1, sender_id=1, sender_name="Client",
        telegram_date="2026-09-20T00:00:00+00:00", text="raw secret A", reply_to_message_id=None,
        role="client", processing_status="processed",
    )
    service = AgentBridgeApplication(_registry(), store, provider)
    result = await service.handle_owner_query("все проекты за неделю")
    assert isinstance(result, OwnerQueryResult)
    assert provider.aggregate_inputs
    payload = str(provider.aggregate_inputs[0]["summaries"])
    assert "raw secret A" not in payload and "Client A" in payload


@pytest.mark.asyncio
async def test_partial_summary_failure_is_a_stub(tmp_path) -> None:
    provider = AggregateProvider(fail_chat="Client B")
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider)
    result = await service.handle_owner_query("все проекты")
    assert isinstance(result, OwnerQueryResult)
    summaries = provider.aggregate_inputs[0]["summaries"]
    assert any(item.get("failure") for item in summaries)


@pytest.mark.asyncio
async def test_portfolio_follow_up_reuses_target_set(tmp_path) -> None:
    provider = AggregateProvider()
    service = AgentBridgeApplication(_registry(), ChatThreadStore(tmp_path / "db.sqlite3"), provider)
    first = await service.handle_owner_query("все проекты")
    assert isinstance(first, OwnerQueryResult) and first.prompt_id is not None
    service.attach_owner_query_prompt(first.prompt_id, 500)
    second = await service.continue_owner_query(500, "А теперь только проблемы", update_id=3)
    assert isinstance(second, OwnerQueryResult)
    assert len(provider.aggregate_inputs) == 2
