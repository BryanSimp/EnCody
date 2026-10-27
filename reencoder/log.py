"""Plain-line logging to stdout, a rolling file in /config/logs, and a buffer for the UI."""
import logging
import logging.handlers
import os
from collections import deque

from . import config

FORMAT = logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S")
recent = deque(maxlen=500)


class _Buffer(logging.Handler):
    def emit(self, record):
        recent.append(self.format(record))


def setup():
    log = logging.getLogger("reencoder")
    if log.handlers:
        return log
    log.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    handlers = [logging.StreamHandler(), _Buffer()]
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            os.path.join(config.LOG_DIR, "reencoder.log"), maxBytes=5_000_000, backupCount=3))
    except OSError as e:
        print(f"Cannot write log file: {e}")
    for h in handlers:
        h.setFormatter(FORMAT)
        log.addHandler(h)
    log.propagate = False
    return log


def human_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.2f}TB" if abs(n) < 1024 else f"{n / 1024:.2f}PB"


def human_time(sec):
    if sec is None:
        return "unknown"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m = rem // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{sec}s"
