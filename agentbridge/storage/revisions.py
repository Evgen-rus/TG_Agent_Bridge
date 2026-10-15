"""Preserve immutable Telegram message versions in the existing inbox tables."""
import re


def allow_message_edits(db, table):
    assert table in {'telegram_messages', 'events'}
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    if 'edit_update_id' not in {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
        db.execute(f'ALTER TABLE {table} ADD COLUMN edit_update_id INTEGER NOT NULL DEFAULT 0')
    schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    revised, changed = re.subn(r'UNIQUE\s*\(\s*chat_id\s*,\s*message_id\s*\)',
                              'UNIQUE(chat_id, message_id, edit_update_id)', schema)
    if not changed:
        return
    # Rebuild only the message uniqueness constraint; retain IDs, columns and indexes.
    indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL", (table,))]
    sequence = db.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (table,)).fetchone() if table == 'telegram_messages' else None
    temporary = table + '_edits_new'
    db.execute(f'CREATE TABLE {temporary} ' + revised[revised.index('('):])
    db.execute(f'INSERT INTO {temporary} SELECT * FROM {table}')
    db.execute(f'DROP TABLE {table}')
    db.execute(f'ALTER TABLE {temporary} RENAME TO {table}')
    for sql in indexes:
        db.execute(sql)
    if sequence:
        db.execute('UPDATE sqlite_sequence SET seq=max(seq,?) WHERE name=?', (sequence[0], table))
