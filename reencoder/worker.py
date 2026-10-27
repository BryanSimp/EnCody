"""The single background worker: scans, follows the schedule, runs encodes, logs progress."""
import datetime as dt
import logging
import os
import threading
import time
from collections import Counter

from . import encoder, integrations, media, paths, replace, scanner, schedule, verify
from .log import human_bytes as hb, human_time as ht

log = logging.getLogger("reencoder")

PROGRESS_EVERY = 60
JELLYFIN_EVERY = 30


class Job:
    def __init__(self, row, s):
        self.path = row["path"]
        self.size = row["size"]
        self.probe = row["probe"]
        self.remux = row["reason"] == "remux to mkv"
        self.final = encoder.final_path(self.path, s)
        self.tmp = paths.temp_path(self.final)
        self.encode = None
        self.started = time.time()
        self.stop_reason = ""

    def remaining_media(self):
        done = self.encode.out_sec if self.encode else 0
        return max(0.0, self.probe["duration"] - done)

    def file_eta(self):
        e = self.encode
        if not e or e.speed <= 0:
            return None
        return self.remaining_media() / e.speed

    def info(self):
        e = self.encode
        return {"path": self.path, "display": paths.display(self.path), "size": self.size,
                "codec": self.probe["codec"], "res": media.resolution(self.probe),
                "percent": round(e.percent, 1) if e else 0, "fps": e.fps if e else 0,
                "speed": e.speed if e else 0, "eta": self.file_eta(),
                "paused": bool(e and e.paused), "remux": self.remux,
                "phase": "encoding" if e and e.proc and e.proc.poll() is None else "verifying"}


