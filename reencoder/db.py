"""SQLite state: settings, file index, job history. One connection guarded by a lock."""
import json
import sqlite3
import threading
import time

from .settings import DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    status TEXT NOT NULL,          -- todo | running | done | ok | skipped | failed | no_savings
    reason TEXT DEFAULT '',
    failures INTEGER DEFAULT 0,
    probe TEXT,                    -- JSON summary from media.summarize()
    updated REAL
);
CREATE INDEX IF NOT EXISTS files_status ON files(status);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT, new_path TEXT,
    started REAL, finished REAL,
    orig_size INTEGER, new_size INTEGER, saved INTEGER DEFAULT 0,
    media_sec REAL, result TEXT, message TEXT
);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(history)")}
            if "active_sec" not in cols:
                self.conn.execute("ALTER TABLE history ADD COLUMN active_sec REAL")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).rowcount

    def many(self, sql, rows):
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                self.conn.executemany(sql, rows)
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    # Settings
    def settings(self):
        out = dict(DEFAULTS)
        for r in self.q("SELECT key, value FROM settings"):
            if r["key"] in DEFAULTS:
                out[r["key"]] = json.loads(r["value"])
        return out

    def set_settings(self, values):
        self.many("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                  [(k, json.dumps(v)) for k, v in values.items()])

    # Files
    def file(self, path):
        rows = self.q("SELECT * FROM files WHERE path = ?", (path,))
        if not rows:
            return None
        row = rows[0]
        row["probe"] = json.loads(row["probe"]) if row["probe"] else None
        return row

    def all_files(self):
        rows = self.q("SELECT * FROM files")
        for r in rows:
            r["probe"] = json.loads(r["probe"]) if r["probe"] else None
        return {r["path"]: r for r in rows}

    def put_files(self, rows):
        self.many(
            "INSERT OR REPLACE INTO files(path, size, mtime, status, reason, failures, probe, updated) "
            "VALUES (:path, :size, :mtime, :status, :reason, :failures, :probe, :updated)",
            [dict(r, probe=json.dumps(r["probe"]) if r.get("probe") else None, updated=time.time())
             for r in rows])

    def set_status(self, path, status, reason="", failures=None):
        if failures is None:
            self.x("UPDATE files SET status=?, reason=?, updated=? WHERE path=?",
                   (status, reason, time.time(), path))
        else:
            self.x("UPDATE files SET status=?, reason=?, failures=?, updated=? WHERE path=?",
                   (status, reason, failures, time.time(), path))

    def delete_files(self, paths):
        self.many("DELETE FROM files WHERE path = ?", [(p,) for p in paths])

    def counts(self):
        c = {r["status"]: r["n"] for r in self.q("SELECT status, COUNT(*) n FROM files GROUP BY status")}
        return {
            "total": sum(c.values()),
            "done": c.get("done", 0) + c.get("ok", 0),
            "todo": c.get("todo", 0) + c.get("running", 0),
            "failed": c.get("failed", 0),
            "skipped": c.get("skipped", 0) + c.get("no_savings", 0),
        }

    def queue(self, sort, limit=None):
        order = {"size_desc": "size DESC", "size_asc": "size ASC", "path": "path ASC"}[sort]
        sql = f"SELECT * FROM files WHERE status = 'todo' ORDER BY failures ASC, {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self.q(sql)
        for r in rows:
            r["probe"] = json.loads(r["probe"]) if r["probe"] else None
        return rows

    # History
    def add_history(self, **h):
        cols = ", ".join(h)
        self.x(f"INSERT INTO history({cols}) VALUES ({', '.join('?' * len(h))})", tuple(h.values()))

    def history(self, limit=200):
        return self.q("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,))

    def total_saved(self):
        return self.q("SELECT COALESCE(SUM(saved), 0) s FROM history WHERE result = 'done'")[0]["s"]

    def recent_speeds(self, n=10):
        """(media seconds, encoding seconds without pauses) of the last n successful encodes."""
        return [(r["media_sec"], r["active_sec"] or r["finished"] - r["started"]) for r in self.q(
            "SELECT media_sec, started, finished, active_sec FROM history WHERE result='done' AND media_sec > 0 "
            "ORDER BY id DESC LIMIT ?", (n,))]
