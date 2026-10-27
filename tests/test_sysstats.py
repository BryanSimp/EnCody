import os

from reencoder import sysstats


def fake_proc(root, cpu, video_ns=None, xe=None):
    root.mkdir(exist_ok=True)
    busy, idle = cpu
    (root / "stat").write_text(f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0\ncpu0 1 2 3 4\n")
    (root / "meminfo").write_text("MemTotal:       32000000 kB\nMemFree:  1000000 kB\nMemAvailable:   24000000 kB\n")
    pid = root / "4242"
    (pid / "fd").mkdir(parents=True, exist_ok=True)
    (pid / "fdinfo").mkdir(exist_ok=True)
    for fd, target in (("3", "/dev/dri/renderD128"), ("4", "/dev/dri/renderD128"), ("5", "/tmp/x.mkv")):
        link = pid / "fd" / fd
        if not os.path.islink(link):
            os.symlink(target, link)
    if video_ns is not None:   # i915, two fds of the same client
        info = (f"drm-driver:\ti915\ndrm-client-id:\t7\ndrm-engine-render:\t0 ns\n"
                f"drm-engine-video:\t{video_ns} ns\ndrm-engine-capacity-video:\t2\n")
    else:                      # xe
        cyc, tot = xe
        info = (f"drm-driver:\txe\ndrm-client-id:\t9\ndrm-cycles-vcs:\t{cyc}\n"
                f"drm-total-cycles-vcs:\t{tot}\ndrm-cycles-rcs:\t0\ndrm-total-cycles-rcs:\t{tot}\n")
    for fd in ("3", "4"):
        (pid / "fdinfo" / fd).write_text(info)
    (pid / "fdinfo" / "5").write_text("pos:\t0\n")
    return 4242


def test_cpu_ram_and_i915_gpu(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    pid = fake_proc(proc, (100, 900), video_ns=0)
    clock = iter([1000.0, 1002.0])
    monkeypatch.setattr(sysstats.time, "monotonic", lambda: next(clock))
    s = sysstats.Sampler(str(proc))
    first = s.sample([pid])
    assert first["cpu"] is None and first["gpu"] == {}
    assert first["ram_total"] == 32000000 * 1024 and first["ram_used"] == 8000000 * 1024
    # +2 s: CPU 300 busy of 1000 jiffies; video engine 3.6e9 ns busy of 2 s x 2 engines -> 90%
    fake_proc(proc, (400, 1600), video_ns=3_600_000_000)
    second = s.sample([pid])
    assert second["cpu"] == 30.0
    assert second["gpu"]["video"] == 90.0
    assert second["gpu"]["render"] == 0.0


def test_xe_gpu(tmp_path):
    proc = tmp_path / "proc"
    pid = fake_proc(proc, (0, 0), xe=(0, 1000))
    s = sysstats.Sampler(str(proc))
    s.sample([pid])
    fake_proc(proc, (0, 0), xe=(750, 2000))
    assert s.sample([pid])["gpu"]["video"] == 75.0


def test_missing_process_is_ignored(tmp_path):
    proc = tmp_path / "proc"
    fake_proc(proc, (1, 1), video_ns=0)
    s = sysstats.Sampler(str(proc))
    assert s.sample([99999])["gpu"] == {}
    assert sysstats.Sampler("/nonexistent").sample()["cpu"] is None


def test_real_proc_works():
    out = sysstats.Sampler().sample([os.getpid()])
    assert out["ram_total"] and out["ram_used"] <= out["ram_total"]
