import os

import psycopg2
import psycopg2.extras

SCHEMA = """
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


class Connection:
    """psycopg2 connection-ны sqlite3.Connection интерфейсіне ұқсас етіп қайтарады:
    conn.execute(sql, params).fetchone()/.fetchall() тікелей жұмыс істейді."""

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


def get_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL орнатылмаған. Postgres байланысу жолын "
            "(мыс. Neon-нан алған 'postgres://...') ортаңыз айнымалысына қойыңыз."
        )
    pg_conn = psycopg2.connect(database_url)
    return Connection(pg_conn)


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
