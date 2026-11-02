import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from reencoder import cleanup
from reencoder.db import DB
from reencoder.settings import validate

NOW = 1_800_000_000
DAY = 86400


def torrent(h, name, done=True, finished=NOW - DAY, size=1000):
    return {"hashString": h, "name": name, "status": 6 if done else 4, "percentDone": 1 if done else 0.5,
            "leftUntilDone": 0 if done else 500, "sizeWhenDone": size, "doneDate": finished if done else 0,
            "addedDate": finished - 600}


def grab(h, item, key="episodeId"):
    return {"downloadId": h.upper(), "eventType": "grabbed", key: item, "data": {}}


def imp(h, item, imported, key="episodeId", dropped=None):
    return {"downloadId": h.upper(), "eventType": "downloadFolderImported", key: item,
            "data": {"droppedPath": dropped or f"/downloads/complete/{os.path.basename(imported)}",
                     "importedPath": imported}}


class Fake:
    """One HTTP server playing Transmission (/transmission/rpc), Sonarr (/sonarr) and Radarr (/radarr)."""

    def __init__(self):
        self.torrents, self.removed, self.calls = [], [], []
        self.history = {"sonarr": [], "radarr": []}
        self.episodes, self.episodefiles, self.movies = {}, {}, {}
        self.auth = None
        self.ignore_download_filter = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def reply(self, code, obj=None, headers=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                fake.auth = self.headers.get("Authorization")
                if self.headers.get("X-Transmission-Session-Id") != "sid":
                    return self.reply(409, headers={"X-Transmission-Session-Id": "sid"})
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.calls.append(req)
                if req["method"] == "torrent-get":
                    return self.reply(200, {"result": "success", "arguments": {"torrents": fake.torrents}})
                if req["method"] == "torrent-remove":
                    assert req["arguments"]["delete-local-data"] is True
                    fake.removed += req["arguments"]["ids"]
                    return self.reply(200, {"result": "success", "arguments": {}})
                self.reply(200, {"result": "method not recognized"})

            def do_GET(self):
                u = urlparse(self.path)
                app, _, rest = u.path.lstrip("/").partition("/")
                if self.headers.get("X-Api-Key") != app + "-key":
                    return self.reply(401, {})
                q = parse_qs(u.query)
                if rest == "api/v3/history":
                    recs = fake.history[app]
                    if not fake.ignore_download_filter:
                        recs = [r for r in recs if r["downloadId"] == q["downloadId"][0]]
                    return self.reply(200, {"page": 1, "records": recs})
                kind, _, num = rest.rpartition("/")
                table = {"api/v3/episode": fake.episodes, "api/v3/episodefile": fake.episodefiles,
                         "api/v3/movie": fake.movies}.get(kind, {})
                if int(num) in table:
                    return self.reply(200, table[int(num)])
                self.reply(404, {})

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.srv.shutdown()


@pytest.fixture
def env(media, tmp_path, fake, s):
    s.update(transmission_url=fake.url, sonarr_url=fake.url + "/sonarr", sonarr_key="sonarr-key",
             radarr_url=fake.url + "/radarr", radarr_key="radarr-key", cleanup_dry_run=False)
    return s, DB(str(tmp_path / "t.db")), media


def put(p, data=b"video"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return str(p)


def by_name(report):
    return {r["name"]: r for r in report["rows"]}


def test_removes_only_fully_imported_torrents_on_the_drive(env, fake):
    s, db, media = env
    ep1 = put(media / "tvshows" / "Show" / "Season 01" / "S01E01.mkv")
    ep2 = put(media / "tvshows" / "Show" / "Season 01" / "S01E02.mkv")
    movie = put(media / "movies" / "Heat (1995)" / "Heat.mkv")
    fake.torrents = [
        torrent("aa", "Show.S01.pack"),              # 2 of 2 imported, both on the drive
        torrent("bb", "Show.S02.pack"),              # 1 of 2 imported
        torrent("cc", "Heat.1995"),                  # Radarr movie
        torrent("dd", "Hand.Added"),                 # nobody knows it
        torrent("ee", "Still.Downloading", done=False),
        torrent("ff", "Show.S03E01"),                # imported, but the file is not on the drive
    ]
    fake.history["sonarr"] = [
        grab("aa", 1), grab("aa", 2), imp("aa", 1, ep1), imp("aa", 2, ep2),
        grab("bb", 3), grab("bb", 4), imp("bb", 3, ep1),
        grab("ff", 9), imp("ff", 9, str(media / "tvshows" / "Show" / "Season 03" / "S03E01.mkv")),
    ]
    fake.history["radarr"] = [grab("cc", 7, "movieId"), imp("cc", 7, movie, "movieId")]
    fake.episodes[9] = {"hasFile": False, "episodeFileId": 0}

    report = cleanup.run(s, db, now=NOW)
    rows = by_name(report)
    assert sorted(fake.removed) == ["aa", "cc"]
    assert rows["Show.S01.pack"]["action"] == "removed"
    assert rows["Heat.1995"]["action"] == "removed" and "Radarr" in rows["Heat.1995"]["reason"]
    assert rows["Show.S02.pack"]["action"] == "kept" and "1 of 2 episodes" in rows["Show.S02.pack"]["reason"]
    assert rows["Hand.Added"]["action"] == "kept"
    assert rows["Show.S03E01"]["action"] == "kept" and "not on the drive" in rows["Show.S03E01"]["reason"]
    assert rows["Still.Downloading"]["action"] == "downloading"
    assert rows["Still.Downloading"]["reason"] == "50% downloaded, downloading in Transmission"
    assert report["rows"][-1]["name"] == "Still.Downloading"          # listed after the finished ones
    assert "1 still downloading" in report["summary"]
    # library files untouched, removals recorded
    assert all(os.path.exists(p) for p in (ep1, ep2, movie))
    assert db.total_cleaned() == 2000
    assert {h["path"] for h in db.history() if h["result"] == "cleaned"} == {"Show.S01.pack", "Heat.1995"}


def test_dry_run_removes_nothing(env, fake):
    s, db, media = env
    s["cleanup_dry_run"] = True
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    fake.torrents = [torrent("aa", "Show.S01E01")]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, ep)]
    report = cleanup.run(s, db, now=NOW)
    assert fake.removed == [] and db.total_cleaned() == 0
    assert by_name(report)["Show.S01E01"]["action"] == "would remove"
    assert report["dry_run"] and "DRY RUN" in report["summary"]
    assert [c["method"] for c in fake.calls] == ["torrent-get"]


