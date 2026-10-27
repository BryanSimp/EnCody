"""Settings defaults and validation. Stored as JSON values in SQLite."""
import re

CODECS = {
    # encoder name -> codec name as ffprobe reports it
    "av1_qsv": "av1",
    "hevc_qsv": "hevc",
    "h264_qsv": "h264",
    "libsvtav1": "av1",
}

DEFAULTS = {
    # Folders
    "folders": [],
    "excludes": [],
    # Encode
    "codec": "av1_qsv",
    # global_quality for QSV, CRF for libsvtav1. Three tiers:
    "quality": 26,            # live-action movies (and anything not under tvshows/)
    "quality_tv": 26,         # live-action TV: files under <drive>/tvshows/
    "quality_animation": 26,  # animated movies and shows (needs detect_animation + Jellyfin URL/key)
    "detect_animation": False,  # use Jellyfin genres (Animation/Anime) to find animated titles
    "preset": "slow",         # QSV: veryfast..veryslow. SVT-AV1: 0-13 (names are mapped)
    "bit_depth": "auto",      # auto (match source) | 10 | 8
    "convert_to_mkv": False,  # also remux non-MKV files even if already the target codec
    "render_device": "/dev/dri/renderD128",
    # Scanner / skip rules
    "skip_codecs": ["hevc", "av1"],  # already-efficient source codecs (target codec is always skipped)
    "skip_recent_minutes": 30,
    "skip_hardlinked": True,
    "skip_hdr": True,
    "min_size_mb": 200,
    "max_failures": 2,
    "sort": "size_desc",      # size_desc | size_asc | path
    "scan_interval_hours": 6,
    # Verify
    "duration_tol_sec": 1.0,
    "duration_tol_pct": 0.5,
    "min_savings_pct": 5,
    "decode_test": True,
    "decode_head_sec": 180,
    "decode_tail_sec": 60,
    # Replace
    "delete_original": True,
    "trash_days": 0,
    # Schedule
    "schedule_enabled": True,
    "schedule_days": [0, 1, 2, 3, 4, 5, 6],  # Monday = 0. Day the window STARTS on.
    "schedule_start": "22:00",
    "schedule_end": "07:00",
    "window_end_action": "finish",  # finish | abort
    "concurrent_jobs": 1,
    "dry_run": True,
    # Integrations (all off by default)
    "refresh_after_replace": False,
    "sonarr_url": "", "sonarr_key": "",
    "radarr_url": "", "radarr_key": "",
    "jellyfin_url": "", "jellyfin_key": "",
    "pause_on_jellyfin_transcode": False,
}

ENUMS = {
    "codec": set(CODECS),
    "bit_depth": {"auto", "10", "8"},
    "sort": {"size_desc", "size_asc", "path"},
    "window_end_action": {"finish", "abort"},
}
RANGES = {
    "quality": (1, 63),
    "quality_tv": (1, 63),
    "quality_animation": (1, 63),
    "skip_recent_minutes": (0, 100000),
    "min_size_mb": (0, 1000000),
    "max_failures": (1, 100),
    "scan_interval_hours": (0, 720),
    "duration_tol_sec": (0, 600),
    "duration_tol_pct": (0, 50),
    "min_savings_pct": (0, 99),
    "decode_head_sec": (0, 36000),
    "decode_tail_sec": (0, 36000),
    "trash_days": (0, 3650),
    "concurrent_jobs": (1, 4),
}
SECRET_KEYS = {"sonarr_key", "radarr_key", "jellyfin_key"}
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(key, value):
    """Coerce and validate one setting. Raises ValueError. Folder paths are checked by the caller."""
    if key not in DEFAULTS:
        raise ValueError(f"unknown setting: {key}")
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true/false")
    elif isinstance(default, (int, float)):
        try:
            value = type(default)(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number")
        lo, hi = RANGES.get(key, (float("-inf"), float("inf")))
        if not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        if key == "schedule_days":
            value = sorted({int(d) for d in value if 0 <= int(d) <= 6})
        else:
            value = [str(v).strip() for v in value if str(v).strip()]
    else:
        value = str(value).strip()
    if key in ENUMS and value not in ENUMS[key]:
        raise ValueError(f"{key} must be one of {sorted(ENUMS[key])}")
    if key in ("schedule_start", "schedule_end") and not TIME_RE.match(value):
        raise ValueError(f"{key} must be HH:MM (24h)")
    if key == "preset" and not re.match(r"^[a-z0-9]{1,12}$", value):
        raise ValueError("preset must be a short word or number")
    if key == "render_device" and not value.startswith("/dev/dri/"):
        raise ValueError("render_device must be under /dev/dri/")
    return value
