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


def _iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError()
    return date.fromisoformat(value)


def periods_for_request(value):
    """Return the inclusive envelope and exact ordered periods from a plan."""
    raw_periods = value.get("periods")
    if raw_periods is None:
        raw_start, raw_end = value.get("start", ""), value.get("end", "")
        if not raw_start or not raw_end:
            raise LeadRecordInput("Уточните точный период анализа включительно, например 2026-09-01 — 2026-09-30.")
        raw_periods = [{"period_start": raw_start, "period_end": raw_end}]
    if not isinstance(raw_periods, list) or not raw_periods:
        raise LeadRecordInput("Уточните точные периоды анализа; список периодов не должен быть пустым.")
    if len(raw_periods) > 64:
        raise LeadRecordInput("За один запуск можно указать не более 64 периодов.")

    parsed = []
    seen = set()
    try:
        for item in raw_periods:
            if not isinstance(item, dict):
                raise ValueError()
            start_text, end_text = item.get("period_start"), item.get("period_end")
            period_start, period_end = _iso_date(start_text), _iso_date(end_text)
            if period_start > period_end:
                raise ValueError()
            key = (start_text, end_text)
            if key in seen:
                raise ValueError("duplicate")
            seen.add(key)
            parsed.append((period_start, period_end, start_text, end_text))

        raw_start, raw_end = value.get("start", ""), value.get("end", "")
        if bool(raw_start) != bool(raw_end):
            raise ValueError("partial envelope")
        if raw_start and raw_end:
            envelope_start, envelope_end = _iso_date(raw_start), _iso_date(raw_end)
        else:
            envelope_start = min(item[0] for item in parsed)
            envelope_end = max(item[1] for item in parsed)
        if envelope_start > envelope_end or (envelope_end - envelope_start).days > 365:
            raise ValueError("envelope")
        if any(start < envelope_start or end > envelope_end for start, end, _, _ in parsed):
            raise ValueError("outside")
    except (TypeError, ValueError):
        raise LeadRecordInput(
            "Укажите корректные неповторяющиеся периоды внутри общего диапазона (не более 366 календарных дней)."
        ) from None

    periods = [{"period_start": start, "period_end": end} for _, _, start, end in parsed]
    return envelope_start.isoformat(), envelope_end.isoformat(), periods


def _periods_match(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    try:
        actual_values = []
        for item in actual:
            if not isinstance(item, dict):
                return False
            start, end = item.get("period_start"), item.get("period_end")
            if _iso_date(start) > _iso_date(end):
                return False
            actual_values.append((start, end))
        return actual_values == [(item["period_start"], item["period_end"]) for item in expected]
    except (TypeError, ValueError, KeyError):
        return False


def _report_filename(value, fallback):
    name = value if isinstance(value, str) and value else fallback
    stem = name.rsplit(".", 1)[0].casefold() if isinstance(name, str) else ""
    if (
        not isinstance(name, str) or not name or name in {".", ".."}
        or len(name.encode("utf-8")) > 240 or Path(name).name != name
        or any(char in name for char in '<>:"/\\|?*')
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
        or name.endswith((".", " ")) or not name.casefold().endswith(".xlsx")
        or stem in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
    ):
        raise LeadRecordInput("LeadRecord вернул небезопасное имя Excel-файла.")
    return name


def _format_report(value, report):
    periods = value["periods"]
    if len(periods) > 1:
        details = report.get("periods")
        lines = []
        if isinstance(details, list) and len(details) == len(periods) and _periods_match(details, periods):
            for period, item in zip(periods, details):
                lines.append(
                    f"{period['period_start']} — {period['period_end']}: "
                    f"идентификаций {item.get('total_count')}, недозвон {item.get('missed_count')}, "
                    f"качественные {item.get('quality_count')}, сигнал спроса {item.get('demand_count')}"
                )
        if not lines:
            lines.append(f"Выбрано периодов: {len(periods)}. Показатели по каждому периоду приведены в Excel.")
        return "Отчёт содержит отдельные результаты по периодам; общий итог не складываю, так как периоды могут пересекаться.\n" + "\n".join(lines) + f"\nОтчёт #{report['export_number']}. Качественные входят в сигнал спроса; сигнал спроса не равен продажам."
    period = periods[0]
    return (
        f"{period['period_start']} - {period['period_end']}: идентификаций в знаменателе отчёта {report['total_count']}, "
        f"недозвон {report['missed_count']}, качественные {report['quality_count']}, "
        f"сигнал спроса {report['demand_count']}. Отчёт #{report['export_number']}.\n"
        "Качественные входят в сигнал спроса. Это не число продаж."
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
        start_text, end_text, periods = periods_for_request(value)
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
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
            if not _periods_match(saved_run.get("periods"), periods):
                raise LeadRecordInput("Список периодов сохранённого запуска изменился. Уточните задачу заново.", str(run_id))
            if not _same_project_ids(saved_run.get("project_ids"), plan.get("project_ids")):
                raise LeadRecordInput(
                    "Сохранённый запуск использует другой состав проектов. Для текущего состава создайте новую задачу на анализ без старого run_id.",
                    str(run_id),
                )
        value.update(
            group_id=gid, client_id=plan["client_id"], start=start.isoformat(), end=end.isoformat(), periods=periods,
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
        period_lines = "\n".join(
            f"{index}. {item['period_start']} — {item['period_end']}"
            for index, item in enumerate(periods, 1)
        )
        period_label = "Периоды в порядке отчёта:" if len(periods) > 1 else "Период:"
        return value, (f"LeadRecord: {plan['group_name']} (группа {gid}, клиент {plan['client_id']}).\n"
                       f"Проектов: {len(plan['project_ids'])}. Вкладки: {plan.get('saved_sheets', {})}.\n"
                       f"Общий диапазон: {start} — {end}.\n{period_label}\n{period_lines}.{excluded_note} {next_step}")

    async def _wait(self, gid, rid, client_id, project_ids, periods):
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
            if not _periods_match(result.get("periods"), periods):
                raise LeadRecordInput(
                    f"Список периодов запуска {rid} не совпадает с подтверждённым. Уточните задачу заново.", rid,
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
        start, end, periods = periods_for_request(value)
        value.update(start=start, end=end, periods=periods)
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
                args = ["analytics", "prepare", "--group", str(gid)]
                for period in periods:
                    args += ["--period", f"{period['period_start']}:{period['period_end']}"]
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
                gid, rid, value["client_id"], value["project_ids"], periods,
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
                    gid, rid, value["client_id"], value["project_ids"], periods,
                )
            report = result.get("result")
            if not report:
                raise LeadRecordInput("Отчёт пока не готов.", rid)
            if report.get("periods") is not None and not _periods_match(report.get("periods"), periods):
                raise LeadRecordInput("Периоды в готовом Excel не совпадают с подтверждённым списком.", rid)
            output_dir = Path(output)
            filename = _report_filename(
                report.get("download_filename"), f"leadrecord-task-{rid}.xlsx",
            )
            output = output_dir / filename
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
            except FileExistsError:
                try:
                    if output.read_bytes() != content:
                        raise OSError()
                except OSError:
                    raise LeadRecordInput("Файл Excel с таким именем уже существует и отличается от результата.", rid) from None
            except OSError:
                if created_output:
                    try:
                        output.unlink()
                    except OSError:
                        pass
                raise LeadRecordInput("Не удалось сохранить Excel отчёта.", rid) from None
            return _format_report(value, report), report, output
        except LeadRecordInput as exc:
            raise LeadRecordInput(str(exc), exc.run_id or rid, needs_input=exc.needs_input) from None
