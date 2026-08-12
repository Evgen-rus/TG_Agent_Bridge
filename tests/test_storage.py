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
