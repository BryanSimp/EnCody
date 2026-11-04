"""Download cleanup: remove finished torrents from Transmission once Sonarr/Radarr imported them.

A torrent is removed only when ALL of these hold:
  - Transmission has all of it (nothing left to download, not verifying);
  - it finished at least `cleanup_min_seed_hours` ago;
  - Sonarr or Radarr grabbed it, and their history has an import from this download for every
    episode/movie it was grabbed for (the same rule Sonarr/Radarr use for "fully imported");
  - every imported file exists as a regular, non-empty file on one of Encody's media drives
    (/data/media*). If the imported path is gone, the episode's/movie's current file is accepted
    instead (Sonarr/Radarr upgraded or moved it, or Encody renamed .avi -> .mkv).

Transmission deletes the data itself (torrent-remove with delete-local-data), so Encody never
deletes anything in the downloads folder and does not need it mounted.
"""
import base64
import json
import logging
import os
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from . import paths
from .integrations import CallError, call_error
from .log import human_bytes as hb, human_time as ht

log = logging.getLogger("reencoder")

TR_FIELDS = ["hashString", "name", "status", "percentDone", "leftUntilDone", "sizeWhenDone",
             "doneDate", "addedDate"]
TR_CHECKING = {1, 2}  # check pending, checking
GRABBED = {"grabbed", "1"}
IMPORTED = {"downloadfolderimported", "3"}
KEPT_LABELS = {"seeding": "min seed time", "not_imported": "not fully imported yet",
               "unknown": "not from Sonarr/Radarr", "missing": "not found on a drive", "error": "errors"}


def rpc_url(url):
    """Accept http://host:9091, .../transmission, .../transmission/web or the full .../rpc URL."""
    url = url.strip().rstrip("/")
    if url.endswith("/rpc"):
        return url
    if url.endswith("/web"):
        url = url[:-4]
    return url + ("/rpc" if url.endswith("/transmission") else "/transmission/rpc")


class Transmission:
    """Minimal Transmission RPC client: session-id handshake and optional basic auth."""

    def __init__(self, url, user="", password="", timeout=30):
        self.url = rpc_url(url)
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()} \
            if user or password else {}
        self.session = ""
        self.timeout = timeout

    def call(self, method, arguments):
        body = json.dumps({"method": method, "arguments": arguments}).encode()
        for _ in range(3):
            req = urllib.request.Request(self.url, data=body, method="POST", headers={
                "Content-Type": "application/json", "X-Transmission-Session-Id": self.session, **self.auth})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    out = json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 409 and e.headers.get("X-Transmission-Session-Id"):
                    self.session = e.headers["X-Transmission-Session-Id"]
                    continue
                raise call_error("POST", self.url, e) from None
            except (urllib.error.URLError, OSError) as e:
                raise call_error("POST", self.url, e) from None
            if out.get("result") != "success":
                raise RuntimeError(f"Transmission {method}: {out.get('result')}")
            return out.get("arguments") or {}
        raise RuntimeError("Transmission kept rejecting the session id")

    def torrents(self):
        return self.call("torrent-get", {"fields": TR_FIELDS}).get("torrents", [])

    def remove(self, hash_string):
        self.call("torrent-remove", {"ids": [hash_string], "delete-local-data": True})


