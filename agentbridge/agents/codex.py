from __future__ import annotations

import asyncio
import json
from pathlib import Path

from openai_codex import Codex, LocalImageInput, MentionInput, RunInput, Sandbox, TextInput

from .base import AgentAction, AgentReply, ChatOnboardingDraft, FeedbackAnalysis, MediaAttachment, OwnerQueryAnswer
from ..media import is_visual_media

_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}
_CANDIDATE_STATE_PROPERTIES = {
    "summary": _STRING,
    "stage": _STRING,
    "facts": _STRING_LIST,
    "decisions": _STRING_LIST,
    "agreements": _STRING_LIST,
    "commitments": _STRING_LIST,
    "waiting_from_client": _STRING_LIST,
    "waiting_from_us": _STRING_LIST,
    "open_questions": _STRING_LIST,
    "risks": _STRING_LIST,
    "unknowns": _STRING_LIST,
    "next_step": _STRING,
    "participants": _STRING_LIST,
}


def _nullable(schema: dict) -> dict:
    # Официальный способ optional-поля: ключ обязателен, значение может быть null.
    return {**schema, "type": [schema["type"], "null"]}


def _schema_types(schema: dict) -> list[str]:
    raw = schema.get("type")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    return [str(item) for item in raw]


def validate_structured_output_schema(schema: dict, *, name: str = "schema") -> None:
    """Codex output_schema идёт в OpenAI Structured Outputs: строгий subset JSON Schema."""
    if not isinstance(schema, dict):
        raise ValueError(f"{name}: schema must be an object")
    if schema.get("anyOf"):
        raise ValueError(f"{name}: root must be type=object, not anyOf")
    types = _schema_types(schema)
    if types and types != ["object"]:
        raise ValueError(f"{name}: root must be type=object")
    _validate_schema_node(schema, name)


def _validate_schema_node(schema: object, path: str) -> None:
    if not isinstance(schema, dict):
        return
    types = _schema_types(schema)
    if "object" in types and len(types) > 1:
        raise ValueError(
            f"{path}: nullable object cannot use type=['object', 'null']; "
            "use a required object with nullable fields, or anyOf with additionalProperties=false"
        )
    is_object = "object" in types or "properties" in schema
    if is_object:
        if schema.get("additionalProperties") is not False:
            raise ValueError(f"{path}: additionalProperties must be false")
        properties = schema.get("properties") or {}
        required = schema.get("required")
        missing = set(properties) - set(required or [])
        extra = set(required or []) - set(properties)
        if not isinstance(required, list) or missing or extra:
            raise ValueError(
                f"{path}: required must list every properties key; missing={sorted(missing)} extra={sorted(extra)}"
            )
        for key, child in properties.items():
            _validate_schema_node(child, f"{path}.properties.{key}")
    if "items" in schema:
        _validate_schema_node(schema["items"], f"{path}.items")
    for index, option in enumerate(schema.get("anyOf") or []):
        _validate_schema_node(option, f"{path}.anyOf[{index}]")


