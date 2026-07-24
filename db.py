import os
import sqlite3
from pathlib import Path

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).parent / "data" / "app.db"

# Постгрес (Neon/Vercel) диалектісі
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS weeks (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    passing_score REAL,
    max_score REAL DEFAULT 140,
    summary TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS imports (
    id SERIAL PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    sheet_url TEXT,
    curator TEXT,
    row_count INTEGER,
    skipped_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS results (
    id SERIAL PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    curator TEXT,
    student TEXT,
    subject TEXT,
    topic TEXT,
    score REAL,
    max_score REAL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS curator_notes (
    id SERIAL PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    curator TEXT NOT NULL,
    max_score_reasons TEXT,
    mistaken_topics TEXT,
    prep_factors TEXT,
    low_score_reasons TEXT,
    general_comment TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_results_week ON results(week_id);
CREATE INDEX IF NOT EXISTS idx_notes_week ON curator_notes(week_id);
CREATE INDEX IF NOT EXISTS idx_imports_week ON imports(week_id);
"""

# SQLite диалектісі — DATABASE_URL қойылмаған кезде локальді дамыту үшін
# (нөлдік баптаумен preview көру мақсатында; Postgres 3.35+ RETURNING-мен
# сәйкес келу үшін SQLite 3.35+ қажет)
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    passing_score REAL,
    max_score REAL DEFAULT 140,
    summary TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    sheet_url TEXT,
    curator TEXT,
    row_count INTEGER,
    skipped_count INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    import_id INTEGER REFERENCES imports(id) ON DELETE SET NULL,
    curator TEXT,
    student TEXT,
    subject TEXT,
    topic TEXT,
    score REAL,
    max_score REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS curator_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    curator TEXT NOT NULL,
    max_score_reasons TEXT,
    mistaken_topics TEXT,
    prep_factors TEXT,
    low_score_reasons TEXT,
    general_comment TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_results_week ON results(week_id);
CREATE INDEX IF NOT EXISTS idx_notes_week ON curator_notes(week_id);
CREATE INDEX IF NOT EXISTS idx_imports_week ON imports(week_id);
"""


class PgConnection:
    """psycopg2 connection-ны sqlite3.Connection интерфейсіне ұқсас етіп қайтарады:
    conn.execute(sql, params).fetchone()/.fetchall() тікелей жұмыс істейді."""

    backend = "postgres"

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def executescript(self, sql):
        cur = self._conn.cursor()
        cur.execute(sql)

    def cursor(self):
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _get_pg_connection(database_url):
    return PgConnection(psycopg2.connect(database_url))


class SQLiteConnection(sqlite3.Connection):
    """sqlite3.Connection жалғыз айырмашылығы — backend белгісі бар, дизаетчерге
    (get_connection/init_db) қай схема қолданылатынын білдіру үшін."""

    backend = "sqlite"


def _get_sqlite_connection():
    SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SQLITE_PATH, factory=SQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        return _get_pg_connection(database_url)
    return _get_sqlite_connection()


def init_db():
    conn = get_connection()
    schema = SCHEMA_PG if getattr(conn, "backend", None) == "postgres" else SCHEMA_SQLITE
    conn.executescript(schema)
    conn.commit()
    conn.close()
