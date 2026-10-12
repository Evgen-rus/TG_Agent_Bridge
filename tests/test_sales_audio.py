import asyncio
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from agentbridge.media import MAX_DOWNLOAD_BYTES
from agentbridge.sales.audio import AudioWorker, audio_suffix


class Store:
    def __init__(self, **changes):
        self.row = dict(id=1, event_id=1, chat_id=-100, topic_id=10, message_id=20,
                        file_id="telegram-file", filename="customer-secret.opus", mime="audio/ogg",
                        file_size=20, caption="Manager caption", attempts=0, path="", state="pending", **changes)
        self.failures = []
        self.transcripts = []
        self.downloads = []
        self.cleanup = []

    def awaiting_audio(self, chat):
        return [self.row] if self.row["state"] in {"pending", "downloaded"} else []

    def audio_downloaded(self, event_id, path):
        self.row.update(path=path, state="downloaded")
        self.downloads.append(path)

    def audio_transcribed(self, event_id, transcript):
        self.transcripts.append(transcript)
        self.row["state"] = "transcribed"

    def audio_failed(self, event_id, reason, terminal=False):
        self.failures.append((reason, terminal))
        self.row["attempts"] += 1
        if terminal:
            self.row["state"] = "failed"

    def audio_cleanup(self):
        return self.cleanup

    def audio_cleaned(self, event_id):
        self.cleanup = []


class Bot:
    def __init__(self, content=b"OggS" + b"0" * 20 + b"OpusHead", size=30):
        self.content = content
        self.size = size
        self.requests = 0
        self.downloads = 0

    async def get_file(self, file_id):
        self.requests += 1

        async def download_to_drive(custom_path):
            self.downloads += 1
            Path(custom_path).write_bytes(self.content)

        return NS(file_size=self.size, download_to_drive=download_to_drive)


def run(worker, bot):
    asyncio.run(worker.run_once(bot, -100))


@pytest.mark.parametrize("content,suffix", [
    (b"OggS01234OpusHead", ".ogg"), (b"OggS01234vorbis", ".ogg"),
    (b"ID3abc", ".mp3"), (b"\xff\xfbabc", ".mp3"),
    (b"RIFF0000WAVEabc", ".wav"), (b"0000ftypM4A ", ".m4a"),
    (b"\x1a\x45\xdf\xa3xxxwebm", ".webm"), (b"fLaCdata", ".flac"),
])
def test_download_actual_container_and_filename(tmp_path, content, suffix):
    store, bot = Store(), Bot(content)
    calls = []

    async def stt(path, **settings):
        calls.append((path, settings, path.read_bytes()))
        return " Full transcript "

    worker = AudioWorker(store, tmp_path, "secret", "model", transcriber=stt)
    run(worker, bot)
    run(worker, bot)
    assert len(calls) == bot.downloads == 1
    assert calls[0][0].suffix == suffix
    assert calls[0][2] == content
    assert store.transcripts == ["Full transcript"]
    assert store.row["caption"] == "Manager caption"
    assert "customer-secret" not in str(calls[0][0])


@pytest.mark.parametrize("where", ["metadata", "remote", "download", "cached"])
def test_size_limit_no_stt(tmp_path, where):
    store, bot = Store(), Bot()
    if where == "metadata":
        store.row["file_size"] = MAX_DOWNLOAD_BYTES + 1
    elif where == "remote":
        bot.size = MAX_DOWNLOAD_BYTES + 1
    elif where == "download":
        bot.content = b"x" * (MAX_DOWNLOAD_BYTES + 1)
    else:
        path = tmp_path / "cached.ogg"
        path.write_bytes(b"x" * (MAX_DOWNLOAD_BYTES + 1))
        store.row["path"] = str(path)

    async def stt(*args, **kwargs):
        pytest.fail("Oversized file reached STT")

    run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), bot)
    assert store.failures == [("audio_too_large", True)]
    assert not store.transcripts
    if where in {"metadata", "remote", "cached"}:
        assert not bot.downloads


@pytest.mark.parametrize("content", [b"%PDF-1.7", b"RIFF0000AVI ", b"OggSnot-an-audio", b"arbitrary"])
def test_invalid_container_no_stt(tmp_path, content):
    store = Store()

    async def stt(*args, **kwargs):
        pytest.fail("Invalid file reached STT")

    run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), Bot(content))
    assert store.failures == [("audio_invalid", True)]


def test_empty_transcript_is_terminal(tmp_path):
    store = Store()

    async def stt(*args, **kwargs):
        return "  "

    run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), Bot())
    assert store.failures == [("audio_transcription_empty", True)]
    assert not store.transcripts


def test_retry_reuses_download_and_five_attempts_bound(tmp_path, caplog):
    store, bot = Store(), Bot()

    async def stt(*args, **kwargs):
        raise RuntimeError("SECRET customer transcript and key")

    for _ in range(6):
        run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), bot)
    assert bot.downloads == 1
    assert len(store.failures) == 5
    assert store.failures[-1] == ("audio_transcription_failed", True)
    assert "SECRET" not in caplog.text
    assert not store.transcripts


@pytest.mark.parametrize("status,code,expected", [
    (401, None, "audio_auth"), (403, None, "audio_auth"),
    (429, "insufficient_quota", "audio_quota"), (400, None, "audio_request"),
])
def test_permanent_stt_errors(tmp_path, status, code, expected):
    store = Store()

    async def stt(*args, **kwargs):
        exc = RuntimeError("private")
        exc.status_code, exc.body = status, {"error": {"code": code}}
        raise exc

    run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), Bot())
    assert store.failures == [(expected, True)]


