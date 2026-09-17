"""Local SQLite store. Notes, lists and reminders never leave this machine."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("jarvis.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    body      TEXT NOT NULL,
    created   REAL NOT NULL,
    device    TEXT
);
CREATE TABLE IF NOT EXISTS list_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    list_name TEXT NOT NULL,
    item      TEXT NOT NULL,
    done      INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_list_items_name ON list_items(list_name, done);
CREATE TABLE IF NOT EXISTS reminders (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    body      TEXT NOT NULL,
    due       REAL NOT NULL,
    device    TEXT,
    fired     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due, fired);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    log.info("sqlite ready at %s", path)
    return conn
