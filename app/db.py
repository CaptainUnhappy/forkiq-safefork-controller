"""SQLite storage for ForkIQ."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS repositories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  node_id TEXT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  upstream_full_name TEXT,
  branch TEXT NOT NULL DEFAULT 'main',
  mode TEXT NOT NULL DEFAULT 'mirror',
  enabled INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'pending',
  upstream_sha TEXT,
  fork_sha TEXT,
  last_success_at TEXT,
  last_check_at TEXT,
  last_error TEXT,
  diverged_at TEXT,
  diverged_upstream_sha TEXT,
  last_merged_upstream_sha TEXT,
  self_update TEXT,
  self_update_interval INTEGER,
  actions_disabled INTEGER,
  discovered INTEGER NOT NULL DEFAULT 0,
  is_archived INTEGER NOT NULL DEFAULT 0,
  sync_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_id INTEGER,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path):
        self.path = str(path)
        self.init()

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self):
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)
            # Additive migrations for databases created before these columns.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(repositories)")}
            if "mode" not in columns:
                conn.execute("ALTER TABLE repositories ADD COLUMN mode TEXT NOT NULL DEFAULT 'mirror'")
            if "last_merged_upstream_sha" not in columns:
                conn.execute("ALTER TABLE repositories ADD COLUMN last_merged_upstream_sha TEXT")
            if "self_update" not in columns:
                conn.execute("ALTER TABLE repositories ADD COLUMN self_update TEXT")
            if "self_update_interval" not in columns:
                conn.execute("ALTER TABLE repositories ADD COLUMN self_update_interval INTEGER")
            conn.commit()

    def run(self, sql, params=()):
        with closing(self.connect()) as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur

    def one(self, sql, params=()):
        with closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        with closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchall()

    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def log_event(db, repo_id, kind, message):
    db.run("INSERT INTO events(repo_id, at, kind, message) VALUES(?,?,?,?)",
           (repo_id, utcnow(), kind, message))


def insert_repository(db, *, owner, name, full_name, upstream_full_name, branch="main",
                      node_id=None, status="pending", discovered=0, is_archived=0):
    now = utcnow()
    cur = db.run(
        "INSERT INTO repositories(owner,name,full_name,upstream_full_name,branch,node_id,"
        "status,discovered,is_archived,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (owner, name, full_name, upstream_full_name, branch, node_id, status,
         discovered, is_archived, now, now))
    return cur.lastrowid
