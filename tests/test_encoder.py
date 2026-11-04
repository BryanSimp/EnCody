from reencoder import encoder, media

PROBE = {"video_index": 0, "ten_bit": False, "hdr": False, "color": {}, "sub_codecs": []}


def q_in(cmd):
    return cmd[cmd.index("-global_quality:v:0") + 1]


def test_tv_and_movie_quality(media, s):
    s["quality"], s["quality_tv"] = 23, 28
    tv = str(media / "tvshows" / "Show" / "S01E01.mkv")
    movie = str(media / "movies" / "Film (2000)" / "Film.mkv")
    assert encoder.is_tv(tv) and not encoder.is_tv(movie)
    assert q_in(encoder.build_cmd(tv, "/x.mkv", PROBE, s)) == "28"
    assert q_in(encoder.build_cmd(movie, "/x.mkv", PROBE, s)) == "23"


def test_software_uses_crf_per_library(media, s):
    s.update(codec="libsvtav1", quality=30, quality_tv=34)
    cmd = encoder.build_cmd(str(media / "tvshows" / "S" / "e.mkv"), "/x.mkv", PROBE, s)
    assert cmd[cmd.index("-crf:v:0") + 1] == "34"


def test_resolution_label():
    assert media.resolution({"width": 1440, "height": 1080}) == "1080p"   # 4:3
    assert media.resolution({"width": 1920, "height": 800}) == "1080p"    # scope
    assert media.resolution({"width": 1280, "height": 720}) == "720p"
    assert media.resolution({"width": 3840, "height": 1600}) == "2160p"
    assert media.resolution({"width": 640, "height": 480}) == "480p"


# ---- QSV hardware decode -------------------------------------------------------------------
HEVC10 = dict(PROBE, codec="hevc", pix_fmt="yuv420p10le", ten_bit=True)
H264 = dict(PROBE, codec="h264", pix_fmt="yuv420p")


def before_input(cmd):
    return cmd[:cmd.index("-i")]


def opt(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_hevc_10bit_decoded_on_gpu(media, s):
    cmd = encoder.build_cmd(str(media / "movies" / "a.mkv"), "/x.mkv", HEVC10, s)
    pre = before_input(cmd)
    assert (opt(pre, "-hwaccel"), opt(pre, "-hwaccel_output_format"), opt(pre, "-c:v:0")) == \
        ("qsv", "qsv", "hevc_qsv")
    assert pre.index("-init_hw_device") < pre.index("-hwaccel")   # device exists before it is used
    assert not any("hwupload" in c for c in cmd)                  # frames never leave the GPU
    assert "-filter:v:0" not in cmd                               # 10-bit in, 10-bit out: nothing to convert
    out = cmd[cmd.index("-i"):]
    assert opt(out, "-c:v:0") == "av1_qsv"


def test_bit_depth_change_on_gpu_and_stream_index(media, s):
    s["bit_depth"] = "10"
    cmd = encoder.build_cmd(str(media / "movies" / "a.mkv"), "/x.mkv", dict(H264, video_index=1), s)
    assert opt(before_input(cmd), "-c:v:1") == "h264_qsv"         # cover art is stream 0
    assert opt(cmd, "-filter:v:1") == "vpp_qsv=format=p010"
    s["bit_depth"] = "8"
    cmd = encoder.build_cmd(str(media / "movies" / "a.mkv"), "/x.mkv", HEVC10, s)
    assert opt(cmd, "-filter:v:0") == "vpp_qsv=format=nv12"


def test_cpu_decode_when_gpu_cannot(media, s):
    src = str(media / "movies" / "a.mkv")
    cpu_only = [dict(PROBE, codec="mpeg4", pix_fmt="yuv420p"),       # Xvid/DivX
                dict(H264, pix_fmt="yuv420p10le", ten_bit=True),     # Hi10P
                dict(HEVC10, pix_fmt="yuv422p10le"),                 # 4:2:2
                dict(H264, pix_fmt="")]                              # unknown
    for p in cpu_only:
        cmd = encoder.build_cmd(src, "/x.mkv", p, s)
        assert "-hwaccel" not in cmd and any("hwupload" in c for c in cmd), p
    assert "-hwaccel" not in encoder.build_cmd(src, "/x.mkv", HEVC10, s, hw_decode=False)  # fallback run
    assert "-hwaccel" not in encoder.build_cmd(src, "/x.mkv", HEVC10, s, remux=True)
    s["hw_decode"] = False
    assert "-hwaccel" not in encoder.build_cmd(src, "/x.mkv", HEVC10, s)
    s.update(hw_decode=True, codec="libsvtav1")
    assert "-hwaccel" not in encoder.build_cmd(src, "/x.mkv", HEVC10, s)


# ---- progress ----------------------------------------------------------------------------
def test_progress_uses_frames_when_ffmpeg7_reports_na():
    """ffmpeg 7 prints out_time_us=N/A until every stream (e.g. a late subtitle) wrote a packet."""
    e = encoder.Encode([], 100, fps=25)
    for line in ("frame=250", "fps=131.00", "out_time_us=N/A", "speed=N/A", "progress=continue"):
        e.feed(line + "\n")
    assert (e.out_sec, e.frames, e.fps) == (0.0, 250, 131.0)
    assert e.media_sec == 10.0 and e.percent == 10.0
    e.feed("out_time_us=20000000")
    assert e.media_sec == 20.0
    e.feed("out_time_us=-40000")                                     # negative at start: ignored
    assert e.out_sec == 20.0
    assert encoder.Encode([], 100).percent == 0.0                    # no fps known, no out_time yet


def test_stall_timer_resets_on_any_progress():
    e = encoder.Encode([], 100, fps=25)
    now = [0.0]
    e.active_seconds = lambda: now[0]
    now[0] = 700
    assert e.stalled_for() == 700
    e.feed("out_time_us=N/A")                                        # no progress
    assert e.stalled_for() == 700
    e.feed("frame=1")                                                # frames moving = not stalled
    assert e.stalled_for() == 0
    now[0] = 800
    e.feed("out_time_us=1000000")
    assert e.stalled_for() == 0
