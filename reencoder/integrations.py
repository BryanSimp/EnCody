"""Optional Sonarr/Radarr/Jellyfin calls. All off by default."""
import json
import logging
import os
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


ANIMATION_GENRES = {"animation", "anime"}
_animated_dirs = set()   # series folders Jellyfin tags as animation
_animated_files = set()  # movie paths without extension (survives .avi -> .mkv)


def refresh_animation(s):
    """Load the animated series/movies from Jellyfin. Returns how many, or None if off or failed
    (on failure the previous list is kept)."""
    global _animated_dirs, _animated_files
    if not (s["detect_animation"] and s["jellyfin_url"] and s["jellyfin_key"]):
        _animated_dirs, _animated_files = set(), set()
        return None
    try:
        data = _call("GET", s["jellyfin_url"].rstrip("/") +
                     "/Items?Recursive=true&IncludeItemTypes=Movie,Series&Fields=Path,Genres",
                     {"X-Emby-Token": s["jellyfin_key"]}, timeout=60) or {}
    except Exception as e:
        log.warning(f"Jellyfin genre lookup failed, keeping previous list: {e}")
        return None
    dirs, files = set(), set()
    for item in data.get("Items", []):
        path = item.get("Path")
        if not path or not ANIMATION_GENRES & {g.lower() for g in item.get("Genres") or []}:
            continue
        if item.get("Type") == "Series":
            dirs.add(path.rstrip("/"))
        else:
            files.add(os.path.splitext(path)[0])
    _animated_dirs, _animated_files = dirs, files
    return len(dirs) + len(files)


def is_animated(path):
    if os.path.splitext(path)[0] in _animated_files:
        return True
    d = os.path.dirname(path)
    while d and d != os.sep:
        if d in _animated_dirs:
            return True
        d = os.path.dirname(d)
    return False
