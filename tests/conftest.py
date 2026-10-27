import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from reencoder import config  # noqa: E402
from reencoder.settings import DEFAULTS  # noqa: E402


@pytest.fixture
def media(tmp_path, monkeypatch):
    """A fake /data/media root inside tmp."""
    root = tmp_path / "data" / "media"
    (root / "movies").mkdir(parents=True)
    (root / "tvshows" / "Show").mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT_GLOB", str(tmp_path / "data" / "media*"))
    return root


@pytest.fixture
def s():
    return dict(DEFAULTS)


def have_encoder(name):
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return f" {name} " in out


needs_svt = pytest.mark.skipif(not have_encoder("libsvtav1"), reason="ffmpeg with libsvtav1 not available")


def make_clip(path, seconds=6, size="640x360"):
    """h264 + 2 audio + 1 subtitle test clip."""
    srt = str(path) + ".srt"
    with open(srt, "w") as f:
        f.write("1\n00:00:00,500 --> 00:00:02,000\nhello\n")
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}",
        "-i", srt,
        "-map", "0", "-map", "1", "-map", "2", "-map", "3",
        "-c:v", "libx264", "-preset", "ultrafast", "-qp", "0",
        "-c:a", "aac", "-c:s", "mov_text" if str(path).endswith(".mp4") else "srt", str(path)], check=True)
    os.remove(srt)
    return path
