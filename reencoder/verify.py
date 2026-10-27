"""Checks an encoded temp file must pass before the original is touched."""
import os
import subprocess

from . import config


class Result:
    def __init__(self, ok, reason="", no_savings=False):
        self.ok, self.reason, self.no_savings = ok, reason, no_savings

    def __repr__(self):
        return f"Result(ok={self.ok}, reason={self.reason!r}, no_savings={self.no_savings})"


def duration_ok(orig, out, tol_sec, tol_pct):
    """Within tol_sec seconds or tol_pct percent of the original, whichever is larger."""
    tol = max(tol_sec, orig * tol_pct / 100.0)
    return abs(orig - out) <= tol, tol


def check(orig, out, orig_size, out_size, s, exit_code, remux=False):
    """Pure verification of probe summaries and sizes. `out` is None if ffprobe failed."""
    if exit_code != 0:
        return Result(False, f"ffmpeg exit code {exit_code}")
    if not out_size:
        return Result(False, "output file is empty")
    if out is None:
        return Result(False, "ffprobe could not read the output")
    ok, tol = duration_ok(orig["duration"], out["duration"], s["duration_tol_sec"], s["duration_tol_pct"])
    if not ok:
        return Result(False, f"duration mismatch: original {orig['duration']:.2f}s, "
                             f"output {out['duration']:.2f}s (tolerance {tol:.2f}s)")
    if out["video"] < 1:
        return Result(False, "output has no video stream")
    for kind in ("audio", "subs"):
        if out[kind] != orig[kind]:
            return Result(False, f"{kind} stream count {out[kind]} != original {orig[kind]}")
    if not remux:
        need = orig_size * (1 - s["min_savings_pct"] / 100.0)
        if out_size >= orig_size or out_size > need:
            pct = 100.0 * (orig_size - out_size) / orig_size if orig_size else 0
            return Result(False, f"no savings: output {pct:.1f}% smaller, need {s['min_savings_pct']}%",
                          no_savings=True)
    return Result(True)


def decode_test(path, duration, head_sec, tail_sec, timeout=1800):
    """Decode the first and last part of the file. Returns an error string or None."""
    parts = []
    if head_sec:
        parts.append(["-t", str(head_sec), "-i", path])
    if tail_sec and duration > head_sec:
        parts.append(["-sseof", f"-{min(tail_sec, duration)}", "-i", path])
    for part in parts:
        try:
            r = subprocess.run([config.FFMPEG, "-nostdin", "-v", "error", *part, "-f", "null", "-"],
                               capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"decode test failed: {e}"
        if r.returncode != 0 or r.stderr.strip():
            return f"decode test failed: {r.stderr.strip()[-300:] or 'exit ' + str(r.returncode)}"
    return None


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
