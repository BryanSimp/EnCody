"""FastAPI app: JSON API + one static page. Every route except /health needs basic auth."""
import base64
import datetime as dt
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from . import config, encoder, log as logmod, paths, schedule
from .db import DB
from .settings import DEFAULTS, SECRET_KEYS, validate
from .worker import Worker

log = logging.getLogger("reencoder")
STATIC = os.path.join(os.path.dirname(__file__), "static")


def create_app(db=None, start_worker=True):
    logmod.setup()
    if not (config.UI_USER and config.UI_PASS):
        raise SystemExit("UI_USER and UI_PASS must be set. Refusing to start without authentication.")
    if config.UI_PASS == "change-me":
        log.warning("UI_PASS is still 'change-me'. Please set a real password.")
    if db is None:
        os.makedirs(config.CONFIG_DIR, exist_ok=True)
        db = DB(config.DB_PATH)
    worker = Worker(db)

    @asynccontextmanager
    async def lifespan(app):
        roots = config.media_roots()
        log.info(f"Encody starting. Media roots: {', '.join(roots) or 'none found'}")
        if start_worker:
            worker.start()
        yield
        worker.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.worker = db, worker

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        header = request.headers.get("authorization", "")
        ok = False
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user.encode(), config.UI_USER.encode()) & \
                    secrets.compare_digest(pw.encode(), config.UI_PASS.encode())
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="encody"'})
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(os.path.join(STATIC, "index.html"))

    @app.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "style.css"):
            raise HTTPException(404)
        return FileResponse(os.path.join(STATIC, name))

    def public_settings():
        s = db.settings()
        for k in SECRET_KEYS:
            s[k] = "********" if s[k] else ""
        return s

    @app.get("/api/state")
    def state():
        s = db.settings()
        now = dt.datetime.now()
        win = schedule.current_window(now, s)
        nxt = schedule.next_window(now, s)
        with worker.lock:
            jobs = [j.info() for j in worker.jobs.values()]
        return {
            "settings": public_settings(),
            "counts": db.counts(),
            "queue": worker.queue_eta(s, now),
            "jobs": jobs,
            "total_saved": db.total_saved(),
            "scanning": worker.scanning,
            "manual": worker.manual,
            "jellyfin_busy": worker.jf_busy,
            "window": {"open": bool(win), "end": win[1].isoformat() if win else None,
                       "next": nxt[0].isoformat() if nxt else None,
                       "stopped": bool(win) and worker.stopped_window == win[0]},
            "now": now.isoformat(timespec="seconds"),
        }

    @app.put("/api/settings")
    async def put_settings(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object")
        clean = {}
        try:
            for k, v in body.items():
                if k in SECRET_KEYS and v == "********":
                    continue  # unchanged masked secret
                v = validate(k, v)
                if k in ("folders", "excludes"):
                    v = sorted({paths.safe_path(p) for p in v})
                clean[k] = v
        except (ValueError, paths.PathError) as e:
            raise HTTPException(400, str(e))
        current = db.settings()
        clean = {k: v for k, v in clean.items() if current[k] != v}
        db.set_settings(clean)
        if clean:
            log.info(f"Settings changed: {', '.join(sorted(clean))}")
        worker.request_scan()
        return public_settings()

    @app.get("/api/defaults")
    def defaults():
        return DEFAULTS

    @app.get("/api/browse")
    def browse(path: str = ""):
        roots = config.media_roots()
        if not path:
            return {"path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in roots]}
        try:
            real = paths.safe_path(path, roots)
        except paths.PathError as e:
            raise HTTPException(400, str(e))
        try:
            names = sorted(n for n in os.listdir(real)
                           if not n.startswith(".") and os.path.isdir(os.path.join(real, n)))
        except OSError as e:
            raise HTTPException(400, str(e))
        parent = None if real in roots else os.path.dirname(real)
        return {"path": real, "parent": parent if parent is not None else "",
                "dirs": [{"name": n, "path": os.path.join(real, n)} for n in names]}

    @app.get("/api/files")
    def files(status: str = "todo", limit: int = 300):
        s = db.settings()
        if status == "todo":
            rows = db.queue(s["sort"], limit=limit)
        else:
            rows = db.q("SELECT * FROM files WHERE status = ? ORDER BY size DESC LIMIT ?", (status, limit))
            for r in rows:
                r.pop("probe", None)
        out = []
        for r in rows:
            p = r.pop("probe", None) or {}
            out.append(dict(r, display=paths.display(r["path"]), codec=p.get("codec", ""),
                            duration=p.get("duration", 0), tier=encoder.tier(r["path"]),
                            quality=encoder.quality_for(r["path"], s)))
        return out

    @app.get("/api/history")
    def history(limit: int = 200):
        return [dict(h, display=paths.display(h["path"])) for h in db.history(limit)]

    @app.post("/api/retry")
    async def retry(request: Request):
        body = await request.json() if await request.body() else {}
        path = body.get("path")
        if path:
            n = db.x("UPDATE files SET status='todo', failures=0, reason='' WHERE path=? AND status IN "
                     "('failed','no_savings')", (paths.safe_path(path),))
        else:
            n = db.x("UPDATE files SET status='todo', failures=0, reason='' WHERE status='failed'")
        log.info(f"Retry: {n} file(s) back in the queue")
        return {"reset": n}

    @app.post("/api/scan")
    def scan():
        worker.request_scan()
        return {"ok": True}

    @app.post("/api/run")
    def run():
        worker.run_now()
        return {"ok": True}

    @app.post("/api/stop")
    def stop():
        worker.stop()
        return {"ok": True}

    @app.get("/api/logs")
    def logs():
        return {"lines": list(logmod.recent)}

    @app.exception_handler(paths.PathError)
    async def path_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app
