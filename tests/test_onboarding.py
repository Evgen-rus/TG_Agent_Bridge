from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from agentbridge.agents.base import AgentReply, ChatOnboardingDraft
from agentbridge.application import AgentBridgeApplication, OnboardingDraftProposal, OnboardingNotice
from agentbridge.storage.sqlite import ChatThreadStore
from agentbridge.telegram.bot import create_telegram_application


@dataclass
class FakeProvider:
    calls: list[dict] = field(default_factory=list)
    reply: AgentReply = field(default_factory=lambda: AgentReply("thread-1", "Situation", "Reply"))

    async def suggest(self, **kwargs) -> AgentReply:
        self.calls.append(kwargs)
        return self.reply

    async def draft_chat_onboarding(self, *, group_title: str, owner_brief: str, telegram_chat_id: int) -> ChatOnboardingDraft:
        self.calls.append({"group_title": group_title, "owner_brief": owner_brief, "telegram_chat_id": telegram_chat_id})
        return ChatOnboardingDraft("Baltlease", "# Wiki\n\nКлиент Baltlease.\n", "baltlease")


@pytest.mark.asyncio
async def test_unknown_admin_chat_holds_messages_until_wiki_is_confirmed(tmp_path, chat_registry) -> None:
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, 7654321, chats_dir=chats_dir)

    first = service.begin_unconfigured_chat(-100999, "LR128 group", "Евгений Расюк", 42)
    assert first is not None and first.needs_delivery is True
    service.record_onboarding_notice(first.onboarding_id, 501)
    second = service.begin_unconfigured_chat(-100999, "LR128 group", "Евгений Расюк", 42)
    assert second is not None and second.needs_delivery is False
    assert provider.calls == []

    ingested = service.ingest_telegram_message(
        update_id=11, chat_id=-100999, message_id=101, sender_id=7,
        sender_name="Alice", telegram_date="", text="Need a quote",
        reply_to_message_id=None, is_owner_chat=False,
    )
    assert ingested is True
    assert store.pending_messages(-100999) == []
    assert await service.handle_message(-100999, "Alice", "Need a quote", update_id=12) is None
    assert provider.calls == []

    draft = await service.handle_onboarding_brief(7654321, 501, "Евгений Расюк", "Это клиент Baltlease, лизинг.")
    assert draft is not None
    assert draft.name == "Baltlease"
    assert "Baltlease" in draft.wiki
    assert len(provider.calls) == 1

    chat = service.confirm_onboarding(draft.onboarding_id)
    assert chat is not None
    assert service.is_monitored_chat(-100999) is True
    assert (chat.directory / "wiki.md").read_text(encoding="utf-8").find("Baltlease") != -1
    results = await service.catch_up()
    assert len(results) == 1
    assert results[0].original_message == "Need a quote"
    assert any(call.get("message") == "Need a quote" for call in provider.calls)


@pytest.mark.asyncio
async def test_duplicate_onboarding_brief_is_ignored(tmp_path, chat_registry) -> None:
    store = ChatThreadStore(tmp_path / "agentbridge.sqlite3")
    provider = FakeProvider()
    service = AgentBridgeApplication(chat_registry, store, provider, 7654321, chats_dir=tmp_path / "chats")
    notice = service.begin_unconfigured_chat(-100888, "Quiet group")
    assert notice is not None
    service.record_onboarding_notice(notice.onboarding_id, 601)
    first = await service.handle_onboarding_brief(7654321, 601, "Owner", "Клиент Татьяна", update_id=88)
    second = await service.handle_onboarding_brief(7654321, 601, "Owner", "Клиент Татьяна", update_id=88)
    assert first is not None
    assert second is None
    assert len([call for call in provider.calls if "owner_brief" in call]) == 1


@dataclass
class FakeSentMessage:
    message_id: int


@dataclass
class FakeBot:
    sent: list[dict] = field(default_factory=list)
    id: int = 777
    admin_status: str = "administrator"

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSentMessage(1000 + len(self.sent))

    async def get_chat_member(self, chat_id, user_id):
        return type("Member", (), {"status": self.admin_status})()


@dataclass
class FakeContext:
    bot: FakeBot


@dataclass
class FakeChat:
    id: int
    title: str = "Unknown group"


@dataclass
class FakeUser:
    id: int = 42
    full_name: str = "Евгений Расюк"
    is_bot: bool = False


@dataclass
class FakeMessage:
    text: str
    reply_to_message: object | None = None
    message_id: int = 50
    chat: FakeChat | None = None


@dataclass
class FakeReply:
    message_id: int
    from_user: FakeUser | None = None


@dataclass
class FakeChatMember:
    status: str
    user: FakeUser | None = None


@dataclass
class FakeChatMemberUpdated:
    chat: FakeChat
    from_user: FakeUser
    new_chat_member: FakeChatMember
    old_chat_member: FakeChatMember | None = None


@dataclass
class FakeUpdate:
    effective_message: FakeMessage | None = None
    effective_chat: FakeChat | None = None
    effective_user: FakeUser | None = None
    update_id: int = 123
    my_chat_member: FakeChatMemberUpdated | None = None


@dataclass
class FakeCallbackQuery:
    data: str
    message: FakeMessage
    chat: FakeChat

    async def answer(self) -> None:
        return None

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        return None


