"""End-to-end: real ffmpeg, software AV1 (no GPU needed)."""
import os
import signal
import sys
import time

import pytest

from reencoder import encoder, media, worker as worker_mod
from reencoder.db import DB
from reencoder.worker import Worker

from .conftest import make_clip, needs_svt

pytestmark = needs_svt


@pytest.fixture
def setup(media, tmp_path):
    src = make_clip(media / "tvshows" / "Show" / "S01E01.mkv")
    db = DB(str(tmp_path / "t.db"))
    db.set_settings({"folders": [str(media)], "codec": "libsvtav1", "quality": 45, "preset": "12",
                     "min_size_mb": 0, "skip_recent_minutes": 0, "dry_run": False, "decode_tail_sec": 2})
    w = Worker(db)
    w._scan(db.settings())
    return str(src), db, w


def run_one(w, db, before_wait=None):
    s = db.settings()
    row = w._next_job(s)
    assert row, "expected a job"
    w._start_job(row, s)
    job = w.jobs[row["path"]]
    if before_wait:
        before_wait(job)
    for _ in range(600):
        if not w.jobs:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_encode_verify_replace(setup):
    src, db, w = setup
    orig_size = os.path.getsize(src)
    orig = media.probe(src)
    job = run_one(w, db)
    assert os.path.exists(src)
    new = media.probe(src)
    assert new["codec"] == "av1"
    assert (new["audio"], new["subs"]) == (orig["audio"], orig["subs"]) == (2, 1)
    assert abs(new["duration"] - orig["duration"]) < 1
    assert os.path.getsize(src) < orig_size
    assert not os.path.exists(job.tmp)
    row = db.file(src)
    assert row["status"] == "done"
    h = db.history()[0]
    assert h["result"] == "done" and h["saved"] == orig_size - os.path.getsize(src)
    # a rescan keeps it done and finds nothing to do
    w._scan(db.settings())
    assert db.file(src)["status"] == "done" and w._next_job(db.settings()) is None


def test_killed_ffmpeg_leaves_original(setup):
    src, db, w = setup
    before = open(src, "rb").read()

    def kill(job):
        for _ in range(200):
            if job.encode and job.encode.proc:
                break
            time.sleep(0.01)
        os.kill(job.encode.proc.pid, signal.SIGKILL)

    job = run_one(w, db, before_wait=kill)
    assert open(src, "rb").read() == before
    assert not os.path.exists(job.tmp)
    row = db.file(src)
    assert row["failures"] == 1 and row["status"] == "todo"
    assert db.history()[0]["result"] == "failed"


def test_truncated_output_rejected(setup, monkeypatch):
    src, db, w = setup
    before = open(src, "rb").read()
    real = encoder.build_cmd

    def truncated(*a, **kw):
        cmd = real(*a, **kw)
        return cmd[:-1] + ["-t", "2", cmd[-1]]

    monkeypatch.setattr(worker_mod.encoder, "build_cmd", truncated)
    job = run_one(w, db)
    assert open(src, "rb").read() == before and not os.path.exists(job.tmp)
    assert "duration mismatch" in db.history()[0]["message"]


def test_no_savings_marks_file(setup):
    src, db, w = setup
    before = open(src, "rb").read()
    db.set_settings({"min_savings_pct": 99})
    job = run_one(w, db)
    row = db.file(src)
    assert row["status"] == "no_savings" and row["reason"].startswith("not smaller: ")
    assert db.counts()["skipped"] == 1                       # shown as skipped
    assert open(src, "rb").read() == before and not os.path.exists(job.tmp)
    assert w._next_job(db.settings()) is None
    w._scan(db.settings())                                   # stays skipped, not re-queued
    assert db.file(src)["status"] == "no_savings"


def fake_ffmpeg(code):
    """A stand-in ffmpeg: prints an error, then exits with `code` (None = hang)."""
    body = "import sys, time; sys.stderr.write('fake qsv error\\n'); sys.stderr.flush(); "
    body += "time.sleep(60)" if code is None else f"sys.exit({code})"
    return [sys.executable, "-c", body]


def test_hw_decode_failure_falls_back_to_cpu(setup, monkeypatch, logs):
    src, db, w = setup
    real = encoder.build_cmd
    calls = []

    def build(*a, hw_decode=True, **kw):
        calls.append(hw_decode)
        return fake_ffmpeg(1) if hw_decode else real(*a, hw_decode=hw_decode, **kw)

    monkeypatch.setattr(worker_mod.encoder, "hw_decoder", lambda p, s: "h264_qsv")
    monkeypatch.setattr(worker_mod.encoder, "build_cmd", build)
    job = run_one(w, db)
    assert calls == [True, False]
    assert db.file(src)["status"] == "done" and media.probe(src)["codec"] == "av1"
    assert job.decoder is None
    assert any("HW DECODE failed (h264_qsv)" in m and "fake qsv error" in m and "retrying with CPU decode" in m
               for _, m in logs)
    assert any("decode=h264_qsv" in m for _, m in logs if m.startswith("START"))


def test_cpu_failure_is_not_retried(setup, monkeypatch):
    src, db, w = setup
    calls = []
    monkeypatch.setattr(worker_mod.encoder, "build_cmd", lambda *a, **kw: calls.append(1) or fake_ffmpeg(1))
    run_one(w, db)
    assert calls == [1] and db.file(src)["status"] == "todo" and db.file(src)["failures"] == 1


def test_stalled_job_is_killed_and_failed(setup, monkeypatch):
    src, db, w = setup
    before = open(src, "rb").read()
    monkeypatch.setattr(worker_mod.encoder, "hw_decoder", lambda p, s: "h264_qsv")   # no CPU retry after a stall
    monkeypatch.setattr(worker_mod.encoder, "build_cmd", lambda *a, **kw: fake_ffmpeg(None))

    def stall(job):
        for _ in range(300):
            if job.encode and job.encode.running() and job.encode.stderr:
                break
            time.sleep(0.01)
        job.encode.active_seconds = lambda: 11 * 60      # 11 min without progress
        w._watchdog(db.settings(), [job])

    job = run_one(w, db, before_wait=stall)
    row = db.file(src)
    assert row["status"] == "failed" and row["failures"] == 1   # failed now, although max_failures is 2
    assert row["reason"].startswith("stalled: no progress for 10 min at 0%") and "fake qsv error" in row["reason"]
    assert open(src, "rb").read() == before and not os.path.exists(job.tmp)
    assert db.history()[0]["result"] == "failed"


def test_restart_recovery(setup):
    src, db, w = setup
    tmp = os.path.join(os.path.dirname(src), ".S01E01.reencode.tmp.mkv")
    open(tmp, "wb").write(b"partial")
    db.set_status(src, "running")
    Worker(db).recover()
    assert not os.path.exists(tmp)
    assert db.file(src)["status"] == "todo"


def test_stop_aborts_and_cleans(setup):
    src, db, w = setup
    db.set_settings({"preset": "2"})  # slower so we can stop it
    before = open(src, "rb").read()

    def stop(job):
        for _ in range(200):
            if job.encode and job.encode.proc:
                break
            time.sleep(0.01)
        w.stop()

    job = run_one(w, db, before_wait=stop)
    assert open(src, "rb").read() == before and not os.path.exists(job.tmp)
    assert db.file(src)["status"] == "todo" and db.file(src)["failures"] == 0
    assert db.history()[0]["result"] == "aborted"
