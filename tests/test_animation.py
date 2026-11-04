import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reencoder import encoder, integrations

AUTH = 'MediaBrowser Token="k"'


class FakeServer:
    """Records every request (method, path, headers) and answers with self.status / self.body."""

    def __init__(self, body=None, status=200):
        self.body, self.status, self.requests = body, status, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({"method": self.command, "path": self.path,
                                       "headers": dict(self.headers), "body": self.rfile.read(n)})
                raw = json.dumps(outer.body).encode() if outer.body is not None else b""
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _answer

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def server():
    made = []

    def make(body=None, status=200):
        made.append(FakeServer(body, status))
        return made[-1]

    yield make
    for f in made:
        f.close()
    integrations._animated_dirs, integrations._animated_files = set(), set()
    integrations._session_check_failed = False


@pytest.fixture
def library(media):
    return {"Items": [
        {"Type": "Series", "Id": "at", "Path": str(media / "tvshows" / "Adventure Time"),
         "Genres": ["Animation", "Comedy"]},
        {"Type": "Series", "Id": "rv", "Path": str(media / "tvshows" / "Riviera"), "Genres": ["Drama"]},
        {"Type": "Movie", "Path": str(media / "movies" / "Shrek (2001)" / "Shrek.avi"), "Genres": ["Animation"]},
        {"Type": "Movie", "Path": str(media / "movies" / "Heat (1995)" / "Heat.mkv"), "Genres": ["Crime"]},
        {"Type": "Series", "Id": "fr", "Path": str(media / "tvshows" / "Frieren"), "Genres": ["Anime"]},
        # Episodes carry no genres; they inherit them from their series.
        {"Type": "Episode", "SeriesId": "at", "Genres": [],
         "Path": str(media / "tvshows" / "Adventure Time" / "Season 1" / "e1.mkv")},
        {"Type": "Episode", "SeriesId": "rv", "Genres": [],
         "Path": str(media / "tvshows" / "Riviera" / "Season 1" / "e1.mkv")},
    ]}


def test_jellyfin_headers():
    assert integrations.jellyfin_headers({"jellyfin_key": "abc123"}) == {
        "Authorization": 'MediaBrowser Token="abc123"'}


def test_three_tiers(media, s, server, library):
    jf = server(library)
    s.update(detect_animation=True, jellyfin_url=jf.url, jellyfin_key="k",
             quality=22, quality_tv=24, quality_animation=28)
    assert integrations.refresh_animation(s) == {"series": 2, "movies": 1, "episodes": 1, "outside": 0}
    req = jf.requests[0]
    assert req["headers"]["Authorization"] == AUTH
    assert "X-Emby-Token" not in req["headers"] and "api_key" not in req["path"].lower()
    assert "IncludeItemTypes=Movie,Series,Episode" in req["path"] and "Fields=Path,Genres" in req["path"]
    cases = {
        media / "tvshows" / "Adventure Time" / "Season 4" / "e1.mkv": ("animation", 28),
        media / "tvshows" / "Frieren" / "Season 1" / "e1.mkv": ("animation", 28),
        media / "tvshows" / "Riviera" / "Season 1" / "e1.mkv": ("tv", 24),
        media / "movies" / "Shrek (2001)" / "Shrek.mkv": ("animation", 28),   # renamed .avi -> .mkv
        media / "movies" / "Heat (1995)" / "Heat.mkv": ("movie", 22),
        media / "movies" / "Unknown (2020)" / "Unknown.mkv": ("movie", 22),
    }
    for path, (tier, q) in cases.items():
        assert (encoder.tier(str(path)), encoder.quality_for(str(path), s)) == (tier, q), path


def test_episode_uses_series_genres_by_series_id(tmp_path):
    """Episodes outside the series folder Jellyfin reports (another drive) still match via SeriesId."""
    items = [
        {"Type": "Series", "Id": "pf", "Path": "/data/media/tvshows/Phineas and Ferb", "Genres": ["ANIMATION"]},
        {"Type": "Episode", "SeriesId": "pf", "Genres": [],
         "Path": "/data/media3/tvshows/Phineas and Ferb/Season 2/S02E01.mkv"},
        {"Type": "Series", "Id": "office", "Path": "/data/media/tvshows/The Office", "Genres": ["Comedy"]},
        {"Type": "Episode", "SeriesId": "office", "Genres": [],
         "Path": "/data/media/tvshows/The Office/Season 1/S01E01.mkv"},
        {"Type": "Episode", "Genres": [], "Path": "/data/media/tvshows/Orphan/S01E01.mkv"},  # no SeriesId
    ]
    dirs, files, counts = integrations.animated_paths(items)
    assert dirs == {"/data/media/tvshows/Phineas and Ferb"}
    assert files == {"/data/media3/tvshows/Phineas and Ferb/Season 2/S02E01"}
    assert counts == {"series": 1, "movies": 0, "episodes": 1}
    integrations._animated_dirs, integrations._animated_files = dirs, files
    try:
        assert integrations.is_animated("/data/media3/tvshows/Phineas and Ferb/Season 2/S02E01.mkv")
        assert integrations.is_animated("/data/media/tvshows/Phineas and Ferb/Season 1/S01E01.mkv")
        assert not integrations.is_animated("/data/media/tvshows/The Office/Season 1/S01E01.mkv")
        assert not integrations.is_animated("/data/media/tvshows/Orphan/S01E01.mkv")
    finally:
        integrations._animated_dirs, integrations._animated_files = set(), set()