def test_min_seed_hours(env, fake):
    s, db, media = env
    s["cleanup_min_seed_hours"] = 48
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    fake.torrents = [torrent("aa", "Young", finished=NOW - 3600), torrent("bb", "Old", finished=NOW - 3 * DAY)]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, ep), grab("bb", 1), imp("bb", 1, ep)]
    rows = by_name(cleanup.run(s, db, now=NOW))
    assert fake.removed == ["bb"]
    assert "at least 48h" in rows["Young"]["reason"]


def test_renamed_or_upgraded_files_count(env, fake):
    s, db, media = env
    mkv = put(media / "movies" / "Old (1990)" / "Old.mkv")                  # Encody turned .avi into .mkv
    newer = put(media / "tvshows" / "Show" / "S01E01 [2160p].mkv")         # Sonarr upgraded the episode
    fake.torrents = [torrent("aa", "Old.1990"), torrent("bb", "Show.S01E01.720p")]
    fake.history["radarr"] = [grab("aa", 5, "movieId"), imp("aa", 5, mkv[:-4] + ".avi", "movieId")]
    fake.history["sonarr"] = [grab("bb", 1), imp("bb", 1, str(media / "tvshows" / "Show" / "S01E01 [720p].mkv"))]
    fake.episodes[1] = {"hasFile": True, "episodeFileId": 11}
    fake.episodefiles[11] = {"path": newer}
    cleanup.run(s, db, now=NOW)
    assert sorted(fake.removed) == ["aa", "bb"]


def test_refuses_unsafe_library_files(env, fake, tmp_path):
    s, db, media = env
    outside = put(tmp_path / "ssd" / "library" / "S01E01.mkv")     # not on a media drive
    real = put(media / "tvshows" / "Show" / "real.mkv")
    link = media / "tvshows" / "Show" / "S01E02.mkv"
    os.symlink(real, link)                                         # symlink into somewhere else
    empty = put(media / "tvshows" / "Show" / "S01E03.mkv", b"")    # zero bytes
    inplace = put(media / "downloads" / "Show.S01E04" / "S01E04.mkv")
    fake.torrents = [torrent(h, n) for h, n in (("aa", "Outside"), ("bb", "Symlink"), ("cc", "Empty"), ("dd", "InPlace"))]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, outside), grab("bb", 2), imp("bb", 2, str(link)),
                              grab("cc", 3), imp("cc", 3, empty), grab("dd", 4), imp("dd", 4, inplace, dropped=inplace)]
    rows = by_name(cleanup.run(s, db, now=NOW))
    assert fake.removed == []
    assert "path mapping" in rows["Outside"]["reason"]
    assert "inside the download" in rows["InPlace"]["reason"]