class Worker:
    def __init__(self, db):
        self.db = db
        self.jobs = {}                 # path -> Job
        self.lock = threading.Lock()
        self.manual = False            # "Run now" pressed: run regardless of schedule
        self.stopped_window = None     # window start the user pressed Stop in
        self.window = None             # start of the window we are currently in
        self.scan_requested = True
        self.scanning = False
        self.last_scan = 0.0
        self.last_progress = 0.0
        self.last_jf = 0.0
        self.jf_busy = False
        self.run_started = 0           # jobs started in this window / manual run
        self.stats = Counter()         # this window / manual run
        self.idle_logged = False
        self.dry_logged = False
        self.no_space = set()
        self._stop = threading.Event()

    # ---- lifecycle -------------------------------------------------------
    def start(self):
        self.recover()
        threading.Thread(target=self.loop, name="worker", daemon=True).start()

    def shutdown(self):
        self._stop.set()
        with self.lock:
            for j in self.jobs.values():
                j.stop_reason = "shutdown"
                if j.encode:
                    j.encode.abort()

    def recover(self):
        """Crash safety: remove stale temp files and put interrupted jobs back in the queue."""
        s = self.db.settings()
        n = self.db.x("UPDATE files SET status='todo', reason='' WHERE status='running'")
        removed = replace.clean_stale_temps(s["folders"])
        for p in removed:
            log.info(f"Recovery: removed stale temp file {p}")
        if n or removed:
            log.info(f"Recovery: {n} interrupted job(s) reset to pending, {len(removed)} temp file(s) removed")
        if s["delete_original"]:
            replace.purge_trash(s["trash_days"])

    def loop(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("Worker error")
            self._stop.wait(2)

    # ---- controls (called from the API) ------------------------------------
    def run_now(self):
        self.manual = True
        self.stopped_window = None
        self._begin_run("Run now pressed.")

    def stop(self):
        self.manual = False
        self.stopped_window = self.window
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            j.stop_reason = "stopped by user"
            if j.encode:
                j.encode.abort()
        log.info("Stop pressed. Current job aborted; waiting for the next window.")

    def request_scan(self):
        self.scan_requested = True

    # ---- main tick ---------------------------------------------------------
    def tick(self, now=None):
        s = self.db.settings()
        now = now or dt.datetime.now()
        interval = s["scan_interval_hours"] * 3600
        if not self.scanning and (self.scan_requested or (interval and time.time() - self.last_scan > interval)):
            self.scan_requested = False
            self.scanning = True
            threading.Thread(target=self._scan, args=(s,), name="scan", daemon=True).start()

        win = schedule.current_window(now, s)
        win_id = win[0] if win else None
        if win_id != self.window:
            if self.window is not None and not self.manual:
                self._end_run("Window closed.", s)
            self.window = win_id
            if win_id is not None and not self.manual:
                self._begin_run("Window open.")

        active = self.manual or (win_id is not None and win_id != self.stopped_window)
        with self.lock:
            jobs = list(self.jobs.values())

        if not active and jobs and s["window_end_action"] == "abort":
            for j in jobs:
                if not j.stop_reason:
                    j.stop_reason = "window ended"
                    j.encode and j.encode.abort()

        self._jellyfin_check(s, jobs)

        if active and not self.scanning:
            if s["dry_run"]:
                if not self.dry_logged:
                    self._log_dry_run(s)
                    self.dry_logged = True
                    if self.manual:
                        self.manual = False
            else:
                while len(self.jobs) < s["concurrent_jobs"] and not self.jf_busy:
                    row = self._next_job(s)
                    if not row:
                        break
                    self._start_job(row, s)
                if not self.jobs and not self.idle_logged and not self.jf_busy:
                    self.idle_logged = True
                    if self.manual:
                        self.manual = False
                        self._end_run("Queue empty. Manual run finished.", s)
                    else:
                        log.info("Queue empty. Waiting for the next scan.")

        if self.jobs and time.time() - self.last_progress >= PROGRESS_EVERY:
            self.last_progress = time.time()
            self._log_progress(s, now)

    # ---- scanning ----------------------------------------------------------
    def _scan(self, s):
        try:
            if not s["folders"]:
                log.info("SCAN skipped: no folders selected.")
                return
            log.info(f"SCAN started: {len(s['folders'])} folder(s)")
            n = integrations.refresh_animation(s)
            if n is not None:
                log.info(f"Jellyfin: {n} animated series/movies found (quality {s['quality_animation']})")
            r = scanner.scan(self.db, s, running=self.jobs)
            c = r["counts"]
            q = self.queue_totals()
            reasons = ", ".join(f"{k}: {v}" for k, v in sorted(r["skip_reasons"].items(), key=lambda x: -x[1]))
            log.info(f"SCAN done in {r['seconds']:.0f}s | {c['total']} files | done {c['done']} | "
                     f"to do {c['todo']} ({hb(q['bytes'])}) | failed {c['failed']} | skipped {c['skipped']}"
                     + (f" ({reasons})" if reasons else ""))
            self.idle_logged = False
            self.dry_logged = False
            self.no_space.clear()
        except Exception:
            log.exception("SCAN failed")
        finally:
            self.last_scan = time.time()
            self.scanning = False

    # ---- queue -------------------------------------------------------------
    def queue_totals(self):
        r = self.db.q("SELECT COUNT(*) n, COALESCE(SUM(size),0) b, "
                      "COALESCE(SUM(json_extract(probe,'$.duration')),0) d FROM files WHERE status='todo'")[0]
        return {"files": r["n"], "bytes": r["b"], "media_sec": r["d"]}

    def speed(self):
        """Rolling encode speed: media seconds per wall-clock second."""
        pairs = self.db.recent_speeds(10)
        with self.lock:
            for j in self.jobs.values():
                if j.encode and j.encode.out_sec > 0:
                    pairs.append((j.encode.out_sec, time.time() - j.encode.started))
        media_s = sum(p[0] for p in pairs)
        wall = sum(p[1] for p in pairs)
        return media_s / wall if wall > 0 else None

    def queue_eta(self, s, now=None):
        q = self.queue_totals()
        with self.lock:
            jobs = list(self.jobs.values())
        left = q["media_sec"] + sum(j.remaining_media() for j in jobs)
        speed = self.speed()
        par = max(1, min(s["concurrent_jobs"], q["files"] + len(jobs)))
        eta = left / (speed * par) if speed else None
        windows = schedule.windows_needed(eta, now or dt.datetime.now(), s) if eta and s["schedule_enabled"] else None
        return {"files": q["files"] + len(jobs), "bytes": q["bytes"] + sum(j.size for j in jobs),
                "eta": eta, "windows": windows, "speed": speed}

    def _next_job(self, s):
        for row in self.db.queue(s["sort"], limit=50):
            p = row["path"]
            if p in self.jobs or p in self.no_space:
                continue
            try:
                paths.safe_path(p)
                st = os.stat(p)
            except (OSError, paths.PathError):
                self.scan_requested = True
                continue
            if st.st_size != row["size"] or st.st_mtime != row["mtime"]:
                self.scan_requested = True  # changed since the scan: re-evaluate first
                continue
            reason = media.stat_skip(st, s)
            if reason:
                self.db.set_status(p, "skipped", reason)
                continue
            if not row["probe"]:
                continue
            free = replace.free_space(p)
            if free < row["size"]:
                log.warning(f"SKIP {paths.display(p)}: only {hb(free)} free on the drive, "
                            f"need {hb(row['size'])}")
                self.no_space.add(p)
                continue
            return row
        return None

    # ---- runs and windows ----------------------------------------------------
    def _begin_run(self, msg):
        self.run_started = 0
        self.stats = Counter()
        self.idle_logged = False
        self.dry_logged = False
        self.no_space.clear()
        q = self.queue_totals()
        log.info(f"{msg} Queue: {q['files']} files, {hb(q['bytes'])} to process.")

    def _end_run(self, msg, s):
        st = self.stats
        q = self.queue_eta(s)
        log.info(f"{msg} This run: {st['done']} done, {st['failed']} failed, {st['no_savings']} no savings, "
                 f"saved {hb(st['saved'])} | total saved {hb(self.db.total_saved())} | "
                 f"queue: {q['files']} left, ETA ~{ht(q['eta'])}")
        if st["done"] and s["refresh_after_replace"]:
            threading.Thread(target=integrations.refresh_libraries, args=(s,), daemon=True).start()
        if s["delete_original"]:
            replace.purge_trash(s["trash_days"])

    def _log_dry_run(self, s):
        rows = self.db.queue(s["sort"])
        log.info(f"DRY RUN: {len(rows)} file(s) would be encoded, {hb(sum(r['size'] for r in rows))} total. "
                 "Nothing will be changed.")
        for r in rows:
            p = r["probe"] or {}
            log.info(f"DRY RUN would encode {paths.display(r['path'])}  {p.get('codec', '?')} "
                     f"{media.resolution(p)} {hb(r['size'])}  q={encoder.quality_for(r['path'], s)} "
                     f"({encoder.tier(r['path'])})" + (f"  ({r['reason']})" if r["reason"] else ""))

    def _log_progress(self, s, now):
        q = self.queue_eta(s, now)
        with self.lock:
            jobs = list(self.jobs.values())
        for j in jobs:
            e = j.encode
            if not e:
                continue
            nights = f" ({q['windows']} night{'s' if q['windows'] != 1 else ''})" if q["windows"] else ""
            paused = " | PAUSED (Jellyfin transcoding)" if e.paused else ""
            log.info(f"PROGRESS {e.percent:.0f}% | {e.fps:.0f} fps | {e.speed:.1f}x | file ETA {ht(j.file_eta())} | "
                     f"QUEUE: {q['files'] - 1} left, ETA ~{ht(q['eta'])}{nights}{paused}")

    def _jellyfin_check(self, s, jobs):
        if not s["pause_on_jellyfin_transcode"]:
            if self.jf_busy:
                self.jf_busy = False
                for j in jobs:
                    j.encode and j.encode.pause(False)
            return
        if time.time() - self.last_jf < JELLYFIN_EVERY:
            return
        self.last_jf = time.time()
        busy = integrations.jellyfin_transcoding(s)
        if busy != self.jf_busy:
            log.info("Jellyfin is transcoding: pausing encodes." if busy else "Jellyfin idle: resuming encodes.")
        self.jf_busy = busy
        for j in jobs:
            j.encode and j.encode.pause(busy)

    # ---- one job -------------------------------------------------------------
    def _start_job(self, row, s):
        job = Job(row, s)
        with self.lock:
            self.jobs[job.path] = job
        self.db.set_status(job.path, "running")
        self.run_started += 1
        total = self.run_started + self.queue_totals()["files"]
        p = job.probe
        log.info(f"START ({self.run_started}/{total}) {paths.display(job.path)}  {p['codec']} "
                 f"{media.resolution(p)} {hb(job.size)}"
                 + ("  [remux only]" if job.remux else
                    f"  q={encoder.quality_for(job.path, s)} ({encoder.tier(job.path)})"))
        self.last_progress = time.time()
        threading.Thread(target=self._run_job, args=(job, dict(s)), name="encode", daemon=True).start()

    def _run_job(self, job, s):
        try:
            self._encode_and_replace(job, s)
        except Exception as e:
            log.exception(f"FAIL {paths.display(job.path)}: unexpected error")
            self._failed(job, s, f"unexpected error: {e}")
        finally:
            replace.remove_temp(job.tmp)
            with self.lock:
                self.jobs.pop(job.path, None)
            self.idle_logged = False

    def _encode_and_replace(self, job, s):
        cmd = encoder.build_cmd(job.path, job.tmp, job.probe, s, remux=job.remux)
        log.debug("ffmpeg: " + " ".join(cmd))
        job.encode = enc = encoder.Encode(cmd, job.probe["duration"])
        try:
            code = enc.run()
        except OSError as e:
            code = -1
            enc.stderr.append(str(e))
        if job.stop_reason or enc.aborted:
            replace.remove_temp(job.tmp)
            self.db.set_status(job.path, "todo", "")
            self._history(job, "aborted", job.stop_reason or "aborted")
            log.info(f"ABORT {paths.display(job.path)}: {job.stop_reason or 'aborted'}; temp removed, original untouched")
            return

        out_size = verify.file_size(job.tmp)
        out = None
        if code == 0 and out_size:
            try:
                out = media.probe(job.tmp)
            except media.ProbeError:
                out = None
        res = verify.check(job.probe, out, job.size, out_size, s, code, remux=job.remux)
        if res.ok and s["decode_test"]:
            err = verify.decode_test(job.tmp, out["duration"], s["decode_head_sec"], s["decode_tail_sec"])
            if err:
                res = verify.Result(False, err)
        if not res.ok:
            if code != 0 and enc.stderr:
                log.warning("ffmpeg said: " + " | ".join(list(enc.stderr)[-5:]))
            replace.remove_temp(job.tmp)
            if res.no_savings:
                self.db.set_status(job.path, "no_savings", res.reason)
                self._history(job, "no_savings", res.reason, new_size=out_size)
                self.stats["no_savings"] += 1
                log.info(f"NO SAVINGS {paths.display(job.path)}: {res.reason}; original kept, will not retry")
            else:
                self._failed(job, s, res.reason)
            return

        note = replace.replace(job.path, job.tmp, job.final, s)
        st = os.stat(job.final)
        saved = job.size - st.st_size
        if job.final != job.path:
            self.db.delete_files([job.path])
            log.warning(f"RENAMED {paths.display(job.path)} -> {os.path.basename(job.final)} "
                        "(Sonarr/Radarr/Jellyfin will see a new path)")
        self.db.put_files([{"path": job.final, "size": st.st_size, "mtime": st.st_mtime, "status": "done",
                            "reason": f"saved {hb(saved)}", "failures": 0, "probe": out}])
        self._history(job, "done", note, new_size=st.st_size, saved=saved)
        self.stats["done"] += 1
        self.stats["saved"] += saved
        pct = 100.0 * saved / job.size if job.size else 0
        log.info(f"DONE saved {hb(saved)} ({pct:.0f}%) | verify OK | {note} | total saved {hb(self.db.total_saved())}")

    def _failed(self, job, s, reason):
        row = self.db.file(job.path)
        failures = (row["failures"] if row else 0) + 1
        status = "failed" if failures >= s["max_failures"] else "todo"
        self.db.set_status(job.path, status, reason[:300], failures=failures)
        self._history(job, "failed", reason[:300])
        self.stats["failed"] += 1
        extra = " (will not retry until you press Retry)" if status == "failed" else " (will retry)"
        log.warning(f"FAIL {paths.display(job.path)}: {reason}; temp removed, original untouched{extra}")

    def _history(self, job, result, message, new_size=0, saved=0):
        self.db.add_history(path=job.path, new_path=job.final if result == "done" else None,
                            started=job.started, finished=time.time(), orig_size=job.size,
                            new_size=new_size, saved=saved,
                            media_sec=job.probe["duration"] if result == "done" and not job.remux else 0,
                            result=result, message=message)
