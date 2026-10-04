from __future__ import annotations

from io import BytesIO
import subprocess
import zipfile

import pytest

from agentbridge.agents.base import GeneralTaskPlan
from agentbridge.application import AgentBridgeApplication
from agentbridge.leadrecord import LeadRecordClient, LeadRecordInput, _report_filename, periods_for_request
from agentbridge.storage.sqlite import ChatThreadStore


START = "2026-09-01"
END = "2026-09-30"
PERIOD = {"period_start": START, "period_end": END}
MONTH_AND_WEEKS = [
    PERIOD,
    {"period_start": "2026-09-01", "period_end": "2026-09-06"},
    {"period_start": "2026-09-07", "period_end": "2026-09-13"},
    {"period_start": "2026-09-14", "period_end": "2026-09-20"},
    {"period_start": "2026-09-21", "period_end": "2026-09-27"},
    {"period_start": "2026-09-28", "period_end": "2026-09-30"},
]
REPORT = {
    "id": 91, "total_count": 20, "missed_count": 5, "quality_count": 8,
    "demand_count": 10, "export_number": 7,
    "download_filename": "[LR174]_Эпкара_Аналитика_Данные_2026-09-01_2026-09-30.xlsx",
}


def _xlsx_bytes() -> bytes:
    data = BytesIO()
    with zipfile.ZipFile(data, "w") as book:
        book.writestr("[Content_Types].xml", "<Types/>")
        book.writestr("xl/workbook.xml", "<workbook/>")
    return data.getvalue()


def _install_completed_workflow(client, *, added_project_ids=(20,)):
    calls = []
    project_ids = sorted({10, *added_project_ids})
    results = [
        {"run_id": "run-synthetic", "group_id": 8, "client_id": 5, "project_ids": project_ids, "periods": [PERIOD], "job": {"status": "running"}, "result": None},
        {"run_id": "run-synthetic", "group_id": 8, "client_id": 5, "project_ids": project_ids, "periods": [PERIOD], "job": {"status": "matched"}, "result": None},
        {"run_id": "run-synthetic", "group_id": 8, "client_id": 5, "project_ids": project_ids, "periods": [PERIOD], "job": {"status": "done"}, "result": REPORT},
    ]

    async def fake_call(args, compute=False):
        calls.append((list(args), compute))
        if args[:2] == ["analytics", "plan"]:
            return {"client_id": 5, "project_ids": [10], "saved_sheets": {"Лиды": "A"}}
        if args[:2] == ["analytics", "prepare"]:
            return {"run_id": "run-synthetic", "added_project_ids": list(added_project_ids)}
        if args[:2] == ["analytics", "result"]:
            return results.pop(0)
        if args[:2] == ["analytics", "run"]:
            return {"run_id": "run-synthetic"}
        raise AssertionError(f"Unexpected LeadRecord call: {args!r}")

    async def fake_ssh(args, timeout=140):
        assert args == ["fetch", "8", str(REPORT["id"])]
        return subprocess.CompletedProcess(args, 0, stdout=_xlsx_bytes(), stderr=b"")

    client.call = fake_call
    client._ssh = fake_ssh
    return calls