def test_path_mapping(env, fake):
    s, db, media = env
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    s["cleanup_path_map"] = ["/tv => " + str(media / "tvshows"), "/t => /nowhere"]
    fake.torrents = [torrent("aa", "Show.S01E01")]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, "/tv/Show/S01E01.mkv")]
    cleanup.run(s, db, now=NOW)
    assert fake.removed == ["aa"] and os.path.exists(ep)
    assert cleanup.map_path("/tv2/x.mkv", s["cleanup_path_map"]) == "/tv2/x.mkv"


def test_history_filtered_client_side(env, fake):
    """If Sonarr ignored the downloadId filter, other downloads' imports must not count."""
    s, db, media = env
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    fake.ignore_download_filter = True
    fake.torrents = [torrent("aa", "Mine")]
    fake.history["sonarr"] = [grab("aa", 1), grab("zz", 2), imp("zz", 2, ep)]
    rows = by_name(cleanup.run(s, db, now=NOW))
    assert fake.removed == [] and "0 of 1" in rows["Mine"]["reason"]


def test_arr_down_keeps_everything(env, fake):
    s, db, media = env
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    s["radarr_key"] = "wrong"
    fake.torrents = [torrent("aa", "Show.S01E01")]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, ep)]
    report = cleanup.run(s, db, now=NOW)
    assert fake.removed == [] and "could not ask Radarr" in by_name(report)["Show.S01E01"]["reason"]


def test_transmission_handshake_auth_and_url():
    assert cleanup.rpc_url("http://t:9091") == "http://t:9091/transmission/rpc"
    assert cleanup.rpc_url("http://t:9091/transmission/web/") == "http://t:9091/transmission/rpc"
    assert cleanup.rpc_url("http://t:9091/transmission/rpc") == "http://t:9091/transmission/rpc"
    f = Fake()
    try:
        tr = cleanup.Transmission(f.url, "u", "p")
        f.torrents = [torrent("aa", "x")]
        assert tr.torrents()[0]["name"] == "x"
        assert f.auth == "Basic " + base64.b64encode(b"u:p").decode()
        tr.remove("aa")
        assert f.removed == ["aa"]
    finally:
        f.srv.shutdown()


def test_unreachable_transmission(env):
    s, db, media = env
    s["transmission_url"] = "http://127.0.0.1:9"
    with pytest.raises(RuntimeError, match="could not reach Transmission"):
        cleanup.run(s, db, now=NOW)


def test_cleaner_schedule(tmp_path, s):
    c = cleanup.Cleaner(DB(str(tmp_path / "t.db")))
    assert not c.due(s)                                     # no Transmission URL
    s["transmission_url"] = "http://t:9091"
    assert not c.due(s)                                     # automatic cleanup off
    c.requested = True
    assert c.due(s)                                         # "Clean up now" works even when off
    c.requested = False
    s["cleanup_enabled"] = True
    assert c.due(s)
    c.last_run = time.time()
    assert not c.due(s) and c.due(s, now=time.time() + 3601)


def test_path_map_validation():
    assert validate("cleanup_path_map", [" /tv=>/data/media/tvshows ", ""]) == ["/tv => /data/media/tvshows"]
    for bad in (["/tv /data/media"], ["tv => /data"], ["/tv => data"]):
        with pytest.raises(ValueError):
            validate("cleanup_path_map", bad)


def test_snake_case_transmission(env, fake):
    """A Transmission that answers in snake_case keys must still be understood."""
    s, db, media = env
    ep = put(media / "tvshows" / "Show" / "S01E01.mkv")
    t = torrent("aa", "Show.S01E01")
    fake.torrents = [{"hash_string": t["hashString"], "left_until_done": 0, "percent_done": 1,
                      "size_when_done": 1000, "done_date": t["doneDate"], "added_date": t["addedDate"],
                      "name": t["name"], "status": 6}]
    fake.history["sonarr"] = [grab("aa", 1), imp("aa", 1, ep)]
    cleanup.run(s, db, now=NOW)
    assert fake.removed == ["aa"]
