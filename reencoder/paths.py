"""Path validation: nothing outside the allowed media roots is ever touched."""
import os

from . import config


class PathError(ValueError):
    pass


def root_of(path, roots=None):
    """Return the allowed root containing `path` (resolved), or None."""
    real = os.path.realpath(path)
    for root in roots if roots is not None else config.media_roots():
        if real == root or real.startswith(root + os.sep):
            return root
    return None


def safe_path(path, roots=None):
    """Resolve `path` and make sure it lies inside an allowed root. Raises PathError."""
    if not path or "\x00" in path:
        raise PathError("empty or invalid path")
    real = os.path.realpath(path)
    if root_of(real, roots) is None:
        raise PathError(f"path is outside the allowed media folders: {path}")
    return real


def display(path, roots=None):
    """Short path for logs: relative to its media root."""
    root = root_of(path, roots)
    return os.path.relpath(path, root) if root else path


def is_temp(name):
    return name.startswith(".") and config.TMP_MARK in name


def temp_path(final_path):
    """Temp output next to the final file: .<name>.reencode.tmp.<ext>"""
    d, name = os.path.split(final_path)
    stem, ext = os.path.splitext(name)
    return os.path.join(d, f".{stem}{config.TMP_MARK}{ext.lstrip('.')}")
