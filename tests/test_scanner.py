import os
import time

from reencoder import scanner
from reencoder.db import DB

MB = 1024 * 1024


def probe_for(codec="h264", hdr=False, dv=False):
    return {"container": "matroska", "duration": 100.0, "bitrate": 0, "codec": codec, "width": 1920,
            "height": 1080, "pix_fmt": "yuv420p", "ten_bit": False, "hdr": hdr, "dv": dv, "color": {},
            "video_index": 0, "video": 1, "audio": 1, "subs": 0, "sub_codecs": []}


def make(path, size_mb=300, age_min=60):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(int(size_mb * MB))  # sparse: no real disk use
    t = time.time() - age_min * 60
    os.utime(path, (t, t))
    return str(path)


def test_dry_run_candidates_and_skips(media, s, tmp_path):
    codecs = {}
    m = media / "movies"
    todo = make(m / "h264.mkv"); codecs[todo] = probe_for("h264")
    av1 = make(m / "av1.mkv"); codecs[av1] = probe_for("av1")
    hevc = make(m / "hevc.mkv"); codecs[hevc] = probe_for("hevc")
    small = make(m / "small.mkv", size_mb=50); codecs[small] = probe_for()
    recent = make(m / "recent.mkv", age_min=5); codecs[recent] = probe_for()
    hl = make(m / "linked.mkv"); codecs[hl] = probe_for()
    os.link(hl, tmp_path / "data" / "media" / "seeding.mkv")
    hdr = make(m / "hdr.mkv"); codecs[hdr] = probe_for(hdr=True)
    dv = make(m / "dv.mkv"); codecs[dv] = probe_for(hdr=True, dv=True)
    make(m / ".hidden.mkv")
    make(m / "notes.txt")
    s["folders"] = [str(m)]

    db = DB(str(tmp_path / "t.db"))
    calls = []

    def fake_probe(p):
        calls.append(p)
        return codecs[p]

    scanner.scan(db, s, probe=fake_probe)
    rows = db.all_files()
    st = {os.path.basename(p): (r["status"], r["reason"]) for p, r in rows.items()}
    assert st["h264.mkv"][0] == "todo"
    assert st["av1.mkv"][0] == "ok"
    assert st["hevc.mkv"][0] == "ok"
    assert st["small.mkv"] == ("skipped", "smaller than 200 MB")
    assert st["recent.mkv"] == ("skipped", "modified recently")
    assert st["linked.mkv"][1].startswith("hardlinked")
    assert st["hdr.mkv"][1].startswith("HDR")
    assert st["dv.mkv"][1].startswith("Dolby Vision")
    assert ".hidden.mkv" not in st and "notes.txt" not in st
    assert [r["path"] for r in db.queue("size_desc")] == [todo]
    assert recent not in calls and small not in calls  # stat-only skips never probe

    # rescan without changes does not re-probe
    calls.clear()
    scanner.scan(db, s, probe=fake_probe)
    assert calls == []

    # a changed file is re-probed and its failure count reset
    db.set_status(todo, "failed", "boom", failures=2)
    scanner.scan(db, s, probe=fake_probe)
    assert db.file(todo)["status"] == "failed"
    with open(todo, "ab") as f:
        f.write(b"x")
    os.utime(todo, (time.time() - 3600, time.time() - 3600))
    scanner.scan(db, s, probe=fake_probe)
    assert calls == [todo] and db.file(todo)["status"] == "todo" and db.file(todo)["failures"] == 0


def test_excludes_and_deleted_files(media, s, tmp_path):
    a = make(media / "tvshows" / "Show" / "a.mkv")
    b = make(media / "tvshows" / "Other" / "b.mkv")
    s["folders"] = [str(media)]
    s["excludes"] = [str(media / "tvshows" / "Other")]
    db = DB(str(tmp_path / "t.db"))
    scanner.scan(db, s, probe=lambda p: probe_for())
    assert set(db.all_files()) == {a}
    os.remove(a)
    scanner.scan(db, s, probe=lambda p: probe_for())
    assert db.all_files() == {}
    assert b


def test_hardlink_toggle_off(media, s, tmp_path):
    a = make(media / "movies" / "a.mkv")
    os.link(a, media / "a-seed.mkv")
    s["folders"] = [str(media / "movies")]
    s["skip_hardlinked"] = False
    db = DB(str(tmp_path / "t.db"))
    scanner.scan(db, s, probe=lambda p: probe_for())
    assert db.file(a)["status"] == "todo"


def test_convert_to_mkv_remux(media, s, tmp_path):
    a = make(media / "movies" / "a.mp4")
    s["folders"] = [str(media / "movies")]
    s["convert_to_mkv"] = True
    db = DB(str(tmp_path / "t.db"))
    scanner.scan(db, s, probe=lambda p: probe_for("av1"))
    assert (db.file(a)["status"], db.file(a)["reason"]) == ("todo", "remux to mkv")
