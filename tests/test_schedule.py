import datetime as dt

from reencoder import schedule


def at(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi)


def test_overnight_window(s):
    # 2026-10-28 is a Wednesday
    assert schedule.current_window(at(2026, 10, 28, 22, 0), s)
    assert schedule.current_window(at(2026, 10, 29, 6, 59), s)
    assert not schedule.current_window(at(2026, 10, 29, 7, 0), s)
    assert not schedule.current_window(at(2026, 10, 28, 21, 59), s)
    assert not schedule.current_window(at(2026, 10, 28, 12, 0), s)


def test_days_refer_to_window_start(s):
    s["schedule_days"] = [4]  # Friday only
    assert schedule.current_window(at(2026, 10, 30, 23), s)        # Friday night
    assert schedule.current_window(at(2026, 10, 31, 3), s)         # Saturday early morning, same window
    assert not schedule.current_window(at(2026, 10, 31, 23), s)    # Saturday night


def test_same_day_window(s):
    s["schedule_start"], s["schedule_end"] = "09:00", "17:00"
    assert schedule.current_window(at(2026, 10, 28, 12), s)
    assert not schedule.current_window(at(2026, 10, 28, 18), s)


def test_disabled(s):
    s["schedule_enabled"] = False
    assert schedule.current_window(at(2026, 10, 28, 23), s) is None


def test_next_window_and_windows_needed(s):
    nxt = schedule.next_window(at(2026, 10, 28, 12), s)
    assert nxt[0] == at(2026, 10, 28, 22)
    assert schedule.window_length(s) == 9 * 3600
    now = at(2026, 10, 28, 22)  # 9h left in this window
    assert schedule.windows_needed(3600, now, s) == 1
    assert schedule.windows_needed(10 * 3600, now, s) == 2
    assert schedule.windows_needed(41 * 3600, now, s) == 5
