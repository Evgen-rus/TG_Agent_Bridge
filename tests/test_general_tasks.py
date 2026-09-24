from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pathlib import Path

from agentbridge.agents.base import GeneralTaskPlan, OwnerQueryAnswer
from agentbridge.application import AgentBridgeApplication, OwnerQueryResult
from agentbridge.chats.loader import ChatConfig, ChatRegistry
from agentbridge.owner_query import OwnerQueryIntent
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import _general_task_keyboard, _owner_query_selection_keyboard


@dataclass
class GeneralProvider:
    plans: list[str] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)

    async def plan_general_task(self, *, request, timezone_name, now_local, thread_id):
        self.plans.append(request)
        if "напом" in request.casefold():
            return GeneralTaskPlan("general-thread", "Завтра в 10:00 напомнить проверить отчёт.", "reminder",
                "2099-09-21T03:00:00+00:00", "2099-09-21 10:00", "Проверить отчёт")
        return GeneralTaskPlan("general-thread", "Проверить код и сообщить результат.", "general")

    async def run_general_task(self, *, request, thread_id):
        self.runs.append(request)
        return OwnerQueryAnswer(thread_id, "Готово без клиентского контекста.")


@pytest.mark.asyncio
async def test_general_task_waits_for_confirmation_and_reuses_thread(tmp_path, chat_registry) -> None:
    provider = GeneralProvider()
    store = ChatThreadStore(tmp_path / "general.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=77)

    selection = await service.handle_owner_query("Рик, проверь код")
    assert isinstance(selection, OwnerQueryResult) and selection.selection_id is not None
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)
    assert prepared.general_task_id is not None and provider.runs == []
    assert store.get_owner_query_thread_id(0) == "general-thread"

    result = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    assert result.text == "Готово без клиентского контекста."
    assert result.general_task_id == prepared.general_task_id
    assert not service.general_task_needs_confirmation(prepared.general_task_id)
    assert provider.runs == ["Рик, проверь код"]
    repeated = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    assert "уже" in repeated.text


@pytest.mark.asyncio
async def test_reply_to_completed_general_task_starts_confirmable_followup(tmp_path, chat_registry) -> None:
    provider = GeneralProvider()
    store = ChatThreadStore(tmp_path / "followup.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=77)
    task_id = store.create_general_task(77, "Проверь VPS", "Проверить VPS", "general", {})
    store.set_general_task_status(task_id, "confirming", "executing")
    store.set_general_task_status(task_id, "executing", "done")
    delivery_id = service.save_pending_owner_query_delivery("VPS проверен", None, general_task_id=task_id)
    service.record_owner_query_delivery(delivery_id, 700)

    followup = await service.handle_general_task_followup(77, 700, "Посмотри снапшоты", update_id=13)

    assert followup is not None and followup.general_task_id != task_id
    assert "Исходная задача:\nПроверь VPS" in provider.plans[-1]
    assert "Предыдущий результат:\nVPS проверен" in provider.plans[-1]
    assert "Новый запрос владельца:\nПосмотри снапшоты" in provider.plans[-1]
    assert store.is_update_processed(13)


@pytest.mark.asyncio
async def test_general_reminder_uses_existing_store_only_after_confirmation(tmp_path, chat_registry) -> None:
    provider = GeneralProvider()
    store = ChatThreadStore(tmp_path / "reminder.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=77)
    selection = await service.handle_owner_query("Рик, напомни завтра проверить отчёт")
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)

    assert store.pending_reminders(77) == []
    result = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    assert "Напоминание #" in result.text
    assert [item.text for item in store.pending_reminders(77)] == ["Проверить отчёт"]
    assert provider.runs == []


@pytest.mark.asyncio
async def test_general_task_clarification_replans_and_cancel_is_terminal(tmp_path, chat_registry) -> None:
    provider = GeneralProvider()
    store = ChatThreadStore(tmp_path / "clarify.sqlite3")
    service = AgentBridgeApplication(chat_registry, store, provider, owner_chat_id=77)
    selection = await service.handle_owner_query("Рик, проверь код")
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)
    assert service.mark_general_task_clarification(prepared.general_task_id, 900)

    revised = await service.handle_general_task_clarification(77, 900, "Только чтение", update_id=12)
    assert revised.general_task_id == prepared.general_task_id
    assert "Только чтение" in provider.plans[-1]
    cancelled = await service.handle_general_task_action(prepared.general_task_id, "cancel", 77)
    assert "Отменено" in cancelled.text
    assert provider.runs == []


@dataclass
class RoutingProvider:
    kind: str = "reminder"
    plans: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def plan_general_task(self, *, request, timezone_name, now_local, thread_id):
        self.plans.append(request)
        if self.kind == "reminder":
            return GeneralTaskPlan(
                "general-thread", "11 октября в 14:00 уточнить готовность.", "reminder",
                "2099-10-11T07:00:00+00:00", "2099-10-11 14:00", "Уточнить у Ильи готовность",
            )
        return GeneralTaskPlan("general-thread", "Разобрать клиентский чат.", "general")

    async def resolve_owner_query_scope(self, *, question, known_chats):
        return OwnerQueryIntent("ambiguous")

    async def answer_owner_query(self, *, question, chat_name, context_pack, thread_id):
        self.queries.append(question)
        return OwnerQueryAnswer("owner-thread", f"Разбор {chat_name}")


