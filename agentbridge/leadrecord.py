"""Trusted SSH orchestration; the model chooses inputs, never shell commands."""
from __future__ import annotations
import asyncio
from datetime import date
from io import BytesIO
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
import zipfile


class LeadRecordInput(Exception):
    def __init__(self, message, run_id="", *, needs_input=True):
        super().__init__(message)
        self.run_id = run_id
        self.needs_input = needs_input


def _same_project_ids(actual, expected):
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    try:
        return set(actual) == set(expected)
    except TypeError:
        return False


def _period_matches(periods, start, end):
    return (
        isinstance(periods, list) and len(periods) == 1
        and isinstance(periods[0], dict)
        and periods[0].get("period_start") == start
        and periods[0].get("period_end") == end
    )


class LeadRecordClient:
    def __init__(self, target, identity=""):
        if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[a-zA-Z0-9.-]+", target):
            raise ValueError("Invalid LeadRecord SSH target")
        self.target, self.identity = target, identity

    async def _ssh(self, args, timeout=140):
        command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
                   "-o", "StrictHostKeyChecking=yes"]
        if self.identity:
            command += ["-i", self.identity, "-o", "IdentitiesOnly=yes"]
        command += [self.target, shlex.join(args)]
        try:
            return await asyncio.to_thread(subprocess.run, command, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            raise LeadRecordInput("LeadRecord не ответил. Запуск мог быть создан; проверьте его состояние перед повтором.") from None

    async def call(self, args, compute=False):
        process = await self._ssh(["lkctl-compute" if compute else "lkctl", *args])
        try:
            reply = json.loads(process.stdout)
        except (ValueError, UnicodeError):
            raise LeadRecordInput("Не удалось получить ответ LeadRecord через SSH.") from None
        if not reply.get("ok"):
            error = reply.get("error", {})
            extra = reply.get("data") or {}
            details = ""
            if extra.get("statuses"):
                details = "\nНовые статусы: " + "; ".join(extra["statuses"])
            if extra.get("allowed_categories"):
                details += "\nКатегории: " + "; ".join(extra["allowed_categories"])
            raise LeadRecordInput(str(error.get("code", "REQUEST_FAILED")) + ": " + str(error.get("message", "Нужна проверка")) + details)
        if process.returncode:
            raise LeadRecordInput("SSH завершился с ошибкой; автоматически не повторяю операцию.")
        return reply["data"]

    async def plan(self, request):
        value = dict(request)
        try:
            start, end = date.fromisoformat(value.get("start", "")), date.fromisoformat(value.get("end", ""))
            if start > end or (end-start).days >= 366:
                raise ValueError()
        except (TypeError, ValueError):
            raise LeadRecordInput("Укажите точные даты начала и конца анализа.") from None
        raw_gid = value.get("group_id") or 0
        if isinstance(raw_gid, bool) or not re.fullmatch(r"\d+", str(raw_gid)):
            raise LeadRecordInput("Некорректный ID группы.")
        gid = int(raw_gid)
        if not gid:
            client_query = str(value.get("client_query", "")).strip()
            project_query = str(value.get("project_query", "")).strip()
            if not client_query or not project_query:
                raise LeadRecordInput("Укажите клиента и бизнес-проект LeadRecord.")
            clients = await self.call(["clients", "find", client_query, "--limit", "200"])
            if clients["total"] != 1:
                raise LeadRecordInput("Клиент найден неоднозначно. Уточните точное название клиента.")
            cid = clients["items"][0]["client_id"]
            groups, offset = [], 0
            while True:
                page = await self.call(["analytics", "groups", "--client", str(cid), "--limit", "200", "--offset", str(offset)])
                groups += page["items"]
                offset += len(page["items"])
                if offset >= page["total"] or not page["items"]:
                    break
            matching = [x for x in groups if not x["archived"] and project_query.casefold() in x["name"].casefold()]
            if len(matching) != 1:
                names = "; ".join(f"{x['id']}: {x['name']}" for x in groups if not x["archived"])
                raise LeadRecordInput("Уточните настроенную группу аналитики или её ID. Доступны: " + names)
            gid = matching[0]["id"]
        if gid < 1:
            raise LeadRecordInput("Некорректный ID группы.")
        plan = await self.call(["analytics", "plan", "--group", str(gid), "--period", f"{start}:{end}"])
        if not plan.get("settings_ready"):
            raise LeadRecordInput("Нужна первоначальная настройка аналитики в ЛК: " + ", ".join(plan.get("missing_settings", [])))
        run_id = value.get("run_id")
        if run_id:
            saved_run = await self.call(["analytics", "result", "--group", str(gid), "--run", str(run_id)])
            if (
                saved_run.get("run_id") != run_id or saved_run.get("group_id") != gid
                or saved_run.get("client_id") != plan.get("client_id")
            ):
                raise LeadRecordInput("Сохранённый запуск не относится к подтверждённому клиенту и группе.", str(run_id))
            if not _period_matches(saved_run.get("periods"), start.isoformat(), end.isoformat()):
                raise LeadRecordInput("Период сохранённого запуска изменился. Уточните задачу заново.", str(run_id))
            if not _same_project_ids(saved_run.get("project_ids"), plan.get("project_ids")):
                raise LeadRecordInput(
                    "Сохранённый запуск использует другой состав проектов. Для текущего состава создайте новую задачу на анализ без старого run_id.",
                    str(run_id),
                )
        value.update(
            group_id=gid, client_id=plan["client_id"], start=start.isoformat(), end=end.isoformat(),
            project_ids=plan["project_ids"], saved_sheets=plan.get("saved_sheets", {}),
        )
        candidates = plan.get("new_projects", [])
        confirmed = value.get("confirmed_project_ids") or []
        skip_new_projects = value.get("skip_new_projects", False)
        if not isinstance(skip_new_projects, bool):
            raise LeadRecordInput("Нужно явно подтвердить добавляемые проекты или отказаться от всех новых.")
        if skip_new_projects and confirmed:
            raise LeadRecordInput("Нельзя одновременно подтвердить проекты и отказаться от всех новых.")
        if candidates and not confirmed and not skip_new_projects:
            raise LeadRecordInput("Найдены новые проекты. Укажите ID для добавления или явно откажитесь от всех новых: " + "; ".join(f"{x.get('project_id', x.get('id'))}: {x['name']}" for x in candidates))
        next_step = (
            f"Продолжу существующий запуск {value['run_id']} и отправлю Excel сюда."
            if value.get("run_id") else "Обновлю таблицу, выполню анализ и отправлю Excel сюда."
        )
        excluded_note = ""
        if skip_new_projects and candidates:
            excluded = "; ".join(f"{x.get('project_id', x.get('id'))}: {x['name']}" for x in candidates)
            excluded_note = f"\nНовые проекты не добавляю по вашему выбору: {excluded}."
        return value, (f"LeadRecord: {plan['group_name']} (группа {gid}, клиент {plan['client_id']}).\n"
                       f"Проектов: {len(plan['project_ids'])}. Вкладки: {plan.get('saved_sheets', {})}.\n"
                       f"Период: {start} - {end}.{excluded_note} {next_step}")

    async def _wait(self, gid, rid, client_id, project_ids, start, end):
        deadline = time.monotonic() + 300
        while True:
            result = await self.call(["analytics", "result", "--group", str(gid), "--run", rid])
            if (
                result.get("run_id") != rid or result.get("group_id") != gid
                or result.get("client_id") != client_id
            ):
                raise LeadRecordInput(
                    f"Ответ LeadRecord не относится к подтверждённому клиенту и запуску {rid} этой группы.", rid,
                )
            if not _same_project_ids(result.get("project_ids"), project_ids):
                raise LeadRecordInput(
                    f"Состав проектов запуска {rid} не совпадает с подтверждённым. Уточните задачу заново.", rid,
                )
            periods = result.get("periods")
            if not _period_matches(periods, start, end):
                raise LeadRecordInput(
                    f"Период запуска {rid} не совпадает с подтверждённым периодом. Уточните задачу заново.", rid,
                )
            job = result.get("job") or {}
            if job.get("status") not in {"queued", "running"}:
                if job.get("status") == "failed" or result.get("status") == "failed":
                    raise LeadRecordInput(f"Ошибка обработки запуска {rid}; автоматически не повторяю.", rid)
                return result
            if time.monotonic() >= deadline:
                raise LeadRecordInput(f"Запуск {rid} ещё выполняется. Продолжите этот run_id, не создавайте новый.", rid, needs_input=False)
            await asyncio.sleep(2)

    async def run(self, value, output, remember_run):
        gid = value["group_id"]
        rid = value.get("run_id", "")
        try:
            current = await self.call(["analytics", "plan", "--group", str(gid)])
            if (
                current.get("client_id") != value.get("client_id")
                or not _same_project_ids(current.get("project_ids"), value.get("project_ids"))
                or current.get("saved_sheets", {}) != value.get("saved_sheets", {})
            ):
                raise LeadRecordInput("Клиент, состав или вкладки группы изменились после подтверждения. Уточните задачу заново.", rid)
            if not rid:
                args = ["analytics", "prepare", "--group", str(gid), "--period", f"{value['start']}:{value['end']}"]
                if value.get("confirmed_project_ids"):
                    args += ["--confirm-projects", ",".join(map(str, value["confirmed_project_ids"]))]
                elif value.get("skip_new_projects"):
                    args += ["--confirm-projects", "none"]
                prepared = await self.call(args, compute=True)
                rid = prepared["run_id"]
                added_project_ids = prepared.get("added_project_ids") or []
                if added_project_ids:
                    value["project_ids"] = sorted(set(value["project_ids"]) | set(added_project_ids))
                remember_run(rid)
            result = await self._wait(
                gid, rid, value["client_id"], value["project_ids"], value["start"], value["end"],
            )
            if not result.get("result"):
                assignments = value.get("status_rules") or []
                if assignments:
                    args = ["analytics", "confirm-statuses", "--group", str(gid), "--run", rid]
                    for item in assignments:
                        args += ["--assign", f"{item['status']}={item['category']}"]
                    await self.call(args, compute=True)
                await self.call(["analytics", "run", "--group", str(gid), "--run", rid], compute=True)
                result = await self._wait(
                    gid, rid, value["client_id"], value["project_ids"], value["start"], value["end"],
                )
            report = result.get("result")
            if not report:
                raise LeadRecordInput("Отчёт пока не готов.", rid)
            process = await self._ssh(["fetch", str(gid), str(report["id"])])
            content = process.stdout
            if process.returncode or len(content) > 100*1024*1024:
                raise LeadRecordInput("Не удалось получить Excel отчёта.", rid)
            try:
                with zipfile.ZipFile(BytesIO(content)) as book:
                    if not {"xl/workbook.xml", "[Content_Types].xml"}.issubset(book.namelist()):
                        raise ValueError()
            except (ValueError, zipfile.BadZipFile):
                raise LeadRecordInput("Полученный файл не является XLSX.", rid) from None
            created_output = False
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open("xb") as stream:
                    created_output = True
                    stream.write(content)
            except OSError:
                if created_output:
                    try:
                        output.unlink()
                    except OSError:
                        pass
                raise LeadRecordInput("Не удалось сохранить Excel отчёта.", rid) from None
            return (f"{value['start']} - {value['end']}: идентификаций в знаменателе отчёта {report['total_count']}, "
                    f"недозвон {report['missed_count']}, качественные {report['quality_count']}, "
                    f"сигнал спроса {report['demand_count']}. Отчёт #{report['export_number']}.\n"
                    "Качественные входят в сигнал спроса. Это не число продаж."), report
        except LeadRecordInput as exc:
            raise LeadRecordInput(str(exc), exc.run_id or rid, needs_input=exc.needs_input) from None
