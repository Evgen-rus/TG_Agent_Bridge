"""Private binary transport; analysis stays in the existing agent workers."""
from pathlib import Path
import mimetypes
import os
import time
import warnings
import threading
from uuid import uuid4

from agentbridge.documents import document_material
from tempfile import TemporaryDirectory
from agentbridge.media import MAX_DOWNLOAD_BYTES, safe_filename
from agentbridge.sales.images import image_suffix
from agentbridge.sales.audio import audio_suffix
from .store import WebError


def classify(path, filename):
    with path.open('rb') as stream:
        header = stream.read(4096)
    suffix = image_suffix(header)
    if suffix:
        from PIL import Image
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.width*image.height>40_000_000:
                        raise ValueError('pixels')
                    image.verify()
        except Exception:
            raise WebError(422, 'Изображение повреждено или превышает 40 миллионов пикселей.') from None
        return 'image', suffix, mimetypes.guess_type('a'+suffix)[0]
    suffix = audio_suffix(header)
    if suffix:
        return 'audio', suffix, {'.m4a':'audio/mp4','.webm':'audio/webm','.ogg':'audio/ogg'}.get(suffix, mimetypes.guess_type('a'+suffix)[0] or 'audio/mpeg')
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.txt', '.md', '.pdf', '.docx'}:
        raise WebError(415, 'Поддерживаются изображения, аудио, TXT, MD, PDF и DOCX.')
    target = path.with_suffix(suffix)
    path.replace(target)
    try:
        with TemporaryDirectory(prefix='document-check-') as folder:
            document_material(target, Path(folder))
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise WebError(422, str(exc) if isinstance(exc, ValueError) else 'Не удалось прочитать документ.') from None
    target.replace(path)
    return 'document', suffix, mimetypes.guess_type('a'+suffix)[0] or 'text/plain'


_upload_slots = threading.BoundedSemaphore(2)


def upload(store, user, product, context, filename, stream, size):
    if not _upload_slots.acquire(blocking=False):
        raise WebError(429, 'Загрузка занята. Повторите через несколько секунд.')
    try:
        return _upload(store,user,product,context,filename,stream,size)
    finally:
        _upload_slots.release()


def _upload(store, user, product, context, filename, stream, size):
    if not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise WebError(413, 'Максимальный размер файла — 20 МБ')
    store.throttle('upload:'+str(user['id']), limit=30, seconds=60)
    folder = store.path.parent / 'media'
    folder.mkdir(parents=True, exist_ok=True)
    if os.name == 'posix':
        folder.chmod(0o700)
    cleanup(store)
    with store.connect() as db:
        rows = db.execute('SELECT path FROM media WHERE user_id=?', (user['id'],)).fetchall()
    if sum(Path(r['path']).stat().st_size for r in rows if Path(r['path']).is_file()) + size > 200 * 1024 * 1024:
        raise WebError(413, 'Достигнут лимит вложений 200 МБ; дождитесь очистки кеша.')
    ident = uuid4().hex
    temp = folder / (ident+'.part')
    try:
        remaining = size
        with temp.open('xb') as out:
            while remaining:
                chunk = stream.read(min(65536, remaining))
                if not chunk:
                    raise WebError(400, 'Загрузка оборвалась; файл не сохранён')
                out.write(chunk)
                remaining -= len(chunk)
        kind, suffix, mime = classify(temp, filename)
        dest = temp.with_suffix(suffix)
        temp.replace(dest)
        try:
            with store.connect() as db:
                db.execute('''INSERT INTO media(id,user_id,product,context,filename,kind,mime,path,created)
                    VALUES (?,?,?,?,?,?,?,?,?)''', (ident, user['id'], product, context,
                    safe_filename(filename, 'file'+suffix), kind, mime, str(dest), time.time()))
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        return dict(id=ident, filename=safe_filename(filename, 'file'+suffix), kind=kind)
    finally:
        temp.unlink(missing_ok=True)


def cleanup(store):
    folder=store.path.parent/'media'
    if folder.is_dir():
        for path in folder.glob('*.part'):
            if path.stat().st_mtime<time.time()-3600:
                path.unlink(missing_ok=True)
    with store.connect() as db:
        rows = db.execute('''SELECT * FROM media WHERE created<? AND
            (file_id!='' OR (job_id IS NULL AND user_id>0))''', (time.time()-7*86400,)).fetchall()
        for row in rows:
            path = Path(row['path'])
            if path.is_relative_to(store.path.parent/'media'):
                path.unlink(missing_ok=True)
            if not row['file_id']:
                db.execute('DELETE FROM media WHERE id=?', (row['id'],))
