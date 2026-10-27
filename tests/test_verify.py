from reencoder import verify

ORIG = {"duration": 3600.0, "video": 1, "audio": 2, "subs": 3}


def out(**kw):
    return dict(ORIG, **kw)


def test_duration_tolerance_uses_larger_of_sec_and_pct():
    # 0.5% of 3600s = 18s, larger than 1s
    assert verify.duration_ok(3600, 3600 - 17.9, 1.0, 0.5)[0]
    assert not verify.duration_ok(3600, 3600 - 18.1, 1.0, 0.5)[0]
    # short file: 1s wins over 0.5% of 60s (0.3s)
    assert verify.duration_ok(60, 59.1, 1.0, 0.5)[0]
    assert not verify.duration_ok(60, 58.9, 1.0, 0.5)[0]


def test_good_output_passes(s):
    r = verify.check(ORIG, out(), 1000, 500, s, 0)
    assert r.ok, r


def test_nonzero_exit_fails(s):
    assert not verify.check(ORIG, out(), 1000, 500, s, 1).ok


def test_empty_or_unreadable_output_fails(s):
    assert not verify.check(ORIG, out(), 1000, 0, s, 0).ok
    assert not verify.check(ORIG, None, 1000, 500, s, 0).ok


def test_truncated_output_rejected(s):
    r = verify.check(ORIG, out(duration=1800.0), 1000, 500, s, 0)
    assert not r.ok and "duration" in r.reason and not r.no_savings


def test_stream_counts(s):
    assert not verify.check(ORIG, out(video=0), 1000, 500, s, 0).ok
    assert not verify.check(ORIG, out(audio=1), 1000, 500, s, 0).ok
    assert not verify.check(ORIG, out(subs=2), 1000, 500, s, 0).ok
    assert verify.check(ORIG, out(video=2), 1000, 500, s, 0).ok


def test_size_check(s):
    s["min_savings_pct"] = 5
    r = verify.check(ORIG, out(), 1000, 1000, s, 0)
    assert not r.ok and r.no_savings
    r = verify.check(ORIG, out(), 1000, 1200, s, 0)
    assert not r.ok and r.no_savings
    assert not verify.check(ORIG, out(), 1000, 960, s, 0).ok   # only 4% smaller
    assert verify.check(ORIG, out(), 1000, 950, s, 0).ok       # exactly 5%


def test_remux_skips_size_check(s):
    assert verify.check(ORIG, out(), 1000, 1010, s, 0, remux=True).ok
