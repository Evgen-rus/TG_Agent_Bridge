from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai_codex import Codex, Sandbox

from .base import AgentAction, AgentReply, FeedbackAnalysis

_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["reply", "ask_owner", "observe", "no_action"]},
        "situation": {"type": "string"},
        "suggested_reply": {"type": "string"},
        "observation": {"type": "string"},
        "unknowns": {"type": "string"},
        "owner_question": {"type": "string"},
        "should_notify": {"type": "boolean"},
        "confidence": {"type": "number"},
        "needs_critique": {"type": "boolean"},
        "candidate_state": {"type": ["object", "null"]},
    },
    "required": [
        "action", "situation", "suggested_reply", "observation", "unknowns",
        "owner_question", "should_notify", "confidence", "needs_critique",
    ],
    "additionalProperties": False,
}
_FEEDBACK_SCHEMA = {
    "type": "object",
    "properties": {
        "understanding": {"type": "string"},
        "proposed_rule": {"type": ["string", "null"]},
        "conflict_key": {"type": ["string", "null"]},
        "scope": {"type": "string", "enum": ["client", "global"]},
        "regenerate_current": {"type": "boolean"},
        "revision_instruction": {"type": ["string", "null"]},
    },
    "required": ["understanding", "proposed_rule", "conflict_key", "scope", "regenerate_current", "revision_instruction"],
    "additionalProperties": False,
}
_OWNER_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
_OWNER_VOICE = """Голос только для команды, и только в observation, owner_question и ответах во внутреннем чате.
Сначала думай нейтрально и выбери action. Характер подключай в самом конце, как короткую формулировку уже готового вывода. Не трать длинное рассуждение на стиль и не меняй action ради остроты.
suggested_reply клиенту — обычный деловой текст без этой окраски. observation — 1–3 коротких предложения.

Ты наблюдательный второй пилот. Можно слегка заострить формулировку, если есть конкретное основание:
- несостыковка;
- бессмысленное действие «просто чтобы ответить»;
- очевидная версия, у которой есть дыра;
- несогласие с предлагаемым шагом, не с людьми.

Ирония сухая, не обязательная, максимум одна короткая реплика. Если ситуация простая — пиши прямо, без окраски.
Нельзя: сарказм в каждом сообщении, хамство, мат, цинизм, поза «я умнее всех», уверенность без данных, шутки ради шуток, копирование примеров дословно.

Хорошо:
- «Тут я бы не изображал ясновидящего. Не хватает одного факта: кто у них сейчас принимает решение?»
- «Формально можно ответить. Практически — бессмысленно: клиент уже сам закрыл этот вопрос следующим сообщением.»
- «Тут есть маленькая проблема: две наши договорённости друг другу противоречат. Я бы сначала разобрался с этим.»
Плохо: «Ну да, гениальный план, как всегда»; «Клиент опять несёт чушь»; казённое «Недостаточно информации», если можно назвать, какого факта не хватает."""
_INSTRUCTIONS = """Ты помощник команды по рабочим Telegram-чатам. Режим только suggest: ничего не отправляй клиенту и не выполняй внешние действия.
Источник правды — context pack: wiki, текущее состояние чата, недавняя история, текущий эпизод, подтверждённая память, правила и опыт. Codex thread — только continuity, не память.
Различай факты, гипотезы, договорённости и открытые вопросы. Не выдумывай недостающие факты. Если данных мало — признай неопределённость.
Используй длинное рассуждение на анализ ситуации, а не на характер. Стиль команды — только финальная формулировка observation/owner_question.

Выбери одно действие:
- reply: клиенту сейчас нужен конкретный ответ. suggested_reply — обычный деловой текст под стиль чата, без характера помощника.
- ask_owner: не хватает одного важного факта. owner_question — один короткий вопрос команде. Не выдумывай ответ.
- observe: отвечать клиенту не нужно, но команде стоит увидеть важное изменение или риск.
- no_action: ничего сообщать не нужно. Если более поздние сообщения уже закрыли ранний вопрос — no_action или observe, не предлагай устаревший ответ.

should_notify=true только для reply, ask_owner и observe.
""" + _OWNER_VOICE + """
candidate_state — только изменившиеся поля текущего состояния (summary, stage, facts, decisions, agreements, commitments, waiting_from_client, waiting_from_us, open_questions, risks, unknowns, next_step, participants).
needs_critique=true только при низкой уверенности, конфликте памяти или важном reply/ask_owner на слабой гипотезе. При высоком reasoning по умолчанию хватает одного хода.
Верни JSON по схеме."""
_FEEDBACK_INSTRUCTIONS = """Ты разбираешь замечание владельца к рекомендации помощника.
Сформулируй простыми словами, как понял замечание. Сам определи, является ли оно:
разовым исправлением текущего ответа, постоянным правилом или одновременно обоими.
Не превращай разовую фактическую правку в постоянное правило без оснований.
scope=global допустим только при явном указании владельца применять для всех клиентов.
Для постоянного правила дай короткий proposed_rule и стабильный смысловой conflict_key,
чтобы новое правило той же темы могло заменить старое. Иначе оба поля null.
regenerate_current=true, если замечание требует исправить текущую рекомендацию.
revision_instruction описывает только необходимое исправление текущего ответа или null.
Не применяй замечание: только интерпретируй для подтверждения человеком.
Понимание пиши коротко и по делу, без театральности."""
_OWNER_QUERY_INSTRUCTIONS = """Ты отвечаешь команде во внутреннем чате на вопрос о клиентском чате.
Опирайся только на context pack. Не выдумывай. Если данных нет — прямо скажи, какого факта не хватает.
Длинное рассуждение используй на проверку фактов. Сам ответ команде короткий.
Не предлагай отправлять это клиенту.
""" + _OWNER_VOICE
_CRITIQUE_INSTRUCTIONS = """Проверь предыдущий JSON-ответ как нейтральный аналитик. Исправь выдуманные факты, устаревшие рекомендации и слабые гипотезы, выданные как факты. Если более поздние сообщения закрыли вопрос — не предлагай reply на него. Не усиливай уверенность и не меняй action ради более острого тона. Голос команды может остаться в observation/owner_question, но смысл должен стать точнее. Верни тот же JSON schema."""