def test_cached_audio_recovered_and_deleted_only_after_analysis(tmp_path):
    store, bot = Store(), Bot()
    path = tmp_path / "persisted.ogg"
    path.write_bytes(bot.content)
    store.row["path"] = str(path)

    async def stt(*args, **kwargs):
        return "x" * 17000

    worker = AudioWorker(store, tmp_path, "key", "model", transcriber=stt)
    run(worker, bot)
    assert not bot.requests
    assert len(store.transcripts[0]) == 17000
    assert path.exists()
    store.cleanup = [dict(event_id=1, path=str(path))]
    run(worker, bot)
    assert not path.exists()
    assert not store.cleanup


def test_retry_deadline(tmp_path):
    store, bot = Store(), Bot()
    store.row["retry_at"] = 10**12
    run(AudioWorker(store, tmp_path, "key", "model"), bot)
    assert not bot.requests


def test_unknown_remote_size_telegram_too_big_is_terminal(tmp_path):
    class TooBig(Bot):
        async def get_file(self, file_id):
            raise RuntimeError('File is too big')
    store = Store()
    store.row['file_size'] = None
    run(AudioWorker(store, tmp_path, 'key', 'model'), TooBig())
    assert store.failures == [('audio_too_large', True)]


def test_cancellation_preserves_download(tmp_path):
    store, bot = Store(), Bot()

    async def stt(*args, **kwargs):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), bot)
    assert Path(store.row["path"]).exists()
    assert not store.failures


def test_download_error_logs_no_exception_or_customer_filename(tmp_path, caplog):
    store, bot = Store(), Bot()

    async def get_file(file_id):
        raise RuntimeError("SECRET credentials")

    bot.get_file = get_file
    run(AudioWorker(store, tmp_path, "key", "model"), bot)
    assert store.failures == [("audio_download_failed", False)]
    assert "SECRET" not in caplog.text
    assert "customer-secret" not in caplog.text


def test_empty_download_no_stt(tmp_path):
    store = Store()

    async def stt(*args, **kwargs):
        pytest.fail("Empty download reached STT")

    run(AudioWorker(store, tmp_path, "key", "model", transcriber=stt), Bot(b"", size=0))
    assert store.failures == [("audio_download_failed", False)]
    assert not list(tmp_path.rglob("*.part"))


def test_shared_transcriber_preserves_basename_bytes_and_parameters(tmp_path, monkeypatch):
    import agentbridge.transcribe as module
    path = tmp_path / "call.mp3"
    path.write_bytes(b"ID3test")
    calls = []

    async def transcribe(audio, **kwargs):
        calls.append((audio, kwargs))
        return "text"

    monkeypatch.setattr(module, "transcribe_audio_bytes", transcribe)
    assert asyncio.run(module.transcribe_audio_file(path, api_key="key", model="model")) == "text"
    assert calls == [(b"ID3test", dict(api_key="key", model="model", filename="call.mp3"))]


def real_store(path, caption="Manager caption"):
    from agentbridge.sales.storage import SalesStore
    store = SalesStore(path)
    values = dict(update_id=1, chat_id=-100, topic_id=10, title="deal", message_id=20,
                  author_id=7, author="Manager", date="2026-10-12", text=caption, source="audio_pending",
                  audio=dict(file_id="file", filename="call.opus", mime="audio/ogg", file_size=30, kind="voice"))
    assert store.ingest(**values)
    assert not store.ingest(**values)
    return store


def test_real_sqlite_restart_checkpoint_transcript_and_cleanup(tmp_path):
    from agentbridge.sales.storage import SalesStore
    database = tmp_path / "sales.sqlite3"
    store, bot = real_store(database), Bot()

    async def failed(*args, **kwargs):
        raise TimeoutError("secret customer speech")

    run(AudioWorker(store, tmp_path / "media", "key", "model", transcriber=failed), bot)
    with store.connect() as db:
        row = db.execute("SELECT * FROM audio_jobs").fetchone()
        assert row["state"] == "downloaded" and row["attempts"] == 1
        assert Path(row["path"]).exists()
        db.execute("UPDATE audio_jobs SET retry_at=0")
    store = SalesStore(database)

    async def stt(*args, **kwargs):
        return "x" * 17000

    worker = AudioWorker(store, tmp_path / "media", "key", "model", transcriber=stt)
    run(worker, bot)
    run(worker, bot)
    with store.connect() as db:
        event = db.execute("SELECT * FROM events").fetchone()
        job = db.execute("SELECT * FROM audio_jobs").fetchone()
        assert event["source"] == "audio_transcript" and event["processed"] == 0
        assert event["text"].startswith("Комментарий менеджера:\nManager caption\n\nРаспознанная речь:\n")
        assert len(job["transcript"]) == 17000
        downloaded = Path(job["path"])
        assert downloaded.exists()
        db.execute("UPDATE events SET processed=1")
    run(worker, bot)
    assert bot.downloads == 1
    assert not downloaded.exists()
    with store.connect() as db:
        assert db.execute("SELECT path FROM audio_jobs").fetchone()[0] == ""
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_real_sqlite_empty_terminal_never_analyzed(tmp_path):
    store, bot = real_store(tmp_path / "sales.sqlite3", caption=""), Bot()

    async def stt(*args, **kwargs):
        return ""

    worker = AudioWorker(store, tmp_path / "media", "key", "model", transcriber=stt)
    run(worker, bot)
    run(worker, bot)
    with store.connect() as db:
        event = db.execute("SELECT * FROM events").fetchone()
        job = db.execute("SELECT * FROM audio_jobs").fetchone()
        assert event["processed"] == 3 and event["source"] == "audio_pending"
        assert job["state"] == "failed" and not job["transcript"] and not job["path"]
        assert not db.execute("SELECT 1 FROM recommendations").fetchone()
        assert db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
