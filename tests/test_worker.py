import datetime as dt
import time

from reencoder.db import DB
from reencoder.encoder import Encode
from reencoder.worker import Job, Worker



def make_worker(tmp_path, monkeypatch, **settings):
    db = DB(str(tmp_path / "t.db"))
    db.set_settings({"dry_run": False, **settings})
    w = Worker(db)
    w.scan_requested = False
    w.last_scan = time.time()
    started = []
    monkeypatch.setattr(w, "_next_job", lambda s: None if started else {"path": "/x"})
    monkeypatch.setattr(w, "_start_job", lambda row, s: started.append(row))
    return w, started


def test_window_starts_queue(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch)
    w.tick(dt.datetime(2026, 10, 28, 12, 0))     # outside window
    assert started == []
    w.tick(dt.datetime(2026, 10, 28, 22, 0, 5))  # window open
    assert len(started) == 1


def test_run_now_ignores_schedule(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch)
    w.run_now()
    w.tick(dt.datetime(2026, 10, 28, 12, 0))
    assert len(started) == 1


def test_stop_holds_until_next_window(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch)
    w.tick(dt.datetime(2026, 10, 28, 22, 0))
    started.clear()
    w.stop()
    w.tick(dt.datetime(2026, 10, 28, 23, 0))
    assert started == []
    w.tick(dt.datetime(2026, 10, 29, 12, 0))
    w.tick(dt.datetime(2026, 10, 29, 22, 0))
    assert len(started) == 1


def test_dry_run_never_starts(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch, dry_run=True)
    w.run_now()
    w.tick(dt.datetime(2026, 10, 28, 22, 30))
    assert started == []


def fake_job(w):
    job = Job({"path": "/data/media/x.mkv", "size": 0, "reason": "",
               "probe": {"duration": 100, "codec": "h264", "width": 0, "height": 0}}, w.db.settings())
    job.encode = Encode([], 100)
    return job


def test_window_end_abort(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch, window_end_action="abort")
    job = fake_job(w)
    w.tick(dt.datetime(2026, 10, 28, 22, 0))
    w.jobs = {"/x": job}
    w.tick(dt.datetime(2026, 10, 29, 7, 0, 1))
    assert job.encode.aborted and job.stop_reason == "window ended"


def test_window_end_finish(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch)
    job = fake_job(w)
    w.tick(dt.datetime(2026, 10, 28, 22, 0))
    w.jobs = {"/x": job}
    w.tick(dt.datetime(2026, 10, 29, 7, 0, 1))
    assert not job.encode.aborted
