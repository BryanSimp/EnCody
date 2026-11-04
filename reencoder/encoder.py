"""Build and run the ffmpeg command with live progress."""
import os
import signal
import subprocess
import threading
import time
from collections import deque

from . import config, integrations, paths

MUXERS = {".mkv": "matroska", ".mp4": "mp4", ".m4v": "mp4", ".mov": "mov"}
SVT_PRESETS = {"veryslow": 2, "slower": 3, "slow": 4, "medium": 6, "fast": 8, "faster": 10, "veryfast": 12}


TV_DIRS = {"tvshows", "tv", "tv shows", "shows", "series"}


def is_tv(path):
    """True for files under <drive>/tvshows/ (first folder below the media root)."""
    first = paths.display(path).split(os.sep)[0].lower()
    return first in TV_DIRS


TIERS = {"animation": "quality_animation", "tv": "quality_tv", "movie": "quality"}


def tier(path):
    """animation (Jellyfin genre) | tv (under tvshows/) | movie"""
    if integrations.is_animated(path):
        return "animation"
    return "tv" if is_tv(path) else "movie"


def quality_for(path, s):
    return s[TIERS[tier(path)]]


# Source codec (as ffprobe names it) -> QSV decoder. Intel Arc decodes all of these on the GPU.
# Not here (always CPU-decoded): MPEG-4/Xvid/DivX, WMV, VP8.
QSV_DECODERS = {"h264": "h264_qsv", "hevc": "hevc_qsv", "av1": "av1_qsv", "vp9": "vp9_qsv",
                "mpeg2video": "mpeg2_qsv", "vc1": "vc1_qsv"}
HW_PIX_8BIT = {"yuv420p", "yuvj420p", "nv12"}
HW_PIX_10BIT = {"yuv420p10le", "p010le"}
HW_10BIT_CODECS = {"hevc", "av1", "vp9"}  # H.264 10-bit (Hi10P) has no GPU decoder


def hw_decoder(p, s):
    """QSV decoder for this source, or None to decode on the CPU (setting off, CPU encoder,
    or a codec / pixel format the GPU cannot decode, such as Xvid, Hi10P H.264 or 4:2:2)."""
    if not (s.get("hw_decode", True) and s["codec"].endswith("_qsv")):
        return None
    codec, pix = p.get("codec"), p.get("pix_fmt") or ""
    if codec not in QSV_DECODERS:
        return None
    if pix in HW_PIX_8BIT or (pix in HW_PIX_10BIT and codec in HW_10BIT_CODECS):
        return QSV_DECODERS[codec]
    return None


def output_ext(path, s):
    """Keep the extension when the container can hold the result, otherwise .mkv."""
    ext = os.path.splitext(path)[1].lower()
    if s["convert_to_mkv"] or ext not in MUXERS:
        return ".mkv"
    return ext


def final_path(path, s):
    stem, ext = os.path.splitext(path)
    new_ext = output_ext(path, s)
    return path if ext.lower() == new_ext else stem + new_ext


