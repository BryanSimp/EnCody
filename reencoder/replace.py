"""Safe replace, trash and crash recovery. The original is removed only after the
verified replacement exists at its final path."""
import datetime
import logging
import os
import shutil

from . import config, paths

log = logging.getLogger("reencoder")


def trash_path(original, root, today=None):
    day = (today or datetime.date.today()).isoformat()
    return os.path.join(root, config.TRASH_DIR, day, os.path.relpath(original, root))


def copy_perms(src_st, dst):
    try:
        os.chmod(dst, src_st.st_mode & 0o7777)
        os.chown(dst, src_st.st_uid, src_st.st_gid)
    except OSError:
        pass  # not root / different owner: keep what we have


def replace(original, tmp, final, s, roots=None):
    """Swap the verified temp file into place. Returns a short note for the log."""
    original = paths.safe_path(original, roots)
    final = paths.safe_path(final, roots)
    root = paths.root_of(original, roots)
    keep = not s["delete_original"] or s["trash_days"] > 0
    copy_perms(os.stat(original), tmp)

    if final != original and os.path.exists(final):
        raise FileExistsError(f"target already exists: {final}")

    note = ""
    if keep:
        tp = trash_path(original, root)
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        if os.path.exists(tp):
            os.remove(tp)
        os.link(original, tp)  # same drive: instant, no extra space until the original is unlinked
        note = f"original kept in {config.TRASH_DIR}"
    os.replace(tmp, final)  # atomic; when final == original this also drops the old file
    if final != original:
        os.remove(original)
    return note or "original deleted"


def purge_trash(days, roots=None):
    """Delete trash day-folders older than `days`. days == 0 with delete_original off keeps forever."""
    if days <= 0:
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=days)
    for root in roots if roots is not None else config.media_roots():
        tdir = os.path.join(root, config.TRASH_DIR)
        if not os.path.isdir(tdir):
            continue
        for name in os.listdir(tdir):
            try:
                if datetime.date.fromisoformat(name) < cutoff:
                    shutil.rmtree(os.path.join(tdir, name))
                    log.info(f"Trash: removed {root}/{config.TRASH_DIR}/{name}")
            except ValueError:
                pass


def remove_temp(tmp):
    try:
        os.remove(tmp)
    except FileNotFoundError:
        pass


def clean_stale_temps(folders, roots=None):
    """Delete leftover .*.reencode.tmp.* files under the selected folders. Returns the paths removed."""
    removed = []
    for folder in folders:
        try:
            folder = paths.safe_path(folder, roots)
        except paths.PathError:
            continue
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [d for d in dirnames if d != config.TRASH_DIR]
            for name in filenames:
                if paths.is_temp(name):
                    p = os.path.join(dirpath, name)
                    try:
                        os.remove(p)
                        removed.append(p)
                    except OSError as e:
                        log.warning(f"Could not remove stale temp {p}: {e}")
    return removed


def free_space(path):
    return shutil.disk_usage(os.path.dirname(path)).free


