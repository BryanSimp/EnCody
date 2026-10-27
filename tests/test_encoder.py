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
