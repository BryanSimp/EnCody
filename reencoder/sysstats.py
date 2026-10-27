"""Live CPU / RAM (whole host, from /proc) and GPU engine use of Encody's own ffmpeg processes
(from DRM fdinfo, works for i915 and xe without extra privileges)."""
import os
import time

# fdinfo engine names -> what we show. i915 reports busy nanoseconds, xe reports cycles.
ENGINES = {"video": "video", "vcs": "video", "render": "render", "rcs": "render",
           "video-enhance": "enhance", "vecs": "enhance"}


def cpu_times(proc="/proc"):
    """(busy, total) jiffies over all CPUs."""
    with open(os.path.join(proc, "stat")) as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
    total = sum(vals[:8])  # guest time is already counted in user
    return total - idle, total


def memory(proc="/proc"):
    info = {}
    with open(os.path.join(proc, "meminfo")) as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0]) * 1024
    total = info.get("MemTotal", 0)
    used = total - info.get("MemAvailable", info.get("MemFree", 0))
    return used, total


def drm_clients(pid, proc="/proc"):
    """{client_id: {key: value}} for the DRM render fds a process holds."""
    out = {}
    fd_dir = os.path.join(proc, str(pid), "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return out
    for fd in fds:
        try:
            if not os.readlink(os.path.join(fd_dir, fd)).startswith("/dev/dri/"):
                continue
            with open(os.path.join(proc, str(pid), "fdinfo", fd)) as f:
                info = dict(line.split(":", 1) for line in f if ":" in line)
        except OSError:
            continue
        info = {k.strip(): v.strip() for k, v in info.items()}
        cid = info.get("drm-client-id")
        if cid:
            out[cid] = info
    return out


def engine_counters(clients):
    """{engine: (busy, total_or_None, capacity)} summed over clients.
    i915: drm-engine-<name>: <ns> ns. xe: drm-cycles-<name> / drm-total-cycles-<name>."""
    sums = {}
    for info in clients.values():
        for key, val in info.items():
            if key.startswith("drm-engine-") and not key.startswith("drm-engine-capacity-"):
                name = ENGINES.get(key[len("drm-engine-"):])
                if name:
                    busy, total, cap = sums.get(name, (0, None, 1))
                    capn = int(info.get("drm-engine-capacity-" + key[len("drm-engine-"):], "1").split()[0])
                    sums[name] = (busy + int(val.split()[0]), None, max(cap, capn))
            elif key.startswith("drm-cycles-"):
                eng = key[len("drm-cycles-"):]
                name = ENGINES.get(eng)
                if name:
                    busy, total, cap = sums.get(name, (0, 0, 1))
                    tot = int(info.get("drm-total-cycles-" + eng, "0").split()[0])
                    # all clients share the same GPU clock: keep the largest total, not the sum
                    sums[name] = (busy + int(val.split()[0]), max(total or 0, tot), cap)
    return sums


class Sampler:
    """Call sample() every few seconds; .latest holds the last computed values."""

    def __init__(self, proc="/proc"):
        self.proc = proc
        self.prev_cpu = None
        self.prev_gpu = {}   # (client_id, engine) -> (busy, total, wall)
        self.latest = {"cpu": None, "ram_used": None, "ram_total": None, "gpu": {}}

    def sample(self, pids=()):
        out = {"cpu": None, "ram_used": None, "ram_total": None, "gpu": {}}
        try:
            busy, total = cpu_times(self.proc)
            if self.prev_cpu and total > self.prev_cpu[1]:
                out["cpu"] = round(100.0 * (busy - self.prev_cpu[0]) / (total - self.prev_cpu[1]), 1)
            self.prev_cpu = (busy, total)
            out["ram_used"], out["ram_total"] = memory(self.proc)
        except (OSError, ValueError, IndexError):
            pass
        out["gpu"] = self._gpu(pids)
        self.latest = out
        return out

    def _gpu(self, pids):
        now = time.monotonic()
        cur, pct = {}, {}
        for pid in pids:
            for cid, info in drm_clients(pid, self.proc).items():
                for eng, (busy, total, cap) in engine_counters({cid: info}).items():
                    cur[(cid, eng)] = (busy, total, now, cap)
        for key, (busy, total, wall, cap) in cur.items():
            prev = self.prev_gpu.get(key)
            if not prev:
                continue
            if total is None:  # i915: nanoseconds busy per engine class
                d = (busy - prev[0]) / ((wall - prev[2]) * 1e9 * cap) if wall > prev[2] else 0
            else:              # xe: cycles busy / total cycles
                d = (busy - prev[0]) / (total - prev[1]) if total > prev[1] else 0
            eng = key[1]
            pct[eng] = pct.get(eng, 0) + 100.0 * d
        self.prev_gpu = cur
        return {k: round(min(100.0, max(0.0, v)), 1) for k, v in pct.items()}
