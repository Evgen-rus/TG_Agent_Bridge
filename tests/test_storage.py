from __future__ import annotations

import sqlite3

from agentbridge.storage.sqlite import ChatThreadStore


def test_thread_mapping_survives_a_new_store_instance(tmp_path) -> None:
    database_path = tmp_path / "runtime" / "agentbridge.sqlite3"
    first_store = ChatThreadStore(database_path)
    first_store.save_thread(
        telegram_chat_id=-100123456,
        logical_name="Acme Support",
        codex_thread_id="thread-abc",
        agent_provider="codex",
    )

    restarted_store = ChatThreadStore(database_path)

    assert restarted_store.get_thread_id(-100123456) == "thread-abc"
    assert restarted_store.get_thread_prompt_version(-100123456) is None
    assert restarted_store.get_thread_id(-100999999) is None


def test_prompt_version_is_stored_and_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "runtime" / "agentbridge.sqlite3"
    first_store = ChatThreadStore(database_path)
    first_store.save_thread(-100123456, "Acme Support", "thread-old", prompt_version=None)
    first_store.save_thread(-100123456, "Acme Support", "thread-new", prompt_version=2)
    restarted = ChatThreadStore(database_path)
    assert restarted.get_thread_id(-100123456) == "thread-new"
    assert restarted.get_thread_prompt_version(-100123456) == 2


def test_owner_query_thread_is_stored_separately_and_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "agentbridge.sqlite3"
    store = ChatThreadStore(database_path)
    store.save_thread(-100123456, "Acme Support", "client-thread", prompt_version=3)
    store.save_owner_query_thread(-100123456, "Acme Support", "owner-thread", prompt_version=3)

    restarted = ChatThreadStore(database_path)

    assert restarted.get_thread_id(-100123456) == "client-thread"
    assert restarted.get_owner_query_thread_id(-100123456) == "owner-thread"
    assert restarted.get_owner_query_thread_prompt_version(-100123456) == 3


def test_processed_update_survives_a_new_store_instance(tmp_path) -> None:
    database_path = tmp_path / "runtime" / "agentbridge.sqlite3"
    first_store = ChatThreadStore(database_path)
    assert first_store.is_update_processed(456) is False

    first_store.mark_update_processed(456)
    restarted_store = ChatThreadStore(database_path)

    assert restarted_store.is_update_processed(456) is True


def test_pending_recommendation_survives_restart_until_owner_delivery(tmp_path) -> None:
    database_path = tmp_path / "runtime" / "agentbridge.sqlite3"
    first_store = ChatThreadStore(database_path)
    recommendation_id = first_store.create_recommendation(
        telegram_chat_id=-100123456,
        chat_name="Acme Support",
        sender_name="Alice",
        original_message="Hello",
        situation="Greeting",
        suggested_reply="Hi",
        owner_chat_id=7654321,
    )

    restarted_store = ChatThreadStore(database_path)

    pending = restarted_store.pending_recommendations(7654321)
    assert [record.id for record in pending] == [recommendation_id]
    assert pending[0].owner_message_id is None

    restarted_store.attach_owner_message(recommendation_id, 7654321, 9001)

    assert restarted_store.pending_recommendations(7654321) == []


def test_delivery_parts_survive_restart_and_resolve_only_within_owner_chat(tmp_path) -> None:
    path = tmp_path / "parts.sqlite3"
    store = ChatThreadStore(path)
    assert store.prepare_owner_delivery_parts(42, "recommendation:88", ["первая", "вторая"]) == [
        ("первая", None), ("вторая", None),
    ]
    store.record_owner_delivery_part(42, "recommendation:88", 0, 101)
    restarted = ChatThreadStore(path)
    # Retries must use the frozen text even if formatting code has changed.
    assert restarted.prepare_owner_delivery_parts(42, "recommendation:88", ["changed"]) == [
        ("первая", 101), ("вторая", None),
    ]
    assert restarted.resolve_owner_reply(42, 101) == 101
    restarted.record_owner_delivery_part(42, "recommendation:88", 1, 102)
    assert restarted.resolve_owner_reply(42, 101) == 102
    assert restarted.resolve_owner_reply(42, 102) == 102
    assert restarted.resolve_owner_reply(43, 101) == 101
    assert restarted.resolve_owner_reply(42, 999) == 999