def build_cmd(src, dst, p, s, remux=False, hw_decode=True):
    """ffmpeg command: copy every stream, re-encode only the main video stream.
    With QSV the video is also decoded on the GPU when possible; hw_decode=False forces CPU decoding."""
    ext = os.path.splitext(dst)[1].lower()
    qsv = s["codec"].endswith("_qsv")
    dec = hw_decoder(p, s) if (hw_decode and qsv and not remux) else None
    vi = p["video_index"]
    q = str(quality_for(src, s))
    ten = p["ten_bit"] if s["bit_depth"] == "auto" else s["bit_depth"] == "10"
    if p["hdr"]:
        ten = True  # never drop HDR to 8-bit
    if s["codec"] == "h264_qsv":
        ten = False  # h264_qsv has no 10-bit

    cmd = [config.FFMPEG, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-nostats", "-progress", "pipe:1"]
    if qsv and not remux:
        dev = s["render_device"]
        cmd += ["-init_hw_device", f"vaapi=va:{dev}", "-init_hw_device", "qsv=qs@va",
                "-filter_hw_device", "qs"]
        if dec:
            # Frames stay in GPU memory from decoder to encoder (no CPU decode, no upload).
            cmd += ["-hwaccel", "qsv", "-hwaccel_device", "qs", "-hwaccel_output_format", "qsv",
                    f"-c:v:{vi}", dec]
    cmd += ["-i", src, "-map", "0", "-map", "-0:d", "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy"]
    if not remux:
        v = f"v:{vi}"
        cmd += [f"-c:{v}", s["codec"]]
        if qsv:
            if dec:
                if ten != bool(p.get("ten_bit")):  # bit depth change happens on the GPU
                    cmd += [f"-filter:{v}", f"vpp_qsv=format={'p010' if ten else 'nv12'}"]
            else:
                fmt = "p010le" if ten else "nv12"
                cmd += [f"-filter:{v}", f"format={fmt},hwupload=extra_hw_frames=64"]
            cmd += [f"-global_quality:{v}", q, f"-preset:{v}", s["preset"]]
        else:
            preset = s["preset"] if s["preset"].isdigit() else str(SVT_PRESETS.get(s["preset"], 6))
            cmd += [f"-pix_fmt:{v}", "yuv420p10le" if ten else "yuv420p",
                    f"-crf:{v}", q, f"-preset:{v}", preset]
        color = p.get("color") or {}
        for opt, key in (("color_primaries", "color_primaries"), ("color_trc", "color_transfer"),
                         ("colorspace", "color_space")):
            if key in color:
                cmd += [f"-{opt}:{v}", color[key]]
    # MP4 text subtitles cannot be copied into MKV.
    if ext == ".mkv":
        for i, c in enumerate(p.get("sub_codecs", [])):
            if c == "mov_text":
                cmd += [f"-c:s:{i}", "srt"]
    cmd += ["-max_muxing_queue_size", "4096", "-f", MUXERS.get(ext, "matroska"), dst]
    return cmd


class Encode:
    """One running ffmpeg process. Progress fields are read by the worker/UI.

    ffmpeg 7 reports out_time_us=N/A (and speed=N/A) until EVERY output stream has written a packet,
    and afterwards the time of the stream that is furthest behind. A copied subtitle track whose first
    line comes late keeps it at N/A for a long time while the video encodes fine. So progress also
    counts video frames (frame= / source fps), and a job only counts as stalled when neither moves."""

    def __init__(self, cmd, duration, fps=0.0):
        self.cmd = cmd
        self.duration = duration or 0
        self.src_fps = fps or 0.0
        self.out_sec = 0.0
        self.frames = 0
        self.fps = 0.0
        self.started = time.time()
        self.proc = None
        self.stderr = deque(maxlen=40)
        self.aborted = False
        self.paused = False
        self.paused_total = 0.0
        self._paused_at = 0.0
        self._advanced_at = 0.0  # active seconds at the last progress

    @property
    def media_sec(self):
        """Media seconds encoded so far: the larger of ffmpeg's out_time and frames / source fps."""
        by_frames = self.frames / self.src_fps if self.src_fps else 0.0
        return max(self.out_sec, by_frames)

    @property
    def percent(self):
        return min(100.0, 100.0 * self.media_sec / self.duration) if self.duration else 0.0

    @property
    def speed(self):
        """Media seconds per wall-clock second since start (what ffmpeg's speed= means)."""
        wall = time.time() - self.started
        return self.media_sec / wall if wall > 1 else 0.0

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def stalled_for(self):
        """Active seconds (pauses excluded) since out_time or the frame count last moved."""
        return self.active_seconds() - self._advanced_at

    def _advanced(self):
        self._advanced_at = self.active_seconds()

    def feed(self, line):
        """Parse one line of ffmpeg -progress output."""
        key, _, val = line.strip().partition("=")
        try:
            if key == "out_time_us":
                sec = int(val) / 1e6  # "N/A" -> ValueError; negative at the very start is possible
                if sec > self.out_sec:
                    self.out_sec = sec
                    self._advanced()
            elif key == "frame":
                n = int(val)
                if n > self.frames:
                    self.frames = n
                    self._advanced()
            elif key == "fps":
                self.fps = float(val)
        except ValueError:
            pass

    def run(self):
        """Blocks until ffmpeg exits. Returns the exit code."""
        if self.aborted:
            return -1
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        if self.aborted:  # aborted while starting
            self.proc.kill()
        t = threading.Thread(target=lambda: [self.stderr.append(l.rstrip()) for l in self.proc.stderr],
                             daemon=True)
        t.start()
        for line in self.proc.stdout:
            self.feed(line)
        code = self.proc.wait()
        t.join(timeout=5)
        return code

    def abort(self):
        self.aborted = True
        if self.proc and self.proc.poll() is None:
            if self.paused:
                self.proc.send_signal(signal.SIGCONT)
            self.proc.kill()

    def pause(self, on):
        if self.proc and self.proc.poll() is None and on != self.paused:
            self.proc.send_signal(signal.SIGSTOP if on else signal.SIGCONT)
            self.paused = on
            if on:
                self._paused_at = time.time()
            else:
                self.paused_total += time.time() - self._paused_at

    def active_seconds(self):
        """Wall seconds ffmpeg actually ran (pauses excluded)."""
        paused = self.paused_total + (time.time() - self._paused_at if self.paused else 0)
        return max(0.0, time.time() - self.started - paused)
