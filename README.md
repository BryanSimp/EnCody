# Reencoder

One small Docker container that re-encodes your media library to AV1 (or HEVC/H.264) on an Intel GPU,
overnight, one file at a time, and only replaces a file after the new one has been verified.

- Pick folders, a schedule and a quality in a small web UI.
- Scans the folders, finds videos that are not already in the target codec, biggest first.
- Encodes next to the original, verifies the result, then swaps it in atomically.
- Logs plain progress lines with files left, fps, speed and an ETA for the whole queue.

Stack: Python 3.12, FastAPI, SQLite, jellyfin-ffmpeg (Intel QSV). No Redis, no Celery, no build step.

---

## 1. Deploy

1. Add the service from [`docker-compose.example.yml`](docker-compose.example.yml) to your media stack.
   Set a real `UI_PASS`.
2. `mkdir -p /home/bryan/reencoder` (it holds the database and logs).
3. Make the image pullable (see [GHCR access](#ghcr-access)), then:
   ```sh
   docker compose pull reencoder && docker compose up -d reencoder
   ```
4. **Check the GPU first** (build step 1):
   ```sh
   docker exec -it reencoder qsv-test.sh
   ```
   It prints the devices, the `vainfo` AV1 profiles and encodes a 20 s 1080p test clip with `av1_qsv`.
   It must end with `PASS`. While it runs, `sudo intel_gpu_top` on the host (or
   `docker exec -it reencoder intel_gpu_top`) should show the *Video* engine busy. Expect roughly 100+ fps for 1080p.
   If it fails, see [Troubleshooting](#troubleshooting) and do not go further.
5. Open `https://reencoder.brysimp.com` and log in with `UI_USER` / `UI_PASS`.

### First test (recommended order)

1. **Dry run is ON by default.** Go to *Folders*, open `/data/media/tvshows`, tick ONE small series.
2. Press *Scan now*, then *Run now*. Check the *Queue* tab and the *Logs* tab: it lists what *would* be
   encoded. Check *Failed / skipped* to see why other files were skipped.
3. Settings → turn *Dry run* off. Tick a folder with about 10 files (or exclude the rest). Press *Run now*.
   Check the history, play a few results in Jellyfin.
4. Tune the quality. Then add full libraries and let the schedule run.

### Updating

```sh
docker compose pull reencoder && docker compose up -d reencoder
```
A restart mid-encode is safe (see [Safety](#safety)).

### GHCR access

The GitHub Actions workflow pushes `ghcr.io/bryansimp/reencoder:latest` and `:<commit sha>` on every push to `main`.
Your server needs to be able to pull it. Pick one:

- **Make the package public (simplest):** GitHub → your profile → *Packages* → `reencoder` →
  *Package settings* → *Change visibility* → *Public*. Nothing to do on the server. The code repo can stay private.
- **Keep it private:** GitHub → Settings → Developer settings → Personal access tokens → *Tokens (classic)* →
  new token with only `read:packages`. On the server, once:
  ```sh
  echo <TOKEN> | docker login ghcr.io -u bryansimp --password-stdin
  ```

---

## 2. Settings

All settings are in the UI and stored in `/config/reencoder.db`.

### Encode
| Setting | Default | Notes |
|---|---|---|
| Video codec | `av1_qsv` | `hevc_qsv`, `h264_qsv` (Intel GPU) or `libsvtav1` (CPU, slow fallback). |
| Quality | `26` | `global_quality` for QSV, CRF for `libsvtav1`. Lower = better and bigger. av1_qsv: try 24-28. |
| Preset | `slow` | QSV: `veryfast` … `veryslow`. SVT-AV1: `0`-`13` (QSV names are mapped). |
| Bit depth | Match source | 10-bit sources stay 10-bit. HDR is always 10-bit. `h264_qsv` is always 8-bit. |
| Also convert container to MKV | off | When on, every non-MKV file becomes `.mkv` (files already in the target codec are remuxed, no re-encode). |
| GPU render device | `/dev/dri/renderD128` | Change if you have more than one GPU. |
| Concurrent jobs | `1` | One at a time is best for a single A310. |

Audio, subtitles, attachments and chapters are always copied (`-map 0 -c copy`); only the main video stream is
re-encoded. Data streams (e.g. MP4 timecode tracks) are dropped. MP4 text subtitles (`mov_text`) are converted to SRT
when the output is MKV, because MKV cannot hold `mov_text`.

**Output name:** the extension is kept when the container can hold the result (`.mkv`, `.mp4`, `.m4v`, `.mov`).
Other files (`.avi`, `.wmv`, `.ts`, `.webm`) become `.mkv`. A rename is logged as `RENAMED ... (Sonarr/Radarr/Jellyfin
will see a new path)`.

### Skip rules
| Rule | Default |
|---|---|
| Target codec | always skipped |
| Also skip these source codecs | `hevc, av1` (already efficient; re-encoding HEVC saves little and costs quality). Clear it to encode HEVC too. |
| Modified in the last N minutes | 30 (may still be downloading) |
| Hardlinked (`st_nlink > 1`) | on (see [Hardlinks](#hardlinks-and-seeding)) |
| HDR sources | skipped. Turn off to encode HDR10/HLG as 10-bit with the colour tags kept. |
| Dolby Vision | **always skipped**: the DV layer cannot be carried over, and the rule is "never silently strip HDR". |
| Smaller than N MB | 200 |
| Failed N times | 2. Press *Retry* on the *Failed / skipped* tab. |
| No savings | never retried automatically (Retry button available). |

Rescans are fast: each file is remembered by path + size + mtime. Only new or changed files are probed again.

### Verify and replace
| Setting | Default |
|---|---|
| Duration tolerance | 1 s or 0.5 %, whichever is larger |
| Require at least N % savings | 5 % |
| Quick decode test | on: first 180 s and last 60 s are decoded |
| Delete original after successful verify | on |
| Keep originals N days in `.reencoder-trash` | 0 (delete immediately) |

How the two "original" settings combine:

| Delete original | Trash days | Result |
|---|---|---|
| on | 0 | Original deleted right after the swap (default). |
| on | N > 0 | Original moved to `<drive>/.reencoder-trash/<date>/<path>`, purged after N days. |
| off | any | Original moved to `.reencoder-trash` and kept until you delete it. |

The trash lives on the same drive as the file (it is a hardlink + unlink, so it is instant), e.g.
`/mnt/hard2/media/.reencoder-trash/2026-10-28/tvshows/Show/S01E01.mkv`. Files in trash still use space.

### Schedule
Days + start/end time, default every day 22:00 → 07:00 America/Chicago. A day means the day the window *starts*
(Friday 22:00 → Saturday 07:00 is "Friday"). *Run now* runs until the queue is empty, ignoring the schedule.
*Stop* aborts the current file (temp deleted, original untouched, not counted as a failure) and waits for the next window.

When the window ends: **finish the current file** (default) or **abort** it and delete the temp file.

Scans run on start, every N hours (default 6), after a settings change, and on *Scan now*.

### Dry run
On by default. Scans and lists what would be encoded (Queue tab, and `DRY RUN would encode ...` log lines when a
window opens or you press *Run now*). Nothing is encoded, renamed or deleted.

### Integrations (all off by default)
- **Refresh after replace:** after a run that replaced files, sends `RescanSeries` to Sonarr, `RescanMovie` to
  Radarr and `Library/Refresh` to Jellyfin (only for the ones with URL + API key filled in). Use container names
  on the `proxy` network, e.g. `http://sonarr:8989`.
- **Pause while Jellyfin transcodes:** checks Jellyfin `/Sessions` every 30 s. While someone is being video-transcoded,
  ffmpeg is paused (SIGSTOP) and no new file starts; it resumes when they stop.

---

## 3. Safety

The pipeline for every file:

1. Re-check the file (size/mtime unchanged, not hardlinked, not recent) and that the drive has at least the
   original's size free. Otherwise skip and log.
2. Encode to `.<name>.reencode.tmp.<ext>` **in the same folder**. The leading dot makes Jellyfin, Sonarr and Radarr
   ignore it; the same folder means the final rename is atomic.
3. Verify, all must pass: ffmpeg exit code 0; ffprobe reads the output and it is not empty; duration within
   tolerance; at least 1 video stream; audio and subtitle stream counts equal the original; output at least N %
   smaller; decode test (if on). Any failure → temp deleted, original untouched.
4. Only then: atomic `rename()` of the temp file over the final name, then the original is deleted / trashed.
   **The original is never removed before the verified replacement exists.**
5. Result and bytes saved go to the history table.

Crash safety: on start-up, stale `.*.reencode.tmp.*` files in the selected folders are deleted and any "running"
job goes back to pending. If the server dies mid-encode, the original is intact.

Paths: the UI can only browse and select folders under `/data/media*`. Every path from the UI is resolved
(symlinks included) and rejected if it falls outside those roots. Symlinked files are never processed.

Auth: every route except `GET /health` needs HTTP basic auth (`UI_USER` / `UI_PASS`). The container refuses to
start if either is empty. Put Cloudflare Access in front as well.

### Hardlinks and seeding
Sonarr/Radarr import torrents as hardlinks, so the same data appears in the download folder (seeding in
Transmission) and in the library, using the space once. Re-encoding a hardlinked file breaks the link: the library
gets a new file while the torrent keeps the old one, so you use *more* space, not less. That is why files with
`st_nlink > 1` are skipped by default. Once a torrent is removed from Transmission, the link count drops to 1 and the
next scan picks the file up.

### Free-space tradeoff
There is no scratch disk: the temp file sits next to the original, on the media drive. A drive needs free space for
the output (roughly half the original for AV1). To be safe, a file is skipped (and logged) unless the drive has at
least the original's size free.

### Things to know
- Jellyfin shares the A310. The default schedule keeps encodes overnight; the optional Jellyfin pause covers late
  viewers.
- Jellyfin/Sonarr/Radarr see a changed file (new size and mtime). Same name = no path change. A renamed file
  (`.avi` → `.mkv`) needs a rescan in Sonarr/Radarr; turn on the refresh integration or rescan by hand.

---

## 4. Logs

Plain lines on stdout (`docker logs -f reencoder`), a rolling file in `/config/logs/reencoder.log` (5 MB × 4) and the
last 500 lines in the UI:

```
[2026-10-28 22:00:01] Window open. Queue: 412 files, 6.8TB to process.
[2026-10-28 22:00:03] START (1/412) tvshows/Show/S01E01.mkv  h264 1080p 4.2GB
[2026-10-28 22:04:10] PROGRESS 38% | 112 fps | 3.9x | file ETA 6m | QUEUE: 411 left, ETA ~41h 20m (5 nights)
[2026-10-28 22:11:30] DONE saved 1.9GB (45%) | verify OK | original deleted | total saved 1.9GB
[2026-10-29 07:00:00] Window closed. This run: 38 done, 1 failed, 0 no savings, saved 61.2GB | ...
```

Queue ETA = remaining media seconds ÷ rolling encode speed (media seconds per wall second over the last 10 files
plus the current one), and the number of schedule windows that takes. `LOG_LEVEL=DEBUG` also logs the full ffmpeg
command line.

---

## 5. Environment variables

| Variable | Default | |
|---|---|---|
| `PUID` / `PGID` | 1000 | User the app runs as. Must be able to read/write your media. |
| `TZ` | America/Chicago | Schedule and log times. |
| `UI_USER` / `UI_PASS` | (required) | Basic auth for the UI. |
| `LOG_LEVEL` | INFO | `DEBUG` for ffmpeg command lines. |

Volumes: `/config` (database + logs) and the media drives at `/data/media`, `/data/media2`, `/data/media3`.
The GPU: `/dev/dri` device + `group_add: ["993"]` (the host's `render` group, `getent group render`).

---

## 6. Troubleshooting

| Problem | Check |
|---|---|
| `qsv-test.sh` fails, "Failed to initialise VAAPI" / "Permission denied" | `ls -ln /dev/dri` on the host; the render device group must match `group_add` (993). Check `docker exec reencoder id` shows 993. |
| `vainfo` lists no `VAProfileAV1` | Host kernel too old for Arc (needs 6.2+), or `i915`/`xe` not loaded. `dmesg | grep -i -E "i915|xe"`. Jellyfin AV1 transcoding failing too points to the host. |
| Encode works but slow (< 50 fps at 1080p) | Run `intel_gpu_top`: if *Video* is idle, it is falling back. Most decode is done on the CPU (more robust); 4K HEVC sources are CPU-bound on decode. |
| Files never get picked up | *Failed / skipped* tab shows the reason. Most common: hardlinked (still seeding), modified recently, below minimum size. |
| Everything "no savings" | Quality number too low (= high quality). Raise it by 2-4. |
| "only X free on the drive" | Free up space on that drive; the file is retried after the next scan. |
| Login loop behind Cloudflare Access | Basic auth and Access both work; make sure Traefik passes the `Authorization` header (it does by default). |
| UI says "Connection problem" | `docker logs reencoder`; `curl http://<container>:8080/health`. |

Manual QSV test without the script:
```sh
docker exec -it reencoder vainfo --display drm --device /dev/dri/renderD128
docker exec -it reencoder ffmpeg -hide_banner -init_hw_device vaapi=va:/dev/dri/renderD128 \
  -init_hw_device qsv=qs@va -filter_hw_device qs -f lavfi -i testsrc2=size=1920x1080:rate=24:duration=20 \
  -vf format=nv12,hwupload=extra_hw_frames=64 -c:v av1_qsv -global_quality 26 -preset slow -y /tmp/t.mkv
```

---

## 7. Development

```sh
pip install -r requirements.txt pytest httpx
python -m pytest -q tests          # pipeline tests need ffmpeg with libsvtav1; they skip otherwise
CONFIG_DIR=./config MEDIA_ROOT_GLOB="$PWD/testmedia*" UI_USER=a UI_PASS=b python -m reencoder
```

Layout: `reencoder/` – `media.py` (ffprobe + skip rules), `scanner.py`, `encoder.py` (ffmpeg command + progress),
`verify.py`, `replace.py` (safe swap, trash, crash cleanup), `schedule.py`, `worker.py` (the loop), `app.py` (API +
auth), `static/` (the page).