class CodexProvider:
    def __init__(self, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "xhigh", cwd: Path | None = None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cwd = str((cwd or Path.cwd()).resolve())
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Codex reasoning effort: {reasoning_effort}")

    async def suggest(self, *, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, context_pack: str = "") -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, None, context_pack)

    async def revise(self, *, feedback: str, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str, context_pack: str = "") -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, feedback, context_pack)

    async def critique(self, *, previous: AgentReply, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, context_pack: str = "") -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, f"Самопроверка предыдущего вывода:\n{previous.situation}", context_pack, True)

    def _suggest_sync(self, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, revision: str | None, context_pack: str = "", critique: bool = False) -> AgentReply:
        rules_text = "\n".join(f"- {rule}" for rule in rules) or "(нет)"
        pack = context_pack.strip() or f"Wiki чата:\n{wiki or '(wiki пуста)'}\n\nПодтвержденные правила:\n{rules_text}"
        prompt = f"Чат: {chat_name}\nОтправитель: {sender_name}\n\n{pack}\n\nСообщение:\n{message}"
        if revision:
            prompt += f"\n\nПодтвержденное замечание владельца. Пересоздай текущую рекомендацию:\n{revision}"
        instructions = _CRITIQUE_INSTRUCTIONS if critique else _INSTRUCTIONS
        with Codex() as codex:
            if thread_id:
                thread = codex.thread_resume(thread_id, model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only)
            else:
                thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=instructions, config={"model_reasoning_effort": self.reasoning_effort})
            payload = self._run_json(thread, prompt, _SUGGEST_SCHEMA)
            return _reply_from_payload(thread.id, payload)

    async def analyze_feedback(self, *, feedback: str, chat_name: str, original_message: str, situation: str, suggested_reply: str, rules: list[str]) -> FeedbackAnalysis:
        return await asyncio.to_thread(self._analyze_feedback_sync, feedback, chat_name, original_message, situation, suggested_reply, rules)

    def _analyze_feedback_sync(self, feedback: str, chat_name: str, original_message: str, situation: str, suggested_reply: str, rules: list[str]) -> FeedbackAnalysis:
        prompt = f"Клиент: {chat_name}\nИсходное сообщение:\n{original_message}\n\nСитуация:\n{situation}\n\nРекомендация:\n{suggested_reply}\n\nДействующие правила:\n" + ("\n".join(f"- {r}" for r in rules) or "(нет)") + f"\n\nЗамечание владельца:\n{feedback}"
        with Codex() as codex:
            thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=_FEEDBACK_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort})
            payload = self._run_json(thread, prompt, _FEEDBACK_SCHEMA)
        return FeedbackAnalysis(
            understanding=str(payload["understanding"]).strip(),
            proposed_rule=str(payload["proposed_rule"]).strip() if payload["proposed_rule"] else None,
            conflict_key=str(payload["conflict_key"]).strip() if payload["conflict_key"] else None,
            scope=str(payload["scope"]), regenerate_current=bool(payload["regenerate_current"]),
            revision_instruction=str(payload["revision_instruction"]).strip() if payload["revision_instruction"] else None,
        )

    async def answer_owner_query(self, *, question: str, chat_name: str, context_pack: str) -> str:
        return await asyncio.to_thread(self._answer_owner_query_sync, question, chat_name, context_pack)

    def _answer_owner_query_sync(self, question: str, chat_name: str, context_pack: str) -> str:
        prompt = f"Чат: {chat_name}\n\n{context_pack}\n\nВопрос команды:\n{question}"
        with Codex() as codex:
            thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=_OWNER_QUERY_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort})
            payload = self._run_json(thread, prompt, _OWNER_QUERY_SCHEMA)
        return str(payload["answer"]).strip()

    def _run_json(self, thread, prompt: str, schema: dict) -> dict:
        result = thread.run(prompt, model=self.model, effort=self.reasoning_effort, output_schema=schema, sandbox=Sandbox.read_only)
        if result.error is not None:
            raise RuntimeError(f"Codex turn failed: {result.error}")
        if not result.final_response:
            raise RuntimeError("Codex returned no final response")
        try:
            return json.loads(result.final_response)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Codex returned an invalid structured response") from exc


def _reply_from_payload(thread_id: str, payload: dict) -> AgentReply:
    action = str(payload.get("action") or "").strip()
    suggested = str(payload.get("suggested_reply") or "").strip()
    should_notify = bool(payload.get("should_notify", action in {AgentAction.REPLY, AgentAction.ASK_OWNER, AgentAction.OBSERVE}))
    confidence = payload.get("confidence")
    return AgentReply(
        thread_id=thread_id,
        situation=str(payload.get("situation") or "").strip(),
        suggested_reply=suggested,
        should_notify=should_notify,
        action=action,
        observation=str(payload.get("observation") or "").strip(),
        unknowns=str(payload.get("unknowns") or "").strip(),
        owner_question=str(payload.get("owner_question") or "").strip(),
        candidate_state=payload.get("candidate_state") if isinstance(payload.get("candidate_state"), dict) else None,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        needs_critique=bool(payload.get("needs_critique")),
    )