class Arr:
    """Sonarr (item_key episodeId) or Radarr (item_key movieId), API v3."""

    def __init__(self, name, url, key, item_key, timeout=30):
        self.name, self.url, self.key, self.item_key = name, url.rstrip("/"), key, item_key
        self.items = "episodes" if item_key == "episodeId" else "movies"
        self.timeout = timeout

    def get(self, path, **params):
        q = "?" + urllib.parse.urlencode(params) if params else ""
        url = self.url + path + q
        req = urllib.request.Request(url, headers={"X-Api-Key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except (urllib.error.URLError, OSError) as e:
            raise call_error("GET", url, e) from None
        return json.loads(raw) if raw else None

    def history(self, download_id):
        """History records of one download. Filtered here as well, in case the server ignores the filter."""
        data = self.get("/api/v3/history", downloadId=download_id, pageSize=1000) or {}
        recs = data.get("records", []) if isinstance(data, dict) else data
        return [r for r in recs if str(r.get("downloadId") or "").upper() == download_id]

    def current_file(self, item_id):
        """Path of the episode's/movie's current file according to Sonarr/Radarr, or None."""
        if item_id is None:
            return None
        if self.item_key == "episodeId":
            ep = self.get(f"/api/v3/episode/{int(item_id)}") or {}
            if not ep.get("hasFile") or not ep.get("episodeFileId"):
                return None
            f = ep.get("episodeFile") or self.get(f"/api/v3/episodefile/{int(ep['episodeFileId'])}") or {}
        else:
            m = self.get(f"/api/v3/movie/{int(item_id)}") or {}
            if not m.get("hasFile"):
                return None
            f = m.get("movieFile") or {}
        return f.get("path")


def make_arrs(s):
    arrs = []
    if s["sonarr_url"] and s["sonarr_key"]:
        arrs.append(Arr("Sonarr", s["sonarr_url"], s["sonarr_key"], "episodeId"))
    if s["radarr_url"] and s["radarr_key"]:
        arrs.append(Arr("Radarr", s["radarr_url"], s["radarr_key"], "movieId"))
    return arrs


def map_path(path, mappings):
    """Sonarr/Radarr path -> Encody path, using the longest matching 'remote => local' prefix."""
    best = None
    for m in mappings:
        remote, _, local = m.partition("=>")
        remote, local = remote.strip().rstrip("/"), local.strip().rstrip("/")
        if (path == remote or path.startswith(remote + "/")) and (best is None or len(remote) > len(best[0])):
            best = (remote, local)
    return best[1] + path[len(best[0]):] if best else path


def on_drive(local):
    """The file, or Encody's .mkv rename of it, as a regular non-empty file (not a symlink) on a media drive."""
    for p in dict.fromkeys((local, os.path.splitext(local)[0] + ".mkv")):
        if paths.root_of(p) is None:
            continue
        try:
            st = os.lstat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_size > 0:
            return p
    return None


def _event(rec):
    return str(rec.get("eventType", "")).lower()


def verify_import(arr, rec, s):
    """Find the file one import record put in the library. Returns (path, None) or (None, why)."""
    data = {str(k).lower(): v for k, v in (rec.get("data") or {}).items()}
    imported, dropped = data.get("importedpath") or "", data.get("droppedpath") or ""
    if not imported:
        return None, f"{arr.name} history has no imported path"
    if dropped and (imported == dropped or imported.startswith(os.path.dirname(dropped) + "/")):
        return None, f"library file {imported} is inside the download itself"
    local = map_path(imported, s["cleanup_path_map"])
    if paths.root_of(local) is None:
        return None, f"{imported} is not under Encody's media drives (add a path mapping)"
    found = on_drive(local)
    if found:
        return found, None
    try:
        current = arr.current_file(rec.get(arr.item_key))
    except Exception as e:
        return None, f"{paths.display(local)} is gone and {arr.name} could not be asked for the current file: {e}"
    found = current and on_drive(map_path(current, s["cleanup_path_map"]))
    if found:
        return found, None
    return None, f"{paths.display(local)} is not on the drive and {arr.name} has no other file for it"


def check(t, arrs, s, now):
    """Decide one finished torrent. Returns (remove, kind, reason, library files)."""
    finished = t.get("doneDate") or t.get("addedDate") or 0
    age = now - finished
    if age < s["cleanup_min_seed_hours"] * 3600:
        return False, "seeding", f"finished {ht(age)} ago, keeping it at least {s['cleanup_min_seed_hours']}h", []
    h = t["hashString"].upper()
    found = []
    for arr in arrs:
        try:
            recs = arr.history(h)
        except Exception as e:
            return False, "error", f"could not ask {arr.name}: {e}", []
        if recs:
            found.append((arr, recs))
    if not found:
        return False, "unknown", "not in Sonarr/Radarr history (added by hand, or its history is gone)", []
    files = []
    for arr, recs in found:
        grabbed = {r.get(arr.item_key) for r in recs if _event(r) in GRABBED}
        imports = [r for r in recs if _event(r) in IMPORTED]
        if not grabbed:
            return False, "unknown", f"{arr.name} never grabbed it (added by hand?)", []
        missing = grabbed - {r.get(arr.item_key) for r in imports}
        if missing:
            return False, "not_imported", (f"{arr.name} imported {len(grabbed) - len(missing)} of {len(grabbed)} "
                                           f"{arr.items} (check its Activity queue)"), []
        for r in imports:
            path, why = verify_import(arr, r, s)
            if not path:
                return False, "missing", why, []
            files.append(path)
    files = sorted(set(files))
    more = f" (+{len(files) - 1} more)" if len(files) > 1 else ""
    names = " + ".join(a.name for a, _ in found)
    return True, "ok", f"imported by {names}, on the drive: {paths.display(files[0])}{more}", files


def run(s, db, tr=None, arrs=None, now=None, manual=False):
    """One cleanup pass. Returns the report shown in the UI."""
    now = now or time.time()
    dry = s["cleanup_dry_run"]
    tr = tr or Transmission(s["transmission_url"], s["transmission_user"], s["transmission_pass"])
    arrs = make_arrs(s) if arrs is None else arrs
    if not arrs:
        raise RuntimeError("needs the Sonarr and/or Radarr URL + API key (Settings > Integrations)")
    try:
        torrents = tr.torrents()
    except Exception as e:
        where = "" if isinstance(e, CallError) else f" at {tr.url}"  # CallError already names the URL
        raise RuntimeError(f"could not reach Transmission{where}: {e}")
    rows, kept, removed, freed, downloading, first_error = [], Counter(), 0, 0, 0, ""
    for t in sorted(torrents, key=lambda t: t.get("name", "").lower()):
        if t.get("leftUntilDone", 1) > 0 or t.get("percentDone", 0) < 1 or t.get("status") in TR_CHECKING:
            downloading += 1
            continue
        ok, kind, reason, files = check(t, arrs, s, now)
        size = t.get("sizeWhenDone") or 0
        row = {"name": t["name"], "size": size, "finished": t.get("doneDate") or t.get("addedDate") or None,
               "reason": reason}
        if not ok:
            row["action"] = "kept"
            kept[kind] += 1
            first_error = first_error or (reason if kind == "error" else "")
            log.debug(f"CLEANUP keep {t['name']}: {reason}")
        elif dry:
            row["action"] = "would remove"
            removed += 1
            freed += size
            (log.info if manual else log.debug)(f"CLEANUP DRY RUN would remove {t['name']} ({hb(size)}) | {reason}")
        else:
            try:
                tr.remove(t["hashString"])
            except Exception as e:
                row.update(action="error", reason=f"remove failed: {e}")
                kept["error"] += 1
                log.warning(f"CLEANUP could not remove {t['name']}: {e}")
            else:
                row["action"] = "removed"
                removed += 1
                freed += size
                db.add_history(path=t["name"], new_path=files[0], started=now, finished=time.time(),
                               orig_size=size, new_size=0, saved=size, media_sec=0, result="cleaned",
                               message=f"removed from Transmission with its data | {reason}")
                log.info(f"CLEANUP removed {t['name']} ({hb(size)}) from Transmission with its data | {reason}")
        rows.append(row)

    why = ", ".join(f"{KEPT_LABELS.get(k, k)}: {n}" for k, n in kept.most_common())
    tail = (f" | kept {sum(kept.values())}" + (f" ({why})" if why else "")
            + (f" | {downloading} still downloading" if downloading else ""))
    if dry:
        summary = (f"CLEANUP DRY RUN: {removed} torrent(s) would be removed, {hb(freed)}{tail}. "
                   "Nothing was removed; see the Downloads tab.")
    else:
        summary = f"CLEANUP done: removed {removed} torrent(s), freed {hb(freed)}{tail}"
    log.info(summary)
    if first_error:
        log.warning(f"CLEANUP: {first_error}")
    return {"finished": time.time(), "dry_run": dry, "rows": rows, "summary": summary, "error": ""}


class Cleaner:
    """Runs the cleanup every N hours (when on) or when "Clean up now" is pressed, in its own thread."""

    def __init__(self, db):
        self.db = db
        self.running = False
        self.requested = False
        self.last_run = 0.0
        self.report = None

    def due(self, s, now=None):
        if self.running or not s["transmission_url"]:
            return False
        if self.requested:
            return True
        return s["cleanup_enabled"] and (now or time.time()) - self.last_run >= s["cleanup_interval_hours"] * 3600

    def maybe_start(self, s):
        if not self.due(s):
            return
        manual, self.requested, self.running = self.requested, False, True
        threading.Thread(target=self._run, args=(dict(s), manual), name="cleanup", daemon=True).start()

    def _run(self, s, manual):
        try:
            self.report = run(s, self.db, manual=manual)
        except Exception as e:
            log.warning(f"CLEANUP failed: {e}")
            self.report = {"finished": time.time(), "dry_run": s["cleanup_dry_run"], "rows": [],
                           "summary": f"CLEANUP failed: {e}", "error": str(e)}
        finally:
            self.last_run = time.time()
            self.running = False

    def status(self, s):
        r = self.report or {}
        nxt = None
        if s["cleanup_enabled"] and s["transmission_url"]:
            nxt = self.last_run + s["cleanup_interval_hours"] * 3600 if self.last_run else time.time()
        return {"configured": bool(s["transmission_url"]), "enabled": s["cleanup_enabled"],
                "dry_run": s["cleanup_dry_run"], "running": self.running or self.requested,
                "last_run": r.get("finished"), "next_run": nxt, "summary": r.get("summary", ""),
                "error": r.get("error", ""), "freed_total": self.db.total_cleaned()}
