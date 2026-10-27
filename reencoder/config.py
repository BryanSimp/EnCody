"""Process-wide configuration from environment variables."""
import glob
import os

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
DB_PATH = os.path.join(CONFIG_DIR, "reencoder.db")
LOG_DIR = os.path.join(CONFIG_DIR, "logs")

# Allowed media roots. Everything the app reads or writes must live under one of these.
ROOT_GLOB = os.environ.get("MEDIA_ROOT_GLOB", "/data/media*")

UI_USER = os.environ.get("UI_USER", "")
UI_PASS = os.environ.get("UI_PASS", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
PORT = int(os.environ.get("PORT", "8080"))

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".wmv", ".webm"}
TMP_MARK = ".reencode.tmp."
TRASH_DIR = ".reencoder-trash"


def media_roots():
    """Existing allowed root directories, resolved."""
    return sorted(os.path.realpath(p) for p in glob.glob(ROOT_GLOB) if os.path.isdir(p))
