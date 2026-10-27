import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reencoder import encoder, integrations


@pytest.fixture
def jellyfin(media):
    items = {"Items": [
        {"Type": "Series", "Path": str(media / "tvshows" / "Adventure Time"), "Genres": ["Animation", "Comedy"]},
        {"Type": "Series", "Path": str(media / "tvshows" / "Riviera"), "Genres": ["Drama"]},
        {"Type": "Movie", "Path": str(media / "movies" / "Shrek (2001)" / "Shrek.avi"), "Genres": ["Animation"]},
        {"Type": "Movie", "Path": str(media / "movies" / "Heat (1995)" / "Heat.mkv"), "Genres": ["Crime"]},
        {"Type": "Series", "Path": str(media / "tvshows" / "Frieren"), "Genres": ["Anime"]},
    ]}
    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["token"] = self.headers.get("X-Emby-Token")
            body = json.dumps(items).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    integrations._animated_dirs, integrations._animated_files = set(), set()


def test_three_tiers(media, s, jellyfin):
    url, seen = jellyfin
    s.update(detect_animation=True, jellyfin_url=url, jellyfin_key="k",
             quality=22, quality_tv=24, quality_animation=28)
    assert integrations.refresh_animation(s) == 3
    assert seen["token"] == "k"
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


def test_off_or_unreachable(media, s, jellyfin):
    url, _ = jellyfin
    s.update(detect_animation=True, jellyfin_url=url, jellyfin_key="k")
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
