"""Schedule windows. A window starts on a selected weekday at `start` and runs to `end`
(the next day when end <= start). Times are local (TZ env, e.g. America/Chicago)."""
import datetime as dt
import math


def _t(hhmm):
    h, m = hhmm.split(":")
    return dt.time(int(h), int(m))


def window_bounds(day, s):
    start = dt.datetime.combine(day, _t(s["schedule_start"]))
    end = dt.datetime.combine(day, _t(s["schedule_end"]))
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def current_window(now, s):
    """(start, end) of the window containing `now` (naive local time), or None."""
    if not s["schedule_enabled"]:
        return None
    for back in (0, 1):
        day = now.date() - dt.timedelta(days=back)
        if day.weekday() not in s["schedule_days"]:
            continue
        start, end = window_bounds(day, s)
        if start <= now < end:
            return start, end
    return None


def next_window(now, s):
    if not s["schedule_enabled"] or not s["schedule_days"]:
        return None
    for ahead in range(0, 8):
        day = now.date() + dt.timedelta(days=ahead)
        if day.weekday() in s["schedule_days"]:
            start, end = window_bounds(day, s)
            if start > now:
                return start, end
    return None


def window_length(s):
    start, end = window_bounds(dt.date(2000, 1, 3), s)
    return (end - start).total_seconds()


def windows_needed(eta_sec, now, s):
    """How many scheduled windows (including the current one) the remaining work needs."""
    if eta_sec <= 0:
        return 0
    length = window_length(s)
    cur = current_window(now, s)
    if cur:
        left = (cur[1] - now).total_seconds()
        if eta_sec <= left:
            return 1
        return 1 + math.ceil((eta_sec - left) / length)
    return math.ceil(eta_sec / length)
