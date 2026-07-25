import os
import sqlite3
from pathlib import Path

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).parent / "data" / "app.db"

# Программалар мен потоктар — Юз40-тың нақты бағыттары мен потоктары. Жаңа поток
# ашылғанда осы тізімге қосу жеткілікті (немесе бөлім бетіндегі "+ жаңа поток" пішіні
# арқылы қолмен қосуға да болады).
DEFAULT_PROGRAMS = [
    (
        "smart",
        "Smart",
        1,
        ["Т-01", "Т-11", "Т-21", "Т-31", "Т-41", "Т-51", "Т-61", "Т-71", "Т-81", "Т-91", "Т-101"],
    ),
    (
        "junior",
        "Junior",
        2,
        ["J-01", "J-11", "J-21", "J-31", "J-41", "J-51", "J-61", "J-71", "J-81", "J-91", "J-101"],
    ),
    (
        "smart_pro",
        "Smart Pro",
        3,
        ["Шілде", "Тамыз", "Қыркүйек", "Қазан"],
    ),
]

# Постгрес (Neon/Vercel) диалектісі
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS programs (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS streams (
    id SERIAL PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(program_id, code)
);

CREATE TABLE IF NOT EXISTS weeks (
    id SERIAL PRIMARY KEY,
    stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_streams_program ON streams(program_id);
CREATE INDEX IF NOT EXISTS idx_results_week ON results(week_id);
CREATE INDEX IF NOT EXISTS idx_notes_week ON curator_notes(week_id);
CREATE INDEX IF NOT EXISTS idx_imports_week ON imports(week_id);
"""

# SQLite диалектісі — DATABASE_URL қойылмаған кезде локальді дамыту үшін
# (нөлдік баптаумен preview көру мақсатында; Postgres 3.35+ RETURNING-мен
# сәйкес келу үшін SQLite 3.35+ қажет)
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(program_id, code)
);

CREATE TABLE IF NOT EXISTS weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
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

CREATE INDEX IF NOT EXISTS idx_streams_program ON streams(program_id);
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


def _column_exists(conn, table, column):
    if getattr(conn, "backend", None) == "postgres":
        row = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND column_name = ?",
            (table, column),
        ).fetchone()
        return row is not None
    row = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in row)


def _migrate(conn):
    """Осыдан бұрын құрылған дерекқорларда жоқ бағандарды қосады (мыс. streams
    кестесі енгізілгенге дейін жасалған weeks кестесіне stream_id қосу)."""
    if not _column_exists(conn, "weeks", "stream_id"):
        conn.execute(
            "ALTER TABLE weeks ADD COLUMN stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE"
        )
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weeks_stream ON weeks(stream_id)")
    conn.commit()


def _seed_defaults(conn):
    for slug, name, order, stream_codes in DEFAULT_PROGRAMS:
        row = conn.execute("SELECT id FROM programs WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            program_id = conn.execute(
                "INSERT INTO programs (slug, name, sort_order) VALUES (?, ?, ?) RETURNING id",
                (slug, name, order),
            ).fetchone()["id"]
        else:
            program_id = row["id"]

        for i, code in enumerate(stream_codes):
            existing = conn.execute(
                "SELECT id FROM streams WHERE program_id = ? AND code = ?", (program_id, code)
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO streams (program_id, code, sort_order) VALUES (?, ?, ?)",
                    (program_id, code, i),
                )
    conn.commit()


def init_db():
    conn = get_connection()
    schema = SCHEMA_PG if getattr(conn, "backend", None) == "postgres" else SCHEMA_SQLITE
    conn.executescript(schema)
    conn.commit()
    _migrate(conn)
    _seed_defaults(conn)
    conn.close()
