"""Optional Sonarr/Radarr/Jellyfin calls. All off by default."""
import json
import logging
import urllib.request

log = logging.getLogger("reencoder")


def _call(method, url, headers, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def refresh_libraries(s):
    """Ask Sonarr, Radarr and Jellyfin to rescan. Errors are logged, never raised."""
    jobs = [
        ("Sonarr", s["sonarr_url"], s["sonarr_key"], "/api/v3/command", {"name": "RescanSeries"}, "X-Api-Key"),
        ("Radarr", s["radarr_url"], s["radarr_key"], "/api/v3/command", {"name": "RescanMovie"}, "X-Api-Key"),
        ("Jellyfin", s["jellyfin_url"], s["jellyfin_key"], "/Library/Refresh", None, "X-Emby-Token"),
    ]
    for name, url, key, path, body, header in jobs:
        if not (url and key):
            continue
        try:
            _call("POST", url.rstrip("/") + path, {header: key}, body)
            log.info(f"Refresh requested: {name}")
        except Exception as e:
            log.warning(f"Refresh failed: {name}: {e}")


def jellyfin_transcoding(s):
    """True if Jellyfin reports an active transcode session."""
    if not (s["jellyfin_url"] and s["jellyfin_key"]):
        return False
    try:
        sessions = _call("GET", s["jellyfin_url"].rstrip("/") + "/Sessions",
                         {"X-Emby-Token": s["jellyfin_key"]}) or []
    except Exception as e:
        log.debug(f"Jellyfin session check failed: {e}")
        return False
    # Only video transcodes use the GPU; remux/direct stream sessions do not.
    return any(x.get("NowPlayingItem") and x.get("TranscodingInfo")
               and not x["TranscodingInfo"].get("IsVideoDirect") for x in sessions)