def test_genres_case_insensitive():
    for g in (["Animation"], ["animation"], ["ANIME"], [" Anime "], ["Family", "anImAtIoN"]):
        assert integrations.is_animation(g), g
    for g in ([], None, ["Animal Planet"], ["Family"]):
        assert not integrations.is_animation(g), g


def test_paths_normalized_both_sides():
    items = [{"Type": "Series", "Id": "c", "Path": "/data/media3//tvshows/Coco Show/", "Genres": ["Animation"]},
             {"Type": "Movie", "Path": "/data/media3/movies/./Coco (2017)/Coco.mkv", "Genres": ["Animation"]},
             {"Type": "Movie", "Path": "movies/Relative/Relative.mkv", "Genres": ["Animation"]}]
    dirs, files, _ = integrations.animated_paths(items)
    assert dirs == {"/data/media3/tvshows/Coco Show"}
    assert files == {"/data/media3/movies/Coco (2017)/Coco"}   # relative paths are ignored
    integrations._animated_dirs, integrations._animated_files = dirs, files
    try:
        assert integrations.is_animated("/data/media3/tvshows/Coco Show/Season 1/e.mkv")
        assert integrations.is_animated("/data/media3/movies/Coco (2017)//Coco.mkv")
        assert not integrations.is_animated("movies/Coco (2017)/Coco.mkv")  # display paths never match
    finally:
        integrations._animated_dirs, integrations._animated_files = set(), set()


def test_off_or_unreachable(media, s, server, library):
    jf = server(library)
    s.update(detect_animation=True, jellyfin_url=jf.url, jellyfin_key="k")
    integrations.refresh_animation(s)
    ep = str(media / "tvshows" / "Adventure Time" / "S01" / "e.mkv")
    assert encoder.tier(ep) == "animation"
    # Jellyfin down: keep the previous list
    s["jellyfin_url"] = "http://127.0.0.1:9"
    assert integrations.refresh_animation(s) is None
    assert encoder.tier(ep) == "animation"
    # turned off: back to path-based tiers
    s["detect_animation"] = False
    assert integrations.refresh_animation(s) is None
    assert encoder.tier(ep) == "tv"


def test_401_logged_once_with_status_and_url(s, server, logs):
    jf = server({"error": "nope"}, status=401)
    s.update(detect_animation=True, jellyfin_url=jf.url, jellyfin_key="secret-key")
    assert integrations.refresh_animation(s) is None
    warns = [m for lvl, m in logs if lvl == "WARNING"]
    assert len(warns) == 1
    assert "genre lookup failed" in warns[0] and "HTTP 401" in warns[0] and "check the API key" in warns[0]
    assert f"{jf.url}/Items?" in warns[0] and "secret-key" not in warns[0]


def test_paths_outside_media_roots_warn(media, s, server, logs):
    jf = server({"Items": [{"Type": "Movie", "Path": "/media/movies/Coco (2017)/Coco.mkv",
                            "Genres": ["Animation"]}]})
    s.update(detect_animation=True, jellyfin_url=jf.url, jellyfin_key="k")
    assert integrations.refresh_animation(s)["outside"] == 1
    assert any("none of its 1 animated titles" in m and "/media/movies/Coco" in m for _, m in logs)


def test_refresh_libraries_auth(s, server, logs):
    jf, sonarr = server(), server({"id": 1})
    s.update(jellyfin_url=jf.url, jellyfin_key="k", sonarr_url=sonarr.url, sonarr_key="sk")
    integrations.refresh_libraries(s)
    assert jf.requests[0]["method"] == "POST" and jf.requests[0]["path"] == "/Library/Refresh"
    assert jf.requests[0]["headers"]["Authorization"] == AUTH
    assert "X-Emby-Token" not in jf.requests[0]["headers"]
    assert sonarr.requests[0]["headers"]["X-Api-Key"] == "sk"          # Sonarr/Radarr auth unchanged
    assert "Authorization" not in sonarr.requests[0]["headers"]
    assert json.loads(sonarr.requests[0]["body"]) == {"name": "RescanSeries"}
    assert ("INFO", "Refresh requested: Jellyfin") in logs


def test_refresh_failure_names_status_and_url(s, server, logs):
    jf = server(status=401)
    s.update(jellyfin_url=jf.url, jellyfin_key="secret-key")
    integrations.refresh_libraries(s)
    (msg,) = [m for lvl, m in logs if lvl == "WARNING"]
    assert msg == f"Refresh failed: Jellyfin: POST {jf.url}/Library/Refresh -> HTTP 401 Unauthorized (check the API key)"


def test_session_check_uses_header_and_logs_once(s, server, logs):
    jf = server([{"NowPlayingItem": {"Name": "x"}, "TranscodingInfo": {"IsVideoDirect": False}}])
    s.update(jellyfin_url=jf.url, jellyfin_key="k")
    assert integrations.jellyfin_transcoding(s) is True
    assert jf.requests[0]["headers"]["Authorization"] == AUTH
    jf.status = 500
    assert integrations.jellyfin_transcoding(s) is False
    assert integrations.jellyfin_transcoding(s) is False
    assert len([m for lvl, m in logs if lvl == "WARNING"]) == 1      # not every 30 s
    jf.status = 200
    assert integrations.jellyfin_transcoding(s) is True
    assert any("works again" in m for _, m in logs)


def test_safe_url_masks_secrets():
    assert integrations.safe_url("http://h/Items?api_key=abc&x=1") == "http://h/Items?api_key=***&x=1"
    assert integrations.safe_url("http://u:p@h:9091/rpc") == "http://***@h:9091/rpc"
