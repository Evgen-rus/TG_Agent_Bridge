from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai_codex import Codex, Sandbox

from .base import AgentReply


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "situation": {"type": "string"},
        "suggested_reply": {"type": "string"},
    },
    "required": ["situation", "suggested_reply"],
    "additionalProperties": False,
}

_INSTRUCTIONS = """Ты помогаешь владельцу ответить на новое сообщение в Telegram.
Работай только в режиме suggest: не выполняй внешние действия и не отправляй сообщения.
Опирайся на новое сообщение, wiki этого чата и доступный контекст текущего thread.
Не придумывай факты. При нехватке данных прямо укажи это.
Учитывай стиль предыдущего общения. Ответ должен быть коротким и готовым к копированию.
Верни JSON с полями situation и suggested_reply по заданной схеме."""


class CodexProvider:
    def __init__(
        self,
        *,
        model: str = "gpt-5.6-sol",
        reasoning_effort: str = "medium",
        cwd: Path | None = None,
    ):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cwd = str((cwd or Path.cwd()).resolve())
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Codex reasoning effort: {reasoning_effort}")

    async def suggest(
        self,
        *,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        thread_id: str | None,
    ) -> AgentReply:
        return await asyncio.to_thread(
            self._suggest_sync,
            message,
            sender_name,
            chat_name,
            wiki,
            thread_id,
        )

    def _suggest_sync(
        self,
        message: str,
        sender_name: str,
        chat_name: str,
        wiki: str,
        thread_id: str | None,
    ) -> AgentReply:
        prompt = (
            f"Чат: {chat_name}\n"
            f"Отправитель: {sender_name}\n\n"
            f"Wiki чата:\n{wiki or '(wiki пуста)'}\n\n"
            f"Новое сообщение:\n{message}"
        )
        with Codex() as codex:
            if thread_id:
                thread = codex.thread_resume(
                    thread_id,
                    model=self.model,
                    cwd=self.cwd,
                    sandbox=Sandbox.read_only,
                )
            else:
                thread = codex.thread_start(
                    model=self.model,
                    cwd=self.cwd,
                    sandbox=Sandbox.read_only,
                    developer_instructions=_INSTRUCTIONS,
                    config={"model_reasoning_effort": self.reasoning_effort},
                )
            result = thread.run(
                prompt,
                model=self.model,
                effort=self.reasoning_effort,
                output_schema=_OUTPUT_SCHEMA,
                sandbox=Sandbox.read_only,
            )
            if result.error is not None:
                raise RuntimeError(f"Codex turn failed: {result.error}")
            if not result.final_response:
                raise RuntimeError("Codex returned no final response")
            try:
                payload = json.loads(result.final_response)
                situation = str(payload["situation"]).strip()
                suggested_reply = str(payload["suggested_reply"]).strip()
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError("Codex returned an invalid structured response") from exc
            return AgentReply(
                thread_id=thread.id,
                situation=situation,
                suggested_reply=suggested_reply,
            )
