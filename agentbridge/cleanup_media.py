"""Daily original-attachment cleanup. Default is preview; --apply removes bytes."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from .retention import RETENTION_SECONDS, media_lock, orphan_files, remove_file, safe_path


@contextmanager
def database(path):
    # Never create an empty database or run application startup migrations here.
    db = sqlite3.connect(path.resolve().as_uri()+'?mode=rw', uri=True, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            yield db
    finally:
        db.close()


def cleanup_telegram(db_path, roots, product, *, dry_run=True):
    result = dict(files=0, bytes=0, errors=0)
    if not db_path.is_file():
        return result
    cutoff = time.time()-RETENTION_SECONDS
    with media_lock(db_path.parent,blocking=False), database(db_path) as db:
        known, candidates = set(), []
        if product=='rick':
            rows = db.execute("SELECT * FROM telegram_messages WHERE media_path!=''").fetchall()
            retained = {str(Path(r[0]).absolute()) for r in db.execute(
                "SELECT media_path FROM owner_query_deliveries WHERE owner_message_id IS NULL AND media_path!=''")}
            known.update(retained)
            busy = {r[0] for r in db.execute("SELECT DISTINCT chat_id FROM telegram_messages WHERE processing_status NOT IN ('processed','ignored')")}
            for row in rows:
                path = safe_path(row['media_path'],roots[0])
                if not path or 'owner_generated' in path.relative_to(roots[0]).parts:
                    continue
                known.add(str(path.absolute()))
                created = datetime.fromisoformat(row['created_at'].replace('Z','+00:00')).timestamp()
                if created<cutoff and row['chat_id'] not in busy and str(path.absolute()) not in retained:
                    candidates.append((path,row['id'],'telegram_messages'))
        else:
            busy = bool(db.execute("SELECT 1 FROM events WHERE processed IN (0,2) LIMIT 1").fetchone())
            for table, root, terminal in [('audio_jobs',roots[0],('transcribed','failed')),('image_jobs',roots[1],('read','failed'))]:
                rows = db.execute(f'SELECT j.*,e.processed,e.date,e.chat_id,e.topic_id FROM {table} j JOIN events e ON e.id=j.event_id').fetchall()
                busy = busy or any(r['state'] not in terminal for r in rows)
                for row in rows:
                    path = safe_path(row['path'],root) if row['path'] else None
                    if path:
                        known.add(str(path.absolute()))
                        created = datetime.fromisoformat(row['date'].replace('Z','+00:00')).timestamp()
                        sending = db.execute('SELECT 1 FROM outbox WHERE chat_id=? AND topic_id=? AND telegram_message_id IS NULL LIMIT 1',
                                             (row['chat_id'],row['topic_id'])).fetchone()
                        if created<cutoff and row['state'] in terminal and row['processed'] in (1,3) and not sending:
                            candidates.append((path,row['event_id'],table))
        if not dry_run:
            # Keep all texts/evidence; persist expiry before touching the filesystem.
            for path, ident, table in candidates:
                if table=='telegram_messages':
                    db.execute("UPDATE telegram_messages SET download_status='expired' WHERE id=?",(ident,))
            db.commit()
            db.execute('BEGIN IMMEDIATE')
        for path, ident, table in candidates:
            values = remove_file(path,dry_run=dry_run)
            result = {k:result[k]+v for k,v in zip(result,values)}
            if not dry_run and not values[2]:
                column, key = ('media_path','id') if table=='telegram_messages' else ('path','event_id')
                db.execute(f"UPDATE {table} SET {column}='' WHERE {key}=?",(ident,))
        # Unknown files are only eligible when no unfinished source could own them.
        if not busy:
            for root in roots:
                for path in orphan_files(root,known,telegram=True):
                    values = remove_file(path,dry_run=dry_run)
                    result = {k:result[k]+v for k,v in zip(result,values)}
    return result


def cleanup(root, *, dry_run=True):
    from .web.media import cleanup as cleanup_web
    from .web.store import WebStore
    reports = {}
    with media_lock(root/'runtime/attachment-cleanup',blocking=False):
        for product, db_path, roots in [
            ('rick',root/'runtime/agentbridge.sqlite3',[root/'runtime/media']),
            ('sally',root/'runtime/sales_buddy/sales.sqlite3',[root/'runtime/sales_buddy/audio',root/'runtime/sales_buddy/images']),
        ]:
            try:
                reports[product] = cleanup_telegram(db_path,roots,product,dry_run=dry_run)
            except BlockingIOError:
                reports[product] = dict(skipped='active_reader')
        path = root/'runtime/web/access.sqlite3'
        if path.is_file():
            store = WebStore.__new__(WebStore)
            store.path = path
            reports['web'] = cleanup_web(store,dry_run=dry_run)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        result = cleanup(root,dry_run=not args.apply)
    except BlockingIOError:
        result = dict(skipped='already_running')
    print(json.dumps(dict(dry_run=not args.apply,**result),sort_keys=True))


if __name__=='__main__':
    main()
