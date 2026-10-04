import pytest
from agentbridge.agents.base import GeneralTaskPlan
from agentbridge.application import AgentBridgeApplication
from agentbridge.chats.loader import ChatRegistry
from agentbridge.storage.sqlite import ChatThreadStore


class Planner:
    last_request = ""
    async def plan_general_task(self, **kwargs):
        self.last_request = kwargs["request"]
        return GeneralTaskPlan("synthetic-thread", "Аналитика", "leadrecord_analytics", analytics={})


class Client:
    async def plan(self, request):
        return {"group_id": 8}, "Проверенный план"


@pytest.mark.asyncio
@pytest.mark.parametrize("query_text", ["Аналитика LeadRecord по проекту", "Аналитика ЛК по проекту"])
async def test_explicit_leadrecord_routes_without_telegram_chat_selection(tmp_path, query_text):
    store = ChatThreadStore(tmp_path / "routing.sqlite3")
    app = AgentBridgeApplication(ChatRegistry({}), store, Planner(), owner_chat_id=77,
        leadrecord_client=Client())
    result = await app.handle_owner_query(query_text, update_id=321)
    assert result.general_task_id is not None
    assert result.selection_id is None
    assert store.get_general_task(result.general_task_id).kind == "leadrecord_analytics"
    assert store.is_update_processed(321)


@pytest.mark.asyncio
async def test_named_chat_planner_allows_leadrecord_kind(tmp_path):
    store = ChatThreadStore(tmp_path / "named-routing.sqlite3")
    app = AgentBridgeApplication(ChatRegistry({}), store, Planner(), owner_chat_id=77,
        leadrecord_client=Client())
    result = await app._prepare_general_task("Аналитика по проекту", reminder_only=True)
    assert result.general_task_id is not None
    assert store.get_general_task(result.general_task_id).kind == "leadrecord_analytics"


@pytest.mark.asyncio
async def test_initial_discovery_question_is_durable_and_cannot_execute(tmp_path):
    from agentbridge.leadrecord import LeadRecordInput
    class PendingClient:
        ready = False
        async def plan(self, value):
            if not self.ready:
                raise LeadRecordInput("Какие новые проекты добавить?")
            return {"group_id": 8}, "Уточнённый план"
        async def run(self, *args):
            pytest.fail("unconfirmed incomplete task must not execute")
    client = PendingClient()
    planner = Planner()
    store = ChatThreadStore(tmp_path / "pending.sqlite3")
    app = AgentBridgeApplication(ChatRegistry({}), store, planner, owner_chat_id=77,
        leadrecord_client=client)
    question = await app.handle_owner_query("Аналитика LeadRecord")
    assert question.general_task_id is not None
    assert store.get_general_task(question.general_task_id).payload["needs_input"]
    premature = await app.handle_general_task_action(question.general_task_id, "confirm", 77)
    assert "Сначала уточните" in premature.text
    assert store.get_general_task(question.general_task_id).status == "confirming"
    app.attach_general_task(question.general_task_id, 500)
    assert app.mark_general_task_clarification(question.general_task_id, 501)
    client.ready = True
    clarified = await app.handle_general_task_clarification(77, 501, "Добавь выбранные проекты")
    assert clarified.general_task_id == question.general_task_id
    assert "Вопрос LeadRecord:\nКакие новые проекты добавить?" in planner.last_request
    assert not store.get_general_task(question.general_task_id).payload.get("needs_input")



@pytest.mark.asyncio
async def test_running_job_timeout_can_resume_without_answer(monkeypatch):
    from types import SimpleNamespace
    from agentbridge import leadrecord
    client = leadrecord.LeadRecordClient("rick@example.org")
    ticks = iter([0, 301])
    monkeypatch.setattr(leadrecord, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    async def call(args, compute=False):
        return {"run_id": "pending", "group_id": 8, "client_id": 5, "project_ids": [10],
            "periods": [{"period_start": "2026-09-01", "period_end": "2026-09-30"}],
            "job": {"status": "running"}}
    client.call = call
    with pytest.raises(leadrecord.LeadRecordInput) as error:
        await client._wait(8, "pending", 5, [10], [{"period_start": "2026-09-01", "period_end": "2026-09-30"}])
    assert error.value.run_id == "pending"
    assert error.value.needs_input is False
