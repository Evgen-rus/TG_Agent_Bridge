from __future__ import annotations

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
    assert restarted_store.get_thread_id(-100999999) is None


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
