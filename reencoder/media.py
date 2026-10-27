"""ffprobe wrapper and the "does this file need encoding?" decision."""
import json
import os
import subprocess
import time

from . import config
from .settings import CODECS

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
DV_TAGS = {"dvh1", "dvhe", "dav1", "dva1"}


class ProbeError(Exception):
    pass


def ffprobe(path, timeout=120):
    try:
        r = subprocess.run(
            [config.FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ProbeError(str(e))
    if r.returncode != 0:
        raise ProbeError(" ".join(r.stderr.split())[-300:] or f"ffprobe exit {r.returncode}")
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        raise ProbeError(f"bad ffprobe output: {e}")


def _duration(data):
    """Container duration, falling back to stream durations or MKV DURATION tags."""
    vals = [data.get("format", {}).get("duration")]
    for s in data.get("streams", []):
        vals.append(s.get("duration"))
        tag = (s.get("tags") or {}).get("DURATION")
        if tag and ":" in tag:
            h, m, sec = tag.split(":")
            vals.append(int(h) * 3600 + int(m) * 60 + float(sec))
    for v in vals:
        try:
            if v is not None and float(v) > 0:
                return float(v)
        except ValueError:
            pass
    return 0.0


def main_video_index(streams):
    """Index among video streams of the first real video (not cover art)."""
    vids = [s for s in streams if s.get("codec_type") == "video"]
    for i, s in enumerate(vids):
        if not (s.get("disposition") or {}).get("attached_pic"):
            return i
    return None


def summarize(data):
    """Reduce ffprobe JSON to the fields we store and decide on."""
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    vids = [s for s in streams if s.get("codec_type") == "video"]
    vi = main_video_index(streams)
    v = vids[vi] if vi is not None else {}
    pix = v.get("pix_fmt") or ""
    side = [d.get("side_data_type", "") for d in v.get("side_data_list") or []]
    return {
        "container": fmt.get("format_name", ""),
        "duration": _duration(data),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "codec": v.get("codec_name", ""),
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "pix_fmt": pix,
        "ten_bit": any(x in pix for x in ("10", "12")),
        "hdr": v.get("color_transfer") in HDR_TRANSFERS,
        "dv": (v.get("codec_tag_string") in DV_TAGS) or any("DOVI" in s for s in side),
        "color": {k: v.get(k) for k in ("color_primaries", "color_transfer", "color_space")
                  if v.get(k) and v.get(k) != "unknown"},
        "video_index": vi,
        "video": sum(1 for s in vids if not (s.get("disposition") or {}).get("attached_pic")),
        "audio": sum(1 for s in streams if s.get("codec_type") == "audio"),
        "subs": sum(1 for s in streams if s.get("codec_type") == "subtitle"),
        "sub_codecs": [s.get("codec_name", "") for s in streams if s.get("codec_type") == "subtitle"],
    }


def probe(path):
    return summarize(ffprobe(path))


def resolution(p):
    w, h = p.get("width", 0), p.get("height", 0)
    for minw, minh, label in ((3800, 2100, "2160p"), (2500, 1400, "1440p"), (1900, 1060, "1080p"),
                              (1260, 700, "720p")):
        if w >= minw or h >= minh:
            return label
    return f"{h}p" if h else "?"


def stat_skip(st, s, now=None):
    """Skip rules that need only os.stat(). Returns a reason or None."""
    now = now or time.time()
    if s["skip_recent_minutes"] and now - st.st_mtime < s["skip_recent_minutes"] * 60:
        return "modified recently"
    if s["skip_hardlinked"] and st.st_nlink > 1:
        return "hardlinked (likely seeding)"
    if st.st_size < s["min_size_mb"] * 1024 * 1024:
        return f"smaller than {s['min_size_mb']} MB"
    return None


def needs_remux(path, s):
    return s["convert_to_mkv"] and os.path.splitext(path)[1].lower() != ".mkv"


def classify(path, p, s):
    """Decide on probed file. Returns (status, reason). status: todo | ok | skipped."""
    if p.get("video_index") is None:
        return "skipped", "no video stream"
    target = CODECS[s["codec"]]
    if p["codec"] == target or p["codec"] in s["skip_codecs"]:
        if needs_remux(path, s):
            return "todo", "remux to mkv"
        return "ok", f"already {p['codec']}"
    if p["dv"]:
        return "skipped", "Dolby Vision (cannot be preserved)"
    if p["hdr"] and s["skip_hdr"]:
        return "skipped", "HDR source (skip_hdr is on)"
    if p["hdr"] and s["codec"] == "h264_qsv":
        return "skipped", "HDR source cannot be kept with h264"
    return "todo", ""