@pytest.mark.asyncio
async def test_prepare_wait_run_and_fetch_synthetic_xlsx(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = _install_completed_workflow(client)
    value = {
        "group_id": 8, "client_id": 5, "start": START, "end": END, "project_ids": [10],
        "saved_sheets": {"Лиды": "A"}, "confirmed_project_ids": [20],
        "status_rules": [],
    }
    remembered = []
    output = tmp_path / "owner_generated" / "task-1"

    text, report, path = await client.run(value, output, lambda run_id: remembered.append((run_id, list(value["project_ids"]))))

    assert report == REPORT
    assert path.name == REPORT["download_filename"]
    assert path.read_bytes() == _xlsx_bytes()
    assert "2026-09-01 - 2026-09-30" in text
    assert remembered == [("run-synthetic", [10, 20])]
    assert [args[0][1] for args in calls] == ["plan", "prepare", "result", "result", "run", "result"]
    assert calls[1] == (["analytics", "prepare", "--group", "8", "--period", f"{START}:{END}", "--confirm-projects", "20"], True)
    assert calls[4] == (["analytics", "run", "--group", "8", "--run", "run-synthetic"], True)


@pytest.mark.asyncio
async def test_month_and_weeks_confirmation_prepare_summary_and_filename(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = []
    report_periods = [
        {**period, "total_count": total, "missed_count": 2, "quality_count": 1, "demand_count": 3}
        for period, total in zip(MONTH_AND_WEEKS, [100, 20, 21, 22, 23, 14])
    ]
    report = {**REPORT, "periods": report_periods}
    result_queue = [
        {"run_id": "run-multi", "group_id": 8, "client_id": 5, "project_ids": [10],
         "periods": MONTH_AND_WEEKS, "job": {"status": "matched"}, "result": None},
        {"run_id": "run-multi", "group_id": 8, "client_id": 5, "project_ids": [10],
         "periods": MONTH_AND_WEEKS, "job": {"status": "done"}, "result": report},
    ]

    async def fake_call(args, compute=False):
        calls.append((list(args), compute))
        if args[:2] == ["analytics", "plan"] and "--period" in args:
            assert args[args.index("--period") + 1] == f"{START}:{END}"
            return {"settings_ready": True, "group_name": "Эпкара", "client_id": 5,
                    "project_ids": [10], "saved_sheets": {"Данные": "A"}, "new_projects": []}
        if args[:2] == ["analytics", "plan"]:
            return {"client_id": 5, "project_ids": [10], "saved_sheets": {"Данные": "A"}}
        if args[:2] == ["analytics", "prepare"]:
            return {"run_id": "run-multi", "added_project_ids": []}
        if args[:2] == ["analytics", "result"]:
            return result_queue.pop(0)
        if args[:2] == ["analytics", "run"]:
            return {"run_id": "run-multi"}
        raise AssertionError(f"Unexpected LeadRecord call: {args!r}")

    async def fake_ssh(args, timeout=140):
        assert args == ["fetch", "8", str(REPORT["id"])]
        return subprocess.CompletedProcess(args, 0, stdout=_xlsx_bytes(), stderr=b"")

    client.call = fake_call
    client._ssh = fake_ssh
    value, confirmation = await client.plan({
        "group_id": 8, "start": START, "end": END, "periods": MONTH_AND_WEEKS,
    })
    assert "Общий диапазон: 2026-09-01 — 2026-09-30" in confirmation
    assert "Периоды в порядке отчёта:" in confirmation
    assert confirmation.index("1. 2026-09-01 — 2026-09-30") < confirmation.index("2. 2026-09-01 — 2026-09-06")
    assert value["periods"] == MONTH_AND_WEEKS

    summary, completed_report, output = await client.run(value, tmp_path / "owner_generated" / "task-2", lambda _: None)

    prepare = next(args for args, compute in calls if args[:2] == ["analytics", "prepare"])
    assert prepare == ["analytics", "prepare", "--group", "8", *sum((["--period", f"{p['period_start']}:{p['period_end']}"] for p in MONTH_AND_WEEKS), [])]
    assert completed_report == report
    assert output.name == report["download_filename"]
    assert output.read_bytes() == _xlsx_bytes()
    assert "идентификаций 100" in summary
    assert "идентификаций 20" in summary
    assert "идентификаций в знаменателе отчёта 200" not in summary
    assert "общий итог не складываю" in summary


@pytest.mark.parametrize("request_data", [
    {"start": START, "end": END, "periods": [PERIOD, PERIOD]},
    {"start": START, "end": END, "periods": [{"period_start": END, "period_end": START}]},
    {"start": START, "end": END, "periods": [{"period_start": "2026-08-31", "period_end": END}]},
    {"start": "2025-09-01", "end": "2026-09-30", "periods": [PERIOD]},
])
def test_period_validation_rejects_duplicates_reversed_outside_and_long_envelopes(request_data) -> None:
    with pytest.raises(LeadRecordInput):
        periods_for_request(request_data)


def test_period_validation_rejects_more_than_64_ranges_and_unsafe_filenames() -> None:
    request = {"start": START, "end": END, "periods": [PERIOD] * 65}
    with pytest.raises(LeadRecordInput, match="не более 64"):
        periods_for_request(request)
    assert _report_filename("[LR174]_Эпкара_Аналитика_Данные.xlsx", "fallback.xlsx") == "[LR174]_Эпкара_Аналитика_Данные.xlsx"
    for name in ("../report.xlsx", "C:\\report.xlsx", "bad\nname.xlsx", "bad?.xlsx", "bad.xlsx "):
        with pytest.raises(LeadRecordInput):
            _report_filename(name, "fallback.xlsx")


@pytest.mark.asyncio
@pytest.mark.parametrize("periods", [[], [PERIOD, {"period_start": "2026-08-01", "period_end": END}], [{"period_start": "2026-08-01", "period_end": END}]])
async def test_run_refuses_mismatched_or_multiple_periods_before_analysis(tmp_path, periods) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = []

    async def fake_call(args, compute=False):
        calls.append((list(args), compute))
        if args[:2] == ["analytics", "plan"]:
            return {"client_id": 5, "project_ids": [10], "saved_sheets": {}}
        if args[:2] == ["analytics", "prepare"]:
            return {"run_id": "run-wrong-period", "added_project_ids": []}
        if args[:2] == ["analytics", "result"]:
            return {"run_id": "run-wrong-period", "group_id": 8, "client_id": 5, "project_ids": [10], "periods": periods, "job": {"status": "matched"}, "result": None}
        raise AssertionError(f"Analysis must stop before this call: {args!r}")

    client.call = fake_call
    async def no_fetch(args, timeout=140):
        raise AssertionError("A mismatched run must not be exported")
    client._ssh = no_fetch
    value = {"group_id": 8, "client_id": 5, "start": START, "end": END, "project_ids": [10], "saved_sheets": {}}

    with pytest.raises(LeadRecordInput, match="не совпадает") as error:
        await client.run(value, tmp_path / "report.xlsx", lambda _: None)

    assert error.value.run_id == "run-wrong-period"
    assert not any(args[0][:2] == ["analytics", "run"] for args in calls)




@pytest.mark.asyncio
async def test_resume_plan_uses_current_scope_only_when_it_matches_saved_run(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = []

    async def fake_call(args, compute=False):
        calls.append(list(args))
        if args[:2] == ["analytics", "plan"]:
            return {
                "settings_ready": True, "group_name": "Main", "client_id": 5,
                "project_ids": [10, 20], "saved_sheets": {"Лиды": "A"}, "new_projects": [],
            }
        if args[:2] == ["analytics", "result"]:
            return {
                "run_id": "run-previous", "group_id": 8, "client_id": 5,
                "project_ids": [20, 10], "periods": [PERIOD], "job": {"status": "done"},
                "result": REPORT,
            }
        raise AssertionError(f"Unexpected resume check: {args!r}")

    client.call = fake_call
    value, confirmation = await client.plan({
        "group_id": 8, "run_id": "run-previous", "start": START, "end": END,
    })

    assert value["project_ids"] == [10, 20]
    assert "Продолжу существующий запуск run-previous" in confirmation
    assert "Обновлю таблицу" not in confirmation
    assert calls == [
        ["analytics", "plan", "--group", "8", "--period", f"{START}:{END}"],
        ["analytics", "result", "--group", "8", "--run", "run-previous"],
    ]


@pytest.mark.asyncio
async def test_resume_plan_rejects_saved_run_with_different_project_set() -> None:
    client = LeadRecordClient("rick@example.org")

    async def fake_call(args, compute=False):
        if args[:2] == ["analytics", "plan"]:
            return {
                "settings_ready": True, "group_name": "Main", "client_id": 5,
                "project_ids": [10, 20], "saved_sheets": {}, "new_projects": [],
            }
        return {
            "run_id": "run-old-scope", "group_id": 8, "client_id": 5,
            "project_ids": [10], "periods": [PERIOD], "job": {"status": "done"},
            "result": REPORT,
        }

    client.call = fake_call

    with pytest.raises(LeadRecordInput, match="другой состав проектов") as error:
        await client.plan({"group_id": 8, "run_id": "run-old-scope", "start": START, "end": END})

    assert error.value.run_id == "run-old-scope"


@pytest.mark.asyncio
async def test_resume_plan_rejects_same_envelope_with_reordered_periods() -> None:
    client = LeadRecordClient("rick@example.org")
    reordered = [MONTH_AND_WEEKS[1], MONTH_AND_WEEKS[0], *MONTH_AND_WEEKS[2:]]

    async def fake_call(args, compute=False):
        if args[:2] == ["analytics", "plan"]:
            return {"settings_ready": True, "group_name": "Main", "client_id": 5,
                    "project_ids": [10], "saved_sheets": {}, "new_projects": []}
        return {"run_id": "run-old-order", "group_id": 8, "client_id": 5,
                "project_ids": [10], "periods": reordered, "job": {"status": "done"}, "result": REPORT}

    client.call = fake_call
    with pytest.raises(LeadRecordInput, match="Список периодов сохранённого запуска изменился") as error:
        await client.plan({"group_id": 8, "run_id": "run-old-order", "start": START, "end": END,
                           "periods": MONTH_AND_WEEKS})
    assert error.value.run_id == "run-old-order"


@pytest.mark.asyncio
async def test_run_rejects_project_scope_changed_after_confirmation(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = []

    async def fake_call(args, compute=False):
        calls.append(list(args))
        if args[:2] == ["analytics", "plan"]:
            return {"client_id": 5, "project_ids": [10, 20], "saved_sheets": {}}
        return {
            "run_id": "run-scope-check", "group_id": 8, "client_id": 5,
            "project_ids": [10], "periods": [PERIOD], "job": {"status": "done"},
            "result": REPORT,
        }

    async def no_fetch(args, timeout=140):
        raise AssertionError("A run with a different project scope must not be exported")

    client.call = fake_call
    client._ssh = no_fetch
    value = {
        "group_id": 8, "client_id": 5, "start": START, "end": END,
        "run_id": "run-scope-check", "project_ids": [10, 20], "saved_sheets": {},
    }

    with pytest.raises(LeadRecordInput, match="не совпадает с подтверждённым") as error:
        await client.run(value, tmp_path / "report.xlsx", lambda _: None)

    assert error.value.run_id == "run-scope-check"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_plan_requires_explicit_add_or_decline_for_new_projects() -> None:
    client = LeadRecordClient("rick@example.org")

    async def fake_call(args, compute=False):
        return {
            "settings_ready": True, "group_name": "Main", "client_id": 5,
            "project_ids": [10], "saved_sheets": {"Лиды": "A"},
            "new_projects": [{"project_id": 20, "name": "Новая кампания"}],
        }

    client.call = fake_call

    with pytest.raises(LeadRecordInput, match="явно откажитесь"):
        await client.plan({
            "group_id": 8, "start": START, "end": END,
            "confirmed_project_ids": [], "skip_new_projects": False,
        })

    with pytest.raises(LeadRecordInput, match="одновременно подтвердить"):
        await client.plan({
            "group_id": 8, "start": START, "end": END,
            "confirmed_project_ids": [20], "skip_new_projects": True,
        })


@pytest.mark.asyncio
async def test_explicit_decline_allows_plan_and_prepare_uses_none(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    calls = []
    results = [
        {"run_id": "run-synthetic", "group_id": 8, "client_id": 5, "project_ids": [10],
         "periods": [PERIOD], "job": {"status": "matched"}, "result": None},
        {"run_id": "run-synthetic", "group_id": 8, "client_id": 5, "project_ids": [10],
         "periods": [PERIOD], "job": {"status": "done"}, "result": REPORT},
    ]

    async def fake_call(args, compute=False):
        calls.append((list(args), compute))
        if args[:2] == ["analytics", "plan"] and "--period" in args:
            return {
                "settings_ready": True, "group_name": "Main", "client_id": 5,
                "project_ids": [10], "saved_sheets": {"Лиды": "A"},
                "new_projects": [{"project_id": 20, "name": "Новая кампания"}],
            }
        if args[:2] == ["analytics", "plan"]:
            return {"client_id": 5, "project_ids": [10], "saved_sheets": {"Лиды": "A"}}
        if args[:2] == ["analytics", "prepare"]:
            return {"run_id": "run-synthetic", "added_project_ids": []}
        if args[:2] == ["analytics", "result"]:
            return results.pop(0)
        if args[:2] == ["analytics", "run"]:
            return {"run_id": "run-synthetic"}
        raise AssertionError(f"Unexpected LeadRecord call: {args!r}")

    async def fake_ssh(args, timeout=140):
        return subprocess.CompletedProcess(args, 0, stdout=_xlsx_bytes(), stderr=b"")

    client.call = fake_call
    client._ssh = fake_ssh
    value, confirmation = await client.plan({
        "group_id": 8, "start": START, "end": END,
        "confirmed_project_ids": [], "skip_new_projects": True,
    })
    assert "Новые проекты не добавляю по вашему выбору: 20: Новая кампания" in confirmation
    await client.run(value, tmp_path / "report.xlsx", lambda _: None)

    prepare_call = next(args for args, compute in calls if args[:2] == ["analytics", "prepare"])
    assert prepare_call == [
        "analytics", "prepare", "--group", "8", "--period", f"{START}:{END}",
        "--confirm-projects", "none",
    ]


@pytest.mark.asyncio
async def test_plan_rejects_invalid_group_id_as_input_error() -> None:
    client = LeadRecordClient("rick@example.org")

    with pytest.raises(LeadRecordInput, match="Некорректный ID группы"):
        await client.plan({"group_id": "not-an-id", "start": START, "end": END})


@pytest.mark.asyncio
async def test_output_write_error_is_safe_and_keeps_existing_file(tmp_path) -> None:
    client = LeadRecordClient("rick@example.org")
    _install_completed_workflow(client, added_project_ids=())
    output = tmp_path / "owner_generated"
    output.mkdir()
    existing = output / REPORT["download_filename"]
    existing.write_bytes(b"keep-existing")
    value = {"group_id": 8, "client_id": 5, "start": START, "end": END, "project_ids": [10], "saved_sheets": {"Лиды": "A"}}

    with pytest.raises(LeadRecordInput, match="Файл Excel с таким именем") as error:
        await client.run(value, output, lambda _: None)

    assert error.value.run_id == "run-synthetic"
    assert existing.read_bytes() == b"keep-existing"


@pytest.mark.asyncio
async def test_application_confirmation_persists_added_projects_and_resumes_run(tmp_path, chat_registry) -> None:
    class Provider:
        async def plan_general_task(self, *, request, timezone_name, now_local, thread_id):
            return GeneralTaskPlan(
                "general-thread", "Повторить аналитику выбранной группы.", "leadrecord_analytics",
                analytics={
                    "group_id": 8, "start": START, "end": END,
                    "confirmed_project_ids": [20], "status_rules": [],
                },
            )

    class FakeLeadRecordClient:
        def __init__(self):
            self.calls = []

        async def plan(self, request):
            return ({
                **request, "client_id": 5, "project_ids": [10], "saved_sheets": {"Лиды": "A"},
            }, "Confirmed LeadRecord plan")

        async def run(self, value, output, remember_run):
            self.calls.append(dict(value))
            if len(self.calls) == 1:
                value["project_ids"] = [10, 20]
                remember_run("run-synthetic")
                raise LeadRecordInput("Запуск уже подготовлен; проверьте статус.", "run-synthetic", needs_input=False)
            return "Отчёт готов", REPORT, output / REPORT["download_filename"]

    provider = Provider()
    client = FakeLeadRecordClient()
    store = ChatThreadStore(tmp_path / "leadrecord-application.sqlite3")
    service = AgentBridgeApplication(
        chat_registry, store, provider, owner_chat_id=77, leadrecord_client=client,
        generated_media_dir=tmp_path / "runtime" / "media" / "owner_generated",
    )
    prepared = await service._prepare_general_task("Проверь аналитику группы 8")
    task_id = prepared.general_task_id
    assert task_id is not None

    first = await service.handle_general_task_action(task_id, "confirm", 77)
    saved = store.get_general_task(task_id)
    assert "Запуск уже подготовлен" in first.text
    assert saved.status == "confirming"
    assert saved.payload["analytics"]["run_id"] == "run-synthetic"
    assert saved.payload["analytics"]["project_ids"] == [10, 20]

    result = await service.handle_general_task_action(task_id, "confirm", 77)

    assert result.media_kind == "document"
    assert result.media_path.replace("\\", "/").endswith(f"leadrecord-task-{task_id}/{REPORT['download_filename']}")
    assert store.get_general_task(task_id).status == "done"
    assert client.calls[1]["run_id"] == "run-synthetic"
    assert client.calls[1]["project_ids"] == [10, 20]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_analytics,old_periods", [
    ({"group_id": 8, "start": START, "end": END, "periods": [PERIOD, {"period_start": "2026-09-01", "period_end": "2026-09-14"}]}, MONTH_AND_WEEKS),
    ({"group_id": 9, "start": START, "end": END, "periods": MONTH_AND_WEEKS}, MONTH_AND_WEEKS),
])
async def test_application_does_not_restore_run_for_changed_periods_or_group(tmp_path, chat_registry, new_analytics, old_periods) -> None:
    class Provider:
        async def plan_general_task(self, *, request, timezone_name, now_local, thread_id):
            return GeneralTaskPlan("general-thread", "Изменить анализ", "leadrecord_analytics", analytics=new_analytics)

    class FakeLeadRecordClient:
        request = None
        async def plan(self, request):
            self.request = dict(request)
            return ({**request, "client_id": 5, "project_ids": [10], "saved_sheets": {}}, "План")

    store = ChatThreadStore(tmp_path / "resume-scope.sqlite3")
    old_analytics = {"group_id": 8, "client_id": 5, "start": START, "end": END,
                     "periods": old_periods, "run_id": "run-old", "project_ids": [10],
                     "saved_sheets": {}}
    task_id = store.create_general_task(77, "Старая задача", "Старая задача", "leadrecord_analytics",
                                        {"analytics": old_analytics})
    leadrecord_client = FakeLeadRecordClient()
    app = AgentBridgeApplication(chat_registry, store, Provider(), owner_chat_id=77,
                                 leadrecord_client=leadrecord_client)

    await app._prepare_general_task("Новый запрос", task_id)

    assert leadrecord_client.request is not None
    assert not leadrecord_client.request.get("run_id")
