"""Optional Sonarr/Radarr/Jellyfin calls. All off by default."""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("reencoder")

_SECRET_PARAM = re.compile(r"(?i)([?&](?:api_?key|apikey|token|access_token|x-emby-token)=)[^&#]*")


class CallError(Exception):
    """An integration HTTP call failed. The message has the method, URL and HTTP status, never a key."""

    def __init__(self, method, url, status=None, reason=""):
        self.method, self.url, self.status, self.reason = method, safe_url(url), status, str(reason)
        if status:
            what = f"HTTP {status}" + (f" {self.reason}" if self.reason else "")
            if status in (401, 403):
                what += " (check the API key)"
        else:
            what = self.reason or "failed"
        super().__init__(f"{method} {self.url} -> {what}")


def safe_url(url):
    """URL for logs: secrets in the query string and user:password@ are masked."""
    url = _SECRET_PARAM.sub(r"\1***", url)
    parts = urllib.parse.urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        url = urllib.parse.urlunsplit(parts._replace(netloc="***@" + host))
    return url


def call_error(method, url, e):
    """Turn a urllib/socket exception into a CallError."""
    if isinstance(e, urllib.error.HTTPError):
        return CallError(method, url, e.code, e.reason)
    if isinstance(e, urllib.error.URLError):
        return CallError(method, url, reason=str(e.reason))
    return CallError(method, url, reason=str(e) or type(e).__name__)


def _call(method, url, headers, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except (urllib.error.URLError, OSError) as e:  # HTTPError is a URLError; timeouts are OSError
        raise call_error(method, url, e) from None
    try:
        return json.loads(raw) if raw else None
    except ValueError as e:
        raise CallError(method, url, reason=f"response is not JSON: {e}") from None


def jellyfin_headers(s):
    """Jellyfin auth. Newer servers reject X-Emby-Token and ?api_key=, so only this header is sent."""
    return {"Authorization": f'MediaBrowser Token="{s["jellyfin_key"]}"'}


def refresh_libraries(s):
    """Ask Sonarr, Radarr and Jellyfin to rescan. Errors are logged, never raised."""
    jobs = [
        ("Sonarr", s["sonarr_url"], s["sonarr_key"], "/api/v3/command", {"name": "RescanSeries"},
         {"X-Api-Key": s["sonarr_key"]}),
        ("Radarr", s["radarr_url"], s["radarr_key"], "/api/v3/command", {"name": "RescanMovie"},
         {"X-Api-Key": s["radarr_key"]}),
        ("Jellyfin", s["jellyfin_url"], s["jellyfin_key"], "/Library/Refresh", None, jellyfin_headers(s)),
    ]
    for name, url, key, path, body, headers in jobs:
        if not (url and key):
            continue
        try:
            _call("POST", url.rstrip("/") + path, headers, body)
            log.info(f"Refresh requested: {name}")
        except CallError as e:
            log.warning(f"Refresh failed: {name}: {e}")


_session_check_failed = False


def jellyfin_transcoding(s):
    """True if Jellyfin reports an active transcode session. A failing check is logged once, not every 30 s."""
    global _session_check_failed
    if not (s["jellyfin_url"] and s["jellyfin_key"]):
        return False
    try:
        sessions = _call("GET", s["jellyfin_url"].rstrip("/") + "/Sessions", jellyfin_headers(s)) or []
    except CallError as e:
        if not _session_check_failed:
            log.warning(f"Jellyfin session check failed (assuming no transcode until it works): {e}")
        _session_check_failed = True
        return False
    if _session_check_failed:
        log.info("Jellyfin session check works again.")
        _session_check_failed = False
    # Only video transcodes use the GPU; remux/direct stream sessions do not.
    return any(x.get("NowPlayingItem") and x.get("TranscodingInfo")
               and not x["TranscodingInfo"].get("IsVideoDirect") for x in sessions)


# ---- animation detection -----------------------------------------------------------------
ANIMATION_GENRES = {"animation", "anime"}
JELLYFIN_ITEMS = ("/Items?Recursive=true&IncludeItemTypes=Movie,Series,Episode&Fields=Path,Genres"
                  "&EnableImages=false&EnableUserData=false&EnableTotalRecordCount=false")
_animated_dirs = set()   # series folders Jellyfin tags as animation
_animated_files = set()  # movie/episode paths without extension (survives .avi -> .mkv)


def norm_path(path):
    """Absolute, normalized path for comparing Jellyfin paths with Encody's ('' if unusable)."""
    if not path or not os.path.isabs(path):
        return ""
    return os.path.normpath(path)


def is_animation(genres):
    return bool(ANIMATION_GENRES & {str(g).strip().lower() for g in genres or []})


def animated_paths(items):
    """Split Jellyfin items into animated series folders and animated movie/episode files.

    Genres live on the Series; Episode items usually have none, so an episode counts as animated
    when its SeriesId belongs to an animated series (this also covers seasons stored outside the
    series folder Jellyfin reports). Returns (dirs, file stems, counts)."""
    animated = [it for it in items if it.get("Type") == "Series" and is_animation(it.get("Genres"))]
    series = {it["Id"] for it in animated if it.get("Id")}
    dirs, files = set(), set()
    counts = {"series": len(animated), "movies": 0, "episodes": 0}
    for it in animated:
        path = norm_path(it.get("Path"))
        if path:
            dirs.add(path)
    for it in items:
        kind, path = it.get("Type"), norm_path(it.get("Path"))
        if not path:
            continue
        if kind == "Episode":
            if (it.get("SeriesId") and it["SeriesId"] in series) or is_animation(it.get("Genres")):
                files.add(os.path.splitext(path)[0])
                counts["episodes"] += 1
        elif kind == "Movie" and is_animation(it.get("Genres")):
            files.add(os.path.splitext(path)[0])
            counts["movies"] += 1
    return dirs, files, counts


def _under(path, roots):
    return any(path == r or path.startswith(r + os.sep) for r in roots)


def refresh_animation(s):
    """Load the animated series/movies/episodes from Jellyfin (one call per scan).

    Returns counts {"series", "movies", "episodes", "outside"}, or None if off or failed. On failure the
    previous list is kept and one warning is logged."""
    global _animated_dirs, _animated_files
    if not (s["detect_animation"] and s["jellyfin_url"] and s["jellyfin_key"]):
        _animated_dirs, _animated_files = set(), set()
        return None
    try:
        data = _call("GET", s["jellyfin_url"].rstrip("/") + JELLYFIN_ITEMS, jellyfin_headers(s),
                     timeout=120) or {}
    except CallError as e:
        log.warning(f"Jellyfin genre lookup failed, keeping previous list "
                    f"({len(_animated_dirs)} series folders, {len(_animated_files)} files): {e}")
        return None
    dirs, files, counts = animated_paths(data.get("Items") or [])
    roots = config.media_roots()
    outside = [p for p in sorted(dirs | files) if not _under(p, roots)]
    counts["outside"] = len(outside)
    if outside and len(outside) == len(dirs | files):
        log.warning(f"Jellyfin: none of its {len(outside)} animated titles are under Encody's media folders "
                    f"({', '.join(roots) or 'none'}); e.g. Jellyfin says {outside[0]}. Mount the media at the "
                    "same paths in both containers, or animated titles get the movie/TV quality.")
    _animated_dirs, _animated_files = dirs, files
    return counts


def is_animated(path):
    path = norm_path(path)
    if not path:
        return False
    if os.path.splitext(path)[0] in _animated_files:
        return True
    d = os.path.dirname(path)
    while d and d != os.sep:
        if d in _animated_dirs:
            return True
        d = os.path.dirname(d)
    return False