def _client_registry() -> ChatRegistry:
    return ChatRegistry({
        -11: ChatConfig(-11, "Риолюкс ЕКБ", "codex", "wiki", Path("rio")),
        -12: ChatConfig(-12, "ОптоБель", "codex", "wiki", Path("opt")),
    })


@pytest.mark.asyncio
async def test_named_client_reminder_skips_owner_query_until_confirmed(tmp_path) -> None:
    provider = RoutingProvider()
    store = ChatThreadStore(tmp_path / "named.sqlite3")
    service = AgentBridgeApplication(_client_registry(), store, provider, owner_chat_id=77)
    text = "Рик, поставь напоминание по Риолюкс ЕКБ на 11 октября в 14:00: уточнить у Ильи готовность к запуску"

    prepared = await service.handle_owner_query(
        text, author_user_id=5, author_username="rickowner", author_name="Евгений",
    )

    assert isinstance(prepared, OwnerQueryResult)
    assert prepared.selection_id is None and prepared.general_task_id is not None
    assert provider.queries == []
    assert "Выбранный клиент: Риолюкс ЕКБ" in provider.plans[-1]
    assert store.pending_reminders(77) == []
    result = await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    reminder = store.pending_reminders(77)[0]
    assert "Напоминание #" in result.text
    assert reminder.related_chat_id == -11
    assert reminder.related_chat_name == "Риолюкс ЕКБ"
    assert reminder.created_by_user_id == 5
    assert reminder.created_by_username == "rickowner"
    assert reminder.text == "Уточнить у Ильи готовность"
    assert provider.queries == []


@pytest.mark.asyncio
async def test_selected_client_reminder_uses_planner_before_owner_query(tmp_path) -> None:
    provider = RoutingProvider()
    store = ChatThreadStore(tmp_path / "selected.sqlite3")
    service = AgentBridgeApplication(_client_registry(), store, provider, owner_chat_id=77)
    text = "Рик, сделай напоминание на 11 октября: уточнить готовность"

    selection = await service.handle_owner_query(text, author_user_id=8, author_username="owner2", author_name="Анна")
    assert isinstance(selection, OwnerQueryResult) and selection.selection_id is not None
    prepared = await service.handle_owner_query_selection(selection.selection_id, "choose", 0, 77)

    assert prepared.general_task_id is not None
    assert provider.queries == []
    assert provider.plans[-1].startswith("Выбранный клиент: Риолюкс ЕКБ")
    assert text in provider.plans[-1]
    await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    reminder = store.pending_reminders(77)[0]
    assert reminder.related_chat_id == -11
    assert reminder.created_by_user_id == 8
    assert reminder.created_by_username == "owner2"


@pytest.mark.asyncio
async def test_general_choice_reminder_stays_unscoped(tmp_path) -> None:
    provider = RoutingProvider()
    store = ChatThreadStore(tmp_path / "general-choice.sqlite3")
    service = AgentBridgeApplication(_client_registry(), store, provider, owner_chat_id=77)
    selection = await service.handle_owner_query(
        "Рик, сделай напоминание на 11 октября", author_user_id=5, author_username="rickowner", author_name="Евгений",
    )
    prepared = await service.handle_owner_query_selection(selection.selection_id, "general", owner_chat_id=77)

    assert "Выбранный клиент:" not in provider.plans[-1]
    await service.handle_general_task_action(prepared.general_task_id, "confirm", 77)
    reminder = store.pending_reminders(77)[0]
    assert reminder.related_chat_id is None and reminder.related_chat_name is None
    assert reminder.created_by_user_id == 5
    assert provider.queries == []


@pytest.mark.asyncio
async def test_selected_client_non_reminder_stays_owner_query(tmp_path) -> None:
    provider = RoutingProvider(kind="general")
    store = ChatThreadStore(tmp_path / "query.sqlite3")
    service = AgentBridgeApplication(_client_registry(), store, provider, owner_chat_id=77)
    text = "Что сейчас по Риолюкс ЕКБ?"

    result = await service.handle_owner_query(text)
    assert isinstance(result, OwnerQueryResult)
    assert result.general_task_id is None
    assert provider.queries == [text]
    assert store.pending_reminders(77) == []

    selection = await service.handle_owner_query("Нужен статус")
    answered = await service.handle_owner_query_selection(selection.selection_id, "choose", 0, 77)
    assert answered.general_task_id is None
    assert provider.queries[-1] == "Нужен статус"
    assert store.pending_reminders(77) == []


def test_general_task_button_text_and_single_project_choice() -> None:
    general = _general_task_keyboard(5).inline_keyboard
    assert [row[0].text for row in general] == [
        "🟢 Да, чувак, погнали!",
        "🟡 Погоди, Рик, есть нюанс...",
        "🔴 Кладу на это болт! *отрыжка*",
    ]
    choice = _owner_query_selection_keyboard(3, [(0, "Acme", False)]).inline_keyboard
    assert [row[0].text for row in choice] == ["Acme", "Общая задача", "Отмена"]