_CANDIDATE_STATE_SCHEMA = {
    "type": "object",
    "properties": {key: _nullable(schema) for key, schema in _CANDIDATE_STATE_PROPERTIES.items()},
    "required": list(_CANDIDATE_STATE_PROPERTIES),
    "additionalProperties": False,
}
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
        "candidate_state": _CANDIDATE_STATE_SCHEMA,
    },
    "required": [
        "action", "situation", "suggested_reply", "observation", "unknowns",
        "owner_question", "should_notify", "confidence", "needs_critique",
        "candidate_state",
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
Редкий фирменный маркер существенной неопределённости — Морти. Если данных достаточно — говори прямо, без Морти. Иногда можно коротко обыграть Морти, только когда неопределённость серьёзная: не хватает важного факта, конфликт фактов или договорённостей, слишком слабая гипотеза, додумывание за клиента, ask_owner из-за критической нехватки информации, заметно ограниченная уверенность. Не в каждом ask_owner, не при мелочи, не ради шутки, не дважды в одном ответе, не когда и так ясно, не если реплика мешает понять смысл. Если Морти использован — сразу скажи, чего не хватает, в чём конфликт или что уточнить. Шутка не заменяет полезную информацию. Рика, Морти и эту метафору никогда не используй в suggested_reply.
Нельзя: сарказм в каждом сообщении, хамство, мат, цинизм, поза «я умнее всех», уверенность без данных, шутки ради шуток, копирование примеров дословно.

Хорошо:
- "Тут я бы не изображал ясновидящего. Не хватает одного факта: кто у них сейчас принимает решение?"
- "Формально можно ответить. Практически - бессмысленно: клиент уже сам закрыл этот вопрос следующим сообщением."
- "Тут есть маленькая проблема: две наши договорённости друг другу противоречат. Я бы сначала разобрался с этим."
Плохо: "Ну да, гениальный план, как всегда"; "Клиент опять несёт чушь"; казённое "Недостаточно информации", если можно назвать, какого факта не хватает."""
_INSTRUCTIONS = """Ты помощник команды по рабочим Telegram-чатам. Режим только suggest: ничего не отправляй клиенту и не выполняй внешние действия.
Источник правды — context pack: wiki чата, общие знания компании если они подключены, текущее состояние, недавняя история, текущий эпизод, подтверждённая память, правила и опыт. Codex thread — только continuity, не память.
Вложения текущего хода (скриншоты, PDF и другие файлы клиента) относятся только к этому чату: прочитай их, если они переданы. Не выдумывай содержимое файла, которого нет во вводе.
Wiki и подтверждённая память этого чата важнее общей методики, если они расходятся.
Различай факты, гипотезы, договорённости и открытые вопросы. Не выдумывай недостающие факты. Если данных мало — признай неопределённость.
В suggested_reply клиенту не раскрывай внутренние кейсы, названия и цифры других клиентов, устройство источников и внутренние гипотезы.
suggested_reply — готовое сообщение в Telegram от нашей команды, сразу для копирования. Подстрой тон под этот чат, его историю и подтверждённую память: коротко и сухо, если так пишут; живее, если общение неформальное. Не копируй ошибки собеседника и не теряй профессиональность. Не переноси характер помощника в клиентский текст.
Пиши как человек в рабочем чате Telegram, не как письмо или статья. Формат сообщения: короткие абзацы, между смысловыми блоками всегда пустая строка - так текст легко читается с телефона. Ориентир - один экран (примерно 15-20 строк). Если материала больше - не ужимай ценность, а подготовь текст так, чтобы его можно было отправить двумя сообщениями: первый блок - главное, второй блок - детали.
Типографика под переписку, не под книгу: длинное тире (—), среднее (–) и кавычки-ёлочки «» не используй. Вместо них обычный дефис "-" или двоеточие, кавычки только прямые '"'. Числовой диапазон пиши как "с 11:00 до 13:00", а не через тире. Эмодзи, Markdown, заголовки и списки не ставь по умолчанию: только если этого требуют стиль чата, память или сам смысл. Без вводных, канцелярита, пересказа клиенту его же сообщения и искусственного резюме в конце.
Если для хода не хватает узкой методики, можно прочитать указанный файл из knowledge/; не читай всю базу целиком.
Используй длинное рассуждение на анализ ситуации, а не на характер. Стиль команды — только финальная формулировка observation/owner_question. Формат ответа команде во внутреннем чате тот же: короткие абзацы с пустой строкой между блоками, без длинных тире и кавычек-ёлочек, по возможности в один экран.

Выбери одно действие:
- reply: клиенту сейчас нужен конкретный ответ. suggested_reply — готовый текст клиенту под стиль этого чата, без характера помощника.
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
Wiki чата важнее общей методики. Чужие клиентские факты и цифры кейсов не подмешивай.
Если не хватает узкой методики, можно прочитать указанный файл из knowledge/.
Длинное рассуждение используй на проверку фактов. Сам ответ команде короткий.
Не предлагай отправлять это клиенту.
Формат под Telegram: короткие абзацы, между смысловыми блоками пустая строка, ориентир один экран. Типографика переписки, не книги: длинное тире (—) и среднее (–) не используй, вместо них дефис "-" или двоеточие; кавычки только прямые '"', не «»; без Markdown, заголовков и списков, если тебя прямо не просят.
""" + _OWNER_VOICE
_ONBOARDING_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "wiki": {"type": "string"},
        "directory_slug": {"type": "string"},
    },
    "required": ["name", "wiki", "directory_slug"],
    "additionalProperties": False,
}
for _schema_name, _schema in (
    ("suggest", _SUGGEST_SCHEMA),
    ("feedback", _FEEDBACK_SCHEMA),
    ("owner_query", _OWNER_QUERY_SCHEMA),
    ("onboarding", _ONBOARDING_SCHEMA),
):
    validate_structured_output_schema(_schema, name=_schema_name)
_ONBOARDING_INSTRUCTIONS = """Ты готовишь карточку нового клиентского Telegram-чата для команды.
Имя чата обязательно возьми из текста владельца, а не из названия группы, если владелец назвал клиента.
wiki.md — стабильный контекст на русском: кто клиент, участники если названы, чем занимаемся, что обычно обсуждается.
Пиши только то, что есть во вводе. Не выдумывай факты, цифры, договорённости и роли.
directory_slug — латиница, строчные, слова через подчёркивание, без пути и пробелов.
Не предлагай писать клиенту."""
_CRITIQUE_INSTRUCTIONS = """Проверь предыдущий JSON-ответ как нейтральный аналитик. Исправь выдуманные факты, устаревшие рекомендации и слабые гипотезы, выданные как факты. Если более поздние сообщения закрыли вопрос — не предлагай reply на него. Не усиливай уверенность и не меняй action ради более острого тона. Голос команды может остаться в observation/owner_question, но смысл должен стать точнее. Верни тот же JSON schema."""

AGENT_PROMPT_VERSION = 7


class CodexProvider:
    prompt_version = AGENT_PROMPT_VERSION

    def __init__(self, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "xhigh", cwd: Path | None = None):
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.cwd = str((cwd or Path.cwd()).resolve())
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"Unsupported Codex reasoning effort: {reasoning_effort}")

    async def suggest(self, *, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, context_pack: str = "", attachments: tuple[MediaAttachment, ...] | list[MediaAttachment] = ()) -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, None, context_pack, tuple(attachments))

    async def revise(self, *, feedback: str, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str, context_pack: str = "", attachments: tuple[MediaAttachment, ...] | list[MediaAttachment] = ()) -> AgentReply:
        return await asyncio.to_thread(self._suggest_sync, message, sender_name, chat_name, wiki, rules, thread_id, feedback, context_pack, tuple(attachments))

    async def critique(self, *, previous: AgentReply, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, context_pack: str = "", attachments: tuple[MediaAttachment, ...] | list[MediaAttachment] = ()) -> AgentReply:
        return await asyncio.to_thread(self._critique_sync, previous, message, sender_name, chat_name, wiki, rules, context_pack, tuple(attachments))

    def _suggest_sync(self, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], thread_id: str | None, revision: str | None, context_pack: str = "", attachments: tuple[MediaAttachment, ...] = ()) -> AgentReply:
        pack = self._pack(wiki, rules, context_pack)
        prompt = f"Чат: {chat_name}\nОтправитель: {sender_name}\n\n{pack}\n\nСообщение:\n{message}"
        if revision:
            prompt += f"\n\nПодтвержденное замечание владельца. Пересоздай текущую рекомендацию:\n{revision}"
        with Codex() as codex:
            if thread_id:
                thread = codex.thread_resume(thread_id, model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only)
            else:
                thread = codex.thread_start(
                    model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only,
                    developer_instructions=_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort},
                )
            payload = self._run_json(thread, _turn_input(prompt, attachments), _SUGGEST_SCHEMA)
            return _reply_from_payload(thread.id, payload)

    def _critique_sync(
        self, previous: AgentReply, message: str, sender_name: str, chat_name: str, wiki: str, rules: list[str], context_pack: str,
        attachments: tuple[MediaAttachment, ...] = (),
    ) -> AgentReply:
        pack = self._pack(wiki, rules, context_pack)
        previous_json = json.dumps(
            {
                "action": previous.resolved_action(),
                "situation": previous.situation,
                "suggested_reply": previous.suggested_reply,
                "observation": previous.observation,
                "unknowns": previous.unknowns,
                "owner_question": previous.owner_question,
                "should_notify": previous.should_notify,
                "confidence": previous.confidence,
                "needs_critique": previous.needs_critique,
                "candidate_state": previous.candidate_state,
            },
            ensure_ascii=False,
        )
        prompt = (
            f"Чат: {chat_name}\nОтправитель: {sender_name}\n\n{pack}\n\n"
            f"Текущий эпизод:\n{message}\n\nПредыдущий JSON:\n{previous_json}"
        )
        with Codex() as codex:
            thread = codex.thread_start(
                model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only,
                developer_instructions=_CRITIQUE_INSTRUCTIONS,
                config={"model_reasoning_effort": self.reasoning_effort},
            )
            payload = self._run_json(thread, _turn_input(prompt, attachments), _SUGGEST_SCHEMA)
        return _reply_from_payload(previous.thread_id, payload)

    @staticmethod
    def _pack(wiki: str, rules: list[str], context_pack: str) -> str:
        rules_text = "\n".join(f"- {rule}" for rule in rules) or "(нет)"
        return context_pack.strip() or f"Wiki чата:\n{wiki or '(wiki пуста)'}\n\nПодтвержденные правила:\n{rules_text}"

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

    async def answer_owner_query(
        self, *, question: str, chat_name: str, context_pack: str, thread_id: str | None,
    ) -> OwnerQueryAnswer:
        return await asyncio.to_thread(self._answer_owner_query_sync, question, chat_name, context_pack, thread_id)

    def _answer_owner_query_sync(
        self, question: str, chat_name: str, context_pack: str, thread_id: str | None,
    ) -> OwnerQueryAnswer:
        prompt = f"Чат: {chat_name}\n\n{context_pack}\n\nВопрос команды:\n{question}"
        with Codex() as codex:
            if thread_id:
                try:
                    thread = codex.thread_resume(thread_id, model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only)
                    payload = self._run_json(thread, prompt, _OWNER_QUERY_SCHEMA)
                except Exception:
                    thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=_OWNER_QUERY_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort})
                    payload = self._run_json(thread, prompt, _OWNER_QUERY_SCHEMA)
            else:
                thread = codex.thread_start(model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only, developer_instructions=_OWNER_QUERY_INSTRUCTIONS, config={"model_reasoning_effort": self.reasoning_effort})
                payload = self._run_json(thread, prompt, _OWNER_QUERY_SCHEMA)
        return OwnerQueryAnswer(thread.id, str(payload["answer"]).strip())

    async def draft_chat_onboarding(self, *, group_title: str, owner_brief: str, telegram_chat_id: int) -> ChatOnboardingDraft:
        return await asyncio.to_thread(self._draft_chat_onboarding_sync, group_title, owner_brief, telegram_chat_id)

    def _draft_chat_onboarding_sync(self, group_title: str, owner_brief: str, telegram_chat_id: int) -> ChatOnboardingDraft:
        prompt = (
            f"Название группы в Telegram: {group_title or '(нет)'}\n"
            f"ID чата: {telegram_chat_id}\n\n"
            f"Пояснение владельца, кто это за клиент:\n{owner_brief}"
        )
        with Codex() as codex:
            thread = codex.thread_start(
                model=self.model, cwd=self.cwd, sandbox=Sandbox.read_only,
                developer_instructions=_ONBOARDING_INSTRUCTIONS,
                config={"model_reasoning_effort": self.reasoning_effort},
            )
            payload = self._run_json(thread, prompt, _ONBOARDING_SCHEMA)
        return ChatOnboardingDraft(
            name=str(payload.get("name") or "").strip() or owner_brief.strip().splitlines()[0][:80],
            wiki=str(payload.get("wiki") or "").strip() or owner_brief.strip(),
            directory_slug=str(payload.get("directory_slug") or "").strip(),
        )

    def _run_json(self, thread, prompt: RunInput, schema: dict) -> dict:
        result = thread.run(prompt, model=self.model, effort=self.reasoning_effort, output_schema=schema, sandbox=Sandbox.read_only)
        if result.error is not None:
            raise RuntimeError(f"Codex turn failed: {result.error}")
        if not result.final_response:
            raise RuntimeError("Codex returned no final response")
        try:
            return json.loads(result.final_response)
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("Codex returned an invalid structured response") from exc


def _turn_input(prompt: str, attachments: tuple[MediaAttachment, ...] = ()) -> RunInput:
    if not attachments:
        return prompt
    items: list = [TextInput(prompt)]
    for item in attachments:
        path = str(Path(item.path).resolve())
        if is_visual_media(item.kind, item.mime, item.filename):
            items.append(LocalImageInput(path=path))
        else:
            items.append(MentionInput(name=item.filename or Path(path).name, path=path))
    return items


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
