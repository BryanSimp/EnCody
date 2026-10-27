import datetime as dt
import sqlite3
import threading
import time

from reencoder import schedule
from reencoder.db import DB
from reencoder.encoder import Encode
from reencoder.worker import throttle_off

from .test_worker import fake_job, make_worker

NOON = dt.datetime(2026, 10, 28, 12, 0)
NIGHT = dt.datetime(2026, 10, 28, 23, 0)


def test_throttle_cycle():
    # 50%: runs the first 10 s of every 20 s
    assert [throttle_off(50, t) for t in (0, 9.9, 10, 19.9, 20)] == [False, False, True, True, False]
    assert not any(throttle_off(100, t) for t in range(0, 40))
    assert sum(throttle_off(25, t / 10) for t in range(200)) == 150  # paused 75% of the time


def test_daytime_runs_outside_window(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch)
    w.tick(NOON)
    assert started == [] and w.mode is None              # daytime off: nothing during the day
    w.db.set_settings({"daytime_enabled": True})
    w.tick(NOON)
    assert len(started) == 1 and w.mode == "day"
    assert w.throttled(w.db.settings())
    w.tick(NIGHT)
    assert w.mode == "night" and not w.throttled(w.db.settings())


def test_run_now_is_full_speed_and_stop_holds_day(tmp_path, monkeypatch):
    w, started = make_worker(tmp_path, monkeypatch, daytime_enabled=True)
    w.tick(NOON)
    assert w.throttled(w.db.settings())
    w.manual = True                                       # Run now
    assert not w.throttled(w.db.settings())
    w.stop()
    started.clear()
    w.tick(NOON + dt.timedelta(hours=2))
    assert started == []                                  # stopped for the rest of the day
    w.tick(NIGHT)
    assert len(started) == 1                              # night window starts again


def test_window_end_abort_not_applied_when_daytime_continues(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, daytime_enabled=True, window_end_action="abort")
    job = fake_job(w)
    w.tick(NIGHT)
    w.jobs = {"/x": job}
    w.tick(dt.datetime(2026, 10, 29, 7, 0, 1))
    assert not job.encode.aborted and w.mode == "day"


def test_pause_time_excluded_from_speed():
    e = Encode(["sleep", "5"], 100)
    t = threading.Thread(target=e.run)
    t.start()
    time.sleep(0.3)
    e.pause(True)
    time.sleep(1.0)
    e.pause(False)
    assert e.active_seconds() < time.time() - e.started - 0.9
    e.abort()
    t.join()


def test_days_needed(s):
    s.update(daytime_enabled=True, daytime_limit_pct=50)
    # night 9 h full + 15 h at 50% = 16.5 h of encoding per day
    assert schedule.days_needed(16.5 * 3600, s) == 1
    assert schedule.days_needed(91 * 3600, s) == 6


def test_queue_eta_reports_days(tmp_path, monkeypatch):
    w, _ = make_worker(tmp_path, monkeypatch, daytime_enabled=True)
    monkeypatch.setattr(w, "speed", lambda: 1.0)
    monkeypatch.setattr(w, "queue_totals", lambda: {"files": 5, "bytes": 0, "media_sec": 91 * 3600})
    q = w.queue_eta(w.db.settings(), NOON)
    assert (q["windows"], q["windows_label"]) == (6, "days")


def test_history_migration(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT, new_path TEXT, "
                "started REAL, finished REAL, orig_size INTEGER, new_size INTEGER, saved INTEGER DEFAULT 0, "
                "media_sec REAL, result TEXT, message TEXT)")
    con.execute("INSERT INTO history(started, finished, media_sec, result) VALUES (0, 100, 400, 'done')")
    con.commit()
    con.close()
    db = DB(path)
    assert db.recent_speeds() == [(400, 100)]            # old rows fall back to wall time
    db.add_history(started=0, finished=100, media_sec=400, result="done", active_sec=50)
    assert db.recent_speeds()[0] == (400, 50)

