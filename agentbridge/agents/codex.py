from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai_codex import Codex, Sandbox

from .base import AgentReply, FeedbackAnalysis

_SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {"type": "string"},
        "suggested_reply": {"type": "string"},
        "should_notify": {"type": "boolean"},
    },
    "required": ["situation", "suggested_reply", "should_notify"], "additionalProperties": False,
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
_INSTRUCTIONS = """Ты помогаешь владельцу ответить на новое сообщение в Telegram.
Работай только в режиме suggest: не выполняй внешние действия и не отправляй сообщения.
Опирайся на новое сообщение, wiki, подтвержденные правила и контекст текущего thread.
Не придумывай факты. При нехватке данных прямо укажи это.
Если подтвержденные правила говорят не создавать рекомендацию в этой ситуации,
верни should_notify=false и пустой suggested_reply. Иначе should_notify=true,
а ответ должен быть коротким, естественным и готовым к копированию.
Верни JSON по заданной схеме."""
_FEEDBACK_INSTRUCTIONS = """Ты разбираешь замечание владельца к рекомендации помощника.
Сформулируй простыми словами, как понял замечание. Сам определи, является ли оно:
разовым исправлением текущего ответа, постоянным правилом или одновременно обоими.
Не превращай разовую фактическую правку в постоянное правило без оснований.
scope=global допустим только при явном указании владельца применять для всех клиентов.
Для постоянного правила дай короткий proposed_rule и стабильный смысловой conflict_key,
чтобы новое правило той же темы могло заменить старое. Иначе оба поля null.
regenerate_current=true, если замечание требует исправить текущую рекомендацию.
revision_instruction описывает только необходимое исправление текущего ответа или null.
Не применяй замечание: только интерпретируй для подтверждения человеком."""


class CodexProvider:
    def __init__(self, *, model: str = "gpt-5.6-sol", reasoning_effort: str = "medium", cwd: Path | None = None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cwd = str((cwd or Path.cwd()).resolve())
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Codex reasoning effort: {reasoning_effort}")

    async def suggest(self, *, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None) -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, None)

    async def revise(self, *, feedback: str, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str) -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, feedback)

    def _suggest_sync(self, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, revision: str | None) -> AgentReply:
        rules_text = "\n".join(f"- {rule}" for rule in rules) or "(нет)"
        prompt = f"Чат: {chat_name}\nОтправитель: {sender_name}\n\nWiki чата:\n{wiki or '(wiki пуста)'}\n\nПодтвержденные правила:\n{rules_text}\n\nСообщение:\n{message}"
        if revision:
            prompt += f"\n\nПодтвержденное замечание владельца. Пересоздай текущую рекомендацию:\n{revision}"
        with Codex() as codex:
            if thread_id:
                thread = codex.thread_resume(thread_id, model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only)
            else:
                thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort})
            payload = self._run_json(thread, prompt, _SUGGEST_SCHEMA)
            return AgentReply(thread.id, str(payload["situation"]).strip(), str(payload["suggested_reply"]).strip(), bool(payload["should_notify"]))

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
