"""Database access layer.

PostgreSQL (Amazon RDS) when DB_HOST is set, SQLite otherwise (local development).
SQL is written with %s placeholders and translated for SQLite.
Timestamps are stored as fixed-format UTC ISO-8601 text so one schema works in both.
One connection and one transaction per request: the handler commits or rolls back.
"""
import os
import sqlite3
import threading
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

_local = threading.local()
_init_lock = threading.Lock()
_init_key = None


def using_postgres():
    return bool(os.environ.get("DB_HOST"))


def _connect():
    if using_postgres():
        import pg8000.dbapi

        return pg8000.dbapi.connect(
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "5432")),
            database=os.environ.get("DB_NAME", "foodwaste"),
            user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
            timeout=10,
        )
    path = os.environ.get("SQLITE_PATH", "local_data/foodshare.db")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        c = _connect()
        _local.conn = c
    return c


def commit():
    c = getattr(_local, "conn", None)
    if c is not None:
        c.commit()


def rollback():
    c = getattr(_local, "conn", None)
    if c is not None:
        try:
            c.rollback()
        except Exception:  # connection may already be broken
            pass


def close():
    c = getattr(_local, "conn", None)
    _local.conn = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _adapt(sql):
    return sql if using_postgres() else sql.replace("%s", "?")


def query(sql, params=()):
    cur = conn().cursor()
    try:
        cur.execute(_adapt(sql), tuple(params))
        cols = [d[0] if isinstance(d[0], str) else d[0].decode() for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def execute(sql, params=()):
    """Run a write statement and return the number of affected rows."""
    cur = conn().cursor()
    try:
        cur.execute(_adapt(sql), tuple(params))
        return cur.rowcount
    finally:
        cur.close()


def _statements():
    text = SCHEMA_PATH.read_text()
    return [s.strip() for s in text.split(";") if s.strip()]


def init():
    """Create tables if they do not exist (idempotent, once per container)."""
    global _init_key
    key = os.environ.get("DB_HOST") or os.environ.get("SQLITE_PATH") or "local"
    if _init_key == key:
        return
    with _init_lock:
        if _init_key == key:
            return
        c = conn()
        cur = c.cursor()
        try:
            if using_postgres():
                cur.execute("SELECT pg_advisory_xact_lock(727274)")
            for stmt in _statements():
                cur.execute(stmt)
        finally:
            cur.close()
        c.commit()
        _init_key = key
