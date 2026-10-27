"""Walk the selected folders and update the file index."""
import logging
import os
import time
from collections import Counter

from . import config, media, paths

log = logging.getLogger("reencoder")

STICKY = {"done", "no_savings"}  # kept as long as the file is unchanged


def iter_videos(folders, excludes, roots=None):
    """Yield real paths of video files under `folders`, skipping excludes, dot-dirs and symlinks."""
    ex = []
    for e in excludes:
        try:
            ex.append(paths.safe_path(e, roots))
        except paths.PathError:
            pass
    seen = set()
    for folder in folders:
        try:
            folder = paths.safe_path(folder, roots)
        except paths.PathError as e:
            log.warning(f"Skipping folder: {e}")
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and not any(os.path.join(dirpath, d) == x or
                                             os.path.join(dirpath, d).startswith(x + os.sep) for x in ex))
            for name in filenames:
                if name.startswith(".") or os.path.splitext(name)[1].lower() not in config.VIDEO_EXTS:
                    continue
                p = os.path.join(dirpath, name)
                if p in seen or os.path.islink(p) or any(p.startswith(x + os.sep) for x in ex):
                    continue
                seen.add(p)
                yield p


def evaluate(path, st, old, s, now=None, probe=media.probe):
    """Return the new index row for one file. Re-probes only when size or mtime changed."""
    changed = old is None or old["size"] != st.st_size or old["mtime"] != st.st_mtime
    row = {"path": path, "size": st.st_size, "mtime": st.st_mtime,
           "failures": 0 if changed else old["failures"],
           "probe": None if changed else old["probe"], "status": "todo", "reason": ""}
    if not changed and old["status"] in STICKY:
        row.update(status=old["status"], reason=old["reason"])
        return row
    reason = media.stat_skip(st, s, now)
    if reason:
        row.update(status="skipped", reason=reason)
        return row
    if row["probe"] is None:
        try:
            row["probe"] = probe(path)
        except media.ProbeError as e:
            row.update(status="skipped", reason=f"ffprobe failed: {e}"[:300])
            return row
    status, reason = media.classify(path, row["probe"], s)
    if status == "todo" and row["failures"] >= s["max_failures"]:
        status, reason = "failed", (old or {}).get("reason") or f"failed {row['failures']} times"
    row.update(status=status, reason=reason)
    return row


def scan(db, s, running=(), roots=None, probe=media.probe):
    """Full rescan. Returns a summary dict. `running` paths are left alone."""
    t0 = time.time()
    old = db.all_files()
    rows, seen = [], set()
    for p in iter_videos(s["folders"], s["excludes"], roots):
        seen.add(p)
        if p in running:
            continue
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append(evaluate(p, st, old.get(p), s, probe=probe))
        if len(rows) >= 200:
            db.put_files(rows)
            rows = []
    db.put_files(rows)
    gone = [p for p in old if p not in seen and p not in running]
    db.delete_files(gone)
    reasons = Counter(r["reason"].split(" (")[0].split(":")[0] for r in db.q(
        "SELECT reason FROM files WHERE status = 'skipped'"))
    return {"seconds": time.time() - t0, "counts": db.counts(), "skip_reasons": dict(reasons),
            "removed": len(gone)}