@dataclass
class FakeCallbackUpdate:
    callback_query: FakeCallbackQuery
    effective_message: FakeMessage | None = None
    effective_chat: FakeChat | None = None
    effective_user: FakeUser | None = None
    update_id: int = 900


@dataclass
class RecordingOnboardingService:
    monitored: set[int] = field(default_factory=set)
    notices: list[OnboardingNotice] = field(default_factory=list)
    briefs: list[str] = field(default_factory=list)
    confirmed: list[int] = field(default_factory=list)
    ingested: list[int] = field(default_factory=list)
    catchups: list[int] = field(default_factory=list)
    processed: set[int] = field(default_factory=set)
    notice_by_id: dict[int, OnboardingNotice] = field(default_factory=dict)
    next_id: int = 1

    def is_monitored_chat(self, telegram_chat_id: int) -> bool:
        return telegram_chat_id in self.monitored

    def is_update_processed(self, update_id: int) -> bool:
        return update_id in self.processed

    def mark_update_processed(self, update_id: int) -> None:
        self.processed.add(update_id)

    def ingest_telegram_message(self, **kwargs) -> bool:
        self.ingested.append(kwargs["update_id"])
        return True

    def begin_unconfigured_chat(self, telegram_chat_id, chat_title, added_by_name="", added_by_id=None, update_id=None):
        if update_id is not None:
            self.processed.add(update_id)
        existing = next((item for item in self.notices if item.telegram_chat_id == telegram_chat_id), None)
        if existing is not None:
            return OnboardingNotice(existing.onboarding_id, telegram_chat_id, chat_title, added_by_name, False)
        notice = OnboardingNotice(self.next_id, telegram_chat_id, chat_title, added_by_name, True)
        self.next_id += 1
        self.notices.append(notice)
        self.notice_by_id[notice.onboarding_id] = notice
        return notice

    def record_onboarding_notice(self, onboarding_id: int, owner_message_id: int) -> None:
        return None

    def record_onboarding_draft_message(self, onboarding_id: int, owner_message_id: int) -> None:
        return None

    async def handle_onboarding_brief(self, owner_chat_id, reply_to_message_id, author_name, brief, update_id=None):
        self.briefs.append(brief)
        return OnboardingDraftProposal(1, -100999, "Unknown group", "Baltlease", "wiki")

    def confirm_onboarding(self, onboarding_id: int):
        self.confirmed.append(onboarding_id)
        chat = type("Chat", (), {"name": "Baltlease", "telegram_chat_id": -100999})()
        self.monitored.add(-100999)
        return chat

    async def process_pending_chat(self, telegram_chat_id, *, mode="live"):
        self.catchups.append(telegram_chat_id)
        assert mode == "catchup"
        return None

    async def handle_messages(self, telegram_chat_id, messages):
        raise AssertionError("Unknown chats must not enter the live client pipeline")


def _message_callback(application):
    return application.handlers[0][0].callback


def _callback_query_callback(application):
    return application.handlers[0][1].callback


def _member_callback(application):
    return application.handlers[0][2].callback


@pytest.mark.asyncio
async def test_already_admin_unknown_group_notifies_owner_only() -> None:
    service = RecordingOnboardingService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(-100999, "Baltlease group"), FakeUser(), 301),
        FakeContext(bot),
    )
    assert service.ingested == [301]
    assert [item["chat_id"] for item in bot.sent] == [7654321]
    assert "без wiki" in bot.sent[0]["text"]
    assert all(item["chat_id"] != -100999 for item in bot.sent)


@pytest.mark.asyncio
async def test_unknown_non_admin_group_is_ignored() -> None:
    service = RecordingOnboardingService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot(admin_status="member")
    await _message_callback(application)(
        FakeUpdate(FakeMessage("Hello"), FakeChat(-100404, "Random"), FakeUser(), 302),
        FakeContext(bot),
    )
    assert service.ingested == []
    assert service.notices == []
    assert bot.sent == []


@pytest.mark.asyncio
async def test_my_chat_member_admin_notifies_owner() -> None:
    service = RecordingOnboardingService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    chat = FakeChat(-100777, "Already there")
    event = FakeChatMemberUpdated(
        chat=chat,
        from_user=FakeUser(),
        new_chat_member=FakeChatMember("administrator"),
        old_chat_member=FakeChatMember("member"),
    )
    await _member_callback(application)(
        FakeUpdate(effective_chat=chat, effective_user=FakeUser(), update_id=401, my_chat_member=event),
        FakeContext(bot),
    )
    assert [item["chat_id"] for item in bot.sent] == [7654321]
    assert 401 in service.processed


@pytest.mark.asyncio
async def test_onboarding_confirm_runs_catchup_not_live() -> None:
    service = RecordingOnboardingService()
    application = create_telegram_application(token="test-token", owner_chat_id=7654321, message_service=service, batch_seconds=0)
    bot = FakeBot()
    query = FakeCallbackQuery("onboard:yes:1", FakeMessage("draft", chat=FakeChat(7654321)), FakeChat(7654321))
    update = FakeCallbackUpdate(query, effective_chat=FakeChat(7654321))
    await _callback_query_callback(application)(update, FakeContext(bot))
    assert service.confirmed == [1]
    assert service.catchups == [-100999]
    assert all(item["chat_id"] == 7654321 for item in bot.sent)