def test_chat_onboarding_survives_restart(tmp_path) -> None:
    database_path = tmp_path / "runtime" / "agentbridge.sqlite3"
    first = ChatThreadStore(database_path)
    created = first.ensure_onboarding(-100999, "Новая группа", "Евгений Расюк", 42)
    first.attach_onboarding_notice(created.id, 501)
    first.ingest_telegram_message(
        update_id=11, chat_id=-100999, message_id=101, sender_id=7,
        sender_name="Alice", telegram_date="", text="Hello",
        reply_to_message_id=None, role="client", processing_status="held",
    )

    restarted = ChatThreadStore(database_path)
    record = restarted.get_onboarding(-100999)
    assert record is not None
    assert record.status == "pending_brief"
    assert record.owner_notice_message_id == 501
    assert restarted.has_open_onboarding(-100999) is True
    restarted.save_onboarding_draft(record.id, owner_brief="Это Балтлиз", draft_name="Baltlease", draft_wiki="wiki", draft_directory="baltlease")
    assert restarted.confirm_onboarding(record.id) is not None
    restarted.release_held_messages(-100999)
    pending = restarted.pending_messages(-100999)
    assert len(pending) == 1
    assert pending[0].text == "Hello"


def test_confirmed_project_and_global_memory_are_scoped(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "runtime" / "agentbridge.sqlite3")
    first = store.create_recommendation(-1001, "First", "Alice", "Hello", "Greeting", "Hi")
    second = store.create_recommendation(-1002, "Second", "Bob", "Hello", "Greeting", "Hi")
    project = store.create_memory_draft(first, 1, "Owner", "Project fact", "project", "pilot")
    global_memory = store.create_memory_draft(first, 1, "Owner", "Global fact", "global", None)
    local = store.create_memory_draft(first, 1, "Owner", "Local fact", "chat", None)
    assert store.confirm_memory_draft(project.id) is not None
    assert store.confirm_memory_draft(global_memory.id) is not None
    assert store.confirm_memory_draft(local.id) is not None

    assert store.active_memory_texts(-1001, "pilot") == ["Project fact", "Global fact", "Local fact"]
    assert store.active_memory_texts(-1002, "pilot") == ["Project fact", "Global fact"]
    assert store.active_memory_texts(-1002, None) == ["Global fact"]


def test_unlinked_global_memory_draft_can_be_confirmed(tmp_path) -> None:
    store = ChatThreadStore(tmp_path / "runtime" / "agentbridge.sqlite3")
    draft = store.create_memory_draft(None, 1, "Owner", "Утверждённая фраза для робота", "global", None)
    assert draft.recommendation_id is None
    assert store.confirm_memory_draft(draft.id) is not None
    assert store.active_memory_texts(-1001, None) == ["Утверждённая фраза для робота"]
    assert store.active_memory_texts(-1002, "pilot") == ["Утверждённая фраза для робота"]


def test_legacy_memory_drafts_accept_unlinked_global(tmp_path) -> None:
    path = tmp_path / "runtime" / "agentbridge.sqlite3"
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE memory_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recommendation_id INTEGER NOT NULL,
            author_user_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            content TEXT NOT NULL,
            scope TEXT NOT NULL CHECK(scope IN ('chat', 'project', 'global')),
            project_key TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    connection.commit()
    connection.close()
    store = ChatThreadStore(path)
    draft = store.create_memory_draft(None, 1, "Owner", "Global from legacy db", "global", None)
    assert store.confirm_memory_draft(draft.id) is not None
    assert store.active_memory_texts(-1001, None) == ["Global from legacy db"]
