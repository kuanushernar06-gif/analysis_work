import os
import sqlite3
from pathlib import Path

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).parent / "data" / "app.db"

# Программалар мен потоктар — Юз40-тың нақты бағыттары мен потоктары. Жаңа поток
# ашылғанда осы тізімге қосу жеткілікті.
DEFAULT_PROGRAMS = [
    (
        "smart",
        "Smart",
        1,
        [
            "ТАРИХ-01", "ТАРИХ-11", "ТАРИХ-21", "ТАРИХ-31", "ТАРИХ-41",
            "ТАРИХ-51", "ТАРИХ-61", "ТАРИХ-71", "ТАРИХ-81", "ТАРИХ-91", "ТАРИХ-101",
        ],
    ),
    (
        "junior",
        "Junior",
        2,
        [
            "JUNIOR-01", "JUNIOR-11", "JUNIOR-21", "JUNIOR-31", "JUNIOR-41",
            "JUNIOR-51", "JUNIOR-61", "JUNIOR-71", "JUNIOR-81", "JUNIOR-91", "JUNIOR-101",
        ],
    ),
]

# Бағдарлама бойынша курс ұзақтығы (ай саны); әр ай 4 аптадан тұрады.
PROGRAM_MONTHS = {"smart": 5, "junior": 5}
WEEKS_PER_MONTH = 4

# АЙЛЫҚ ТЕСТ АНАЛИЗ бен ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ АНАЛИЗ санаттарында апта
# бөлінісі жоқ — әр айға бір ғана жазба.
CATEGORY_WEEKS_PER_MONTH_OVERRIDE = {"aylyq_test": 1, "baiqau_test": 1}

# Бағдарлама бойынша бекітілген СТ максимум баллы мен мақсат баллы — бұрын
# әр рейтинг қосқан сайын қолмен енгізілетін, енді осында бір рет бекітіліп,
# барлық ағын мен аптаға автоматты қолданылады.
PROGRAM_MAX_SCORE = {"smart": 15, "junior": 10}
PROGRAM_TARGET_SCORE = {"smart": 12, "junior": 7}

# АЙЛЫҚ ТЕСТ АНАЛИЗ бен ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ АНАЛИЗ санаттарында мақсат
# балл жоқ, ал максимум балл бағдарламаға қарамастан бірдей (30) — САБАҚ
# ТАПСЫРУ АНАЛИЗ-дан өзгеше бекітілген үлгі.
CATEGORY_MAX_SCORE_OVERRIDE = {"aylyq_test": 30, "baiqau_test": 30}
CATEGORY_TARGET_SCORE_OVERRIDE = {"aylyq_test": None, "baiqau_test": None}


def score_defaults_for(program_slug, category_slug):
    """Санат бойынша ерекше бекітілген мән болса соны, әйтпесе бағдарлама
    бойынша PROGRAM_MAX_SCORE/PROGRAM_TARGET_SCORE мәнін қайтарады."""
    if category_slug in CATEGORY_MAX_SCORE_OVERRIDE:
        return CATEGORY_MAX_SCORE_OVERRIDE[category_slug], CATEGORY_TARGET_SCORE_OVERRIDE.get(category_slug)
    return PROGRAM_MAX_SCORE.get(program_slug), PROGRAM_TARGET_SCORE.get(program_slug)

# Талдау санаттары — әр бағдарламаның потоктары осы санаттың әрқайсысында
# бөлек (тәуелсіз апталары/импорттары бар) жазба ретінде қайталанады.
CATEGORIES = [
    ("sabaq_tapsyru", "САБАҚ ТАПСЫРУ АНАЛИЗ", 1),
    ("aylyq_test", "АЙЛЫҚ ТЕСТ АНАЛИЗ", 2),
    ("baiqau_test", "ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ АНАЛИЗ", 3),
]
CATEGORY_LABELS = {slug: label for slug, label, _ in CATEGORIES}
DEFAULT_CATEGORY = CATEGORIES[0][0]

# Сайттың сол жақ мәзіріндегі (sidebar) санат сілтемелерінің мәтіні — баған
# тақырыптарынан (CATEGORY_LABELS) сәл өзгеше, дәл пайдаланушы сұраған түрде.
CATEGORY_NAV_LABELS = {
    "sabaq_tapsyru": "САБАҚ ТАПСЫРУ АНАЛИЗІ",
    "aylyq_test": "АЙЛЫҚ ТЕСТ АНАЛИЗІ",
    "baiqau_test": "ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ АНАЛИЗІ",
}
SIDEBAR_CATEGORIES = [(slug, CATEGORY_NAV_LABELS[slug]) for slug, _label, _order in CATEGORIES]

# Мұғалімдер режимінде санат атаулары "АНАЛИЗ" емес "МҰҒАЛІМДЕРІ" деп
# аталады (бағдарлама/ағын таңдау экрандарында және мұғалімдер сайдбарында).
CATEGORY_STATS_LABELS = {
    "sabaq_tapsyru": "САБАҚ ТАПСЫРУ МҰҒАЛІМДЕРІ",
    "aylyq_test": "АЙЛЫҚ ТЕСТ МҰҒАЛІМДЕРІ",
    "baiqau_test": "ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ МҰҒАЛІМДЕРІ",
}

# Постгрес (Neon/Vercel) диалектісі
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS programs (
    id SERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    plan_doc_url TEXT,
    plan_doc_fetch_error TEXT,
    material_plan_url TEXT,
    material_plan_text TEXT,
    material_plan_fetch_error TEXT
);

CREATE TABLE IF NOT EXISTS streams (
    id SERIAL PRIMARY KEY,
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'sabaq_tapsyru',
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(program_id, category, code)
);

CREATE TABLE IF NOT EXISTS weeks (
    id SERIAL PRIMARY KEY,
    stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
    month_number INTEGER,
    week_number INTEGER,
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    passing_score REAL,
    max_score REAL DEFAULT 140,
    gold_threshold REAL,
    silver_threshold REAL,
    target_score REAL,
    summary TEXT,
    curators_doc_url TEXT,
    curators_doc_text TEXT,
    curators_doc_fetch_error TEXT,
    curators_analysis_json TEXT,
    curators_analysis_error TEXT,
    plan_text TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS imports (
    id SERIAL PRIMARY KEY,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    sheet_url TEXT,
    curator TEXT,
    sheet_count INTEGER,
    row_count INTEGER,
    skipped_count INTEGER,
    target_score REAL,
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
    doc_url TEXT,
    doc_text TEXT,
    fetch_error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teachers (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teacher_curators (
    id SERIAL PRIMARY KEY,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    curator_name TEXT NOT NULL,
    UNIQUE(curator_name)
);

CREATE INDEX IF NOT EXISTS idx_streams_program ON streams(program_id);
CREATE INDEX IF NOT EXISTS idx_results_week ON results(week_id);
CREATE INDEX IF NOT EXISTS idx_notes_week ON curator_notes(week_id);
CREATE INDEX IF NOT EXISTS idx_imports_week ON imports(week_id);
CREATE INDEX IF NOT EXISTS idx_teacher_curators_teacher ON teacher_curators(teacher_id);
"""

# SQLite диалектісі — DATABASE_URL қойылмаған кезде локальді дамыту үшін
# (нөлдік баптаумен preview көру мақсатында; Postgres 3.35+ RETURNING-мен
# сәйкес келу үшін SQLite 3.35+ қажет)
SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS programs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0,
    plan_doc_url TEXT,
    plan_doc_fetch_error TEXT,
    material_plan_url TEXT,
    material_plan_text TEXT,
    material_plan_fetch_error TEXT
);

CREATE TABLE IF NOT EXISTS streams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
    code TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'sabaq_tapsyru',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(program_id, category, code)
);

CREATE TABLE IF NOT EXISTS weeks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE,
    month_number INTEGER,
    week_number INTEGER,
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    passing_score REAL,
    max_score REAL DEFAULT 140,
    gold_threshold REAL,
    silver_threshold REAL,
    target_score REAL,
    summary TEXT,
    curators_doc_url TEXT,
    curators_doc_text TEXT,
    curators_doc_fetch_error TEXT,
    curators_analysis_json TEXT,
    curators_analysis_error TEXT,
    plan_text TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_id INTEGER NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
    sheet_url TEXT,
    curator TEXT,
    sheet_count INTEGER,
    row_count INTEGER,
    skipped_count INTEGER,
    target_score REAL,
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
    doc_url TEXT,
    doc_text TEXT,
    fetch_error TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS teacher_curators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    curator_name TEXT NOT NULL,
    UNIQUE(curator_name)
);

CREATE INDEX IF NOT EXISTS idx_streams_program ON streams(program_id);
CREATE INDEX IF NOT EXISTS idx_results_week ON results(week_id);
CREATE INDEX IF NOT EXISTS idx_notes_week ON curator_notes(week_id);
CREATE INDEX IF NOT EXISTS idx_imports_week ON imports(week_id);
CREATE INDEX IF NOT EXISTS idx_teacher_curators_teacher ON teacher_curators(teacher_id);
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

    def rollback(self):
        self._conn.rollback()

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


def _migrate_drop_material_check_tables(conn):
    """Материал тексеру функциясы толығымен алып тасталды — оның бес
    кестесін (тәуелділік ретімен, FK бұзылмас үшін) ескі дерекқорлардан
    да іс жүзінде өшіреді. Кестелер жоқ болса, IF EXISTS арқасында
    қауіпсіз ешнәрсе жасамайды — бірнеше рет қайталап іске қосу қауіпсіз."""
    for table in ("material_check_runs", "book_chunks", "book_topic_pages", "books", "material_checks"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()


def _migrate(conn):
    """Осыдан бұрын құрылған дерекқорларда жоқ бағандарды қосады (мыс. streams
    кестесі енгізілгенге дейін жасалған weeks кестесіне stream_id қосу)."""
    if not _column_exists(conn, "weeks", "stream_id"):
        conn.execute(
            "ALTER TABLE weeks ADD COLUMN stream_id INTEGER REFERENCES streams(id) ON DELETE CASCADE"
        )
        conn.commit()
    if not _column_exists(conn, "weeks", "month_number"):
        conn.execute("ALTER TABLE weeks ADD COLUMN month_number INTEGER")
        conn.commit()
    if not _column_exists(conn, "weeks", "week_number"):
        conn.execute("ALTER TABLE weeks ADD COLUMN week_number INTEGER")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_weeks_stream ON weeks(stream_id)")
    conn.commit()
    if not _column_exists(conn, "weeks", "curators_doc_url"):
        conn.execute("ALTER TABLE weeks ADD COLUMN curators_doc_url TEXT")
        conn.commit()
    if not _column_exists(conn, "weeks", "curators_doc_text"):
        conn.execute("ALTER TABLE weeks ADD COLUMN curators_doc_text TEXT")
        conn.commit()
    if not _column_exists(conn, "weeks", "curators_doc_fetch_error"):
        conn.execute("ALTER TABLE weeks ADD COLUMN curators_doc_fetch_error TEXT")
        conn.commit()
    if not _column_exists(conn, "weeks", "curators_analysis_json"):
        conn.execute("ALTER TABLE weeks ADD COLUMN curators_analysis_json TEXT")
        conn.commit()
    if not _column_exists(conn, "weeks", "curators_analysis_error"):
        conn.execute("ALTER TABLE weeks ADD COLUMN curators_analysis_error TEXT")
        conn.commit()
    if not _column_exists(conn, "imports", "sheet_count"):
        conn.execute("ALTER TABLE imports ADD COLUMN sheet_count INTEGER")
        conn.commit()
    if not _column_exists(conn, "weeks", "gold_threshold"):
        conn.execute("ALTER TABLE weeks ADD COLUMN gold_threshold REAL")
        conn.commit()
    if not _column_exists(conn, "weeks", "silver_threshold"):
        conn.execute("ALTER TABLE weeks ADD COLUMN silver_threshold REAL")
        conn.commit()
    if not _column_exists(conn, "imports", "target_score"):
        conn.execute("ALTER TABLE imports ADD COLUMN target_score REAL")
        conn.commit()
    if not _column_exists(conn, "weeks", "target_score"):
        conn.execute("ALTER TABLE weeks ADD COLUMN target_score REAL")
        conn.commit()
    if not _column_exists(conn, "weeks", "plan_text"):
        conn.execute("ALTER TABLE weeks ADD COLUMN plan_text TEXT")
        conn.commit()
    if not _column_exists(conn, "programs", "plan_doc_url"):
        conn.execute("ALTER TABLE programs ADD COLUMN plan_doc_url TEXT")
        conn.commit()
    if not _column_exists(conn, "programs", "plan_doc_fetch_error"):
        conn.execute("ALTER TABLE programs ADD COLUMN plan_doc_fetch_error TEXT")
        conn.commit()
    if not _column_exists(conn, "teachers", "stream_id"):
        conn.execute("ALTER TABLE teachers ADD COLUMN stream_id INTEGER REFERENCES streams(id)")
        conn.commit()
    if not _column_exists(conn, "programs", "material_plan_url"):
        conn.execute("ALTER TABLE programs ADD COLUMN material_plan_url TEXT")
        conn.commit()
    if not _column_exists(conn, "programs", "material_plan_text"):
        conn.execute("ALTER TABLE programs ADD COLUMN material_plan_text TEXT")
        conn.commit()
    if not _column_exists(conn, "programs", "material_plan_fetch_error"):
        conn.execute("ALTER TABLE programs ADD COLUMN material_plan_fetch_error TEXT")
        conn.commit()
    _migrate_drop_material_check_tables(conn)
    if not _column_exists(conn, "streams", "category"):
        _migrate_stream_categories(conn)
    _migrate_stream_code_rename(conn)
    _migrate_month_summary_titles(conn)
    _migrate_program_score_defaults(conn)
    _migrate_aylyq_test_monthly(conn)


_STREAM_CODE_RENAMES = {
    "Т-01": "ТАРИХ-01", "Т-11": "ТАРИХ-11", "Т-21": "ТАРИХ-21", "Т-31": "ТАРИХ-31",
    "Т-41": "ТАРИХ-41", "Т-51": "ТАРИХ-51", "Т-61": "ТАРИХ-61", "Т-71": "ТАРИХ-71",
    "Т-81": "ТАРИХ-81", "Т-91": "ТАРИХ-91", "Т-101": "ТАРИХ-101",
    "J-01": "JUNIOR-01", "J-11": "JUNIOR-11", "J-21": "JUNIOR-21", "J-31": "JUNIOR-31",
    "J-41": "JUNIOR-41", "J-51": "JUNIOR-51", "J-61": "JUNIOR-61", "J-71": "JUNIOR-71",
    "J-81": "JUNIOR-81", "J-91": "JUNIOR-91", "J-101": "JUNIOR-101",
}


def _migrate_stream_code_rename(conn):
    """Ертеректе 'Т-01'/'J-01' түріндегі қысқа кодтармен құрылған потоктарды
    'ТАРИХ-01'/'JUNIOR-01' түріндегі толық атауларға қайта атайды — бір рет
    қана, әр ескі кодқа жеке SELECT+UPDATE арқылы, деректің қалғанын
    (аптасын, нәтижесін) мүлде қозғамай."""
    for old_code, new_code in _STREAM_CODE_RENAMES.items():
        conn.execute("UPDATE streams SET code = ? WHERE code = ?", (new_code, old_code))
    conn.commit()


def _migrate_month_summary_titles(conn):
    """САБАҚ ТАПСЫРУ АНАЛИЗ санатындағы әр айдың соңғы (WEEKS_PER_MONTH-ші)
    аптасының атауын 'N-АЙ M-АПТА'-дан 'N-АЙ ОРТАҚ'-қа қайта атайды — бұл
    апта енді бөлек СТ емес, сол айдың алдыңғы апталарының нәтижесі мен
    анализін біріктіретін тор. Әрдайым дұрыс мәнге қайта қойғандықтан
    (шартсыз UPDATE), бірнеше рет қайталап іске қосу қауіпсіз."""
    conn.execute(
        "UPDATE weeks SET title = month_number || '-АЙ ОРТАҚ' "
        "WHERE week_number = ? AND month_number IS NOT NULL AND stream_id IN "
        "(SELECT id FROM streams WHERE category = ?)",
        (WEEKS_PER_MONTH, DEFAULT_CATEGORY),
    )
    conn.commit()


def _migrate_program_score_defaults(conn):
    """Smart/Junior бағдарламаларының СТ максимум баллы мен мақсат баллын
    бекітеді (score_defaults_for — PROGRAM_MAX_SCORE/PROGRAM_TARGET_SCORE,
    санат бойынша CATEGORY_MAX_SCORE_OVERRIDE/CATEGORY_TARGET_SCORE_OVERRIDE
    ерекшеленеді) — бұрын әр рейтинг қосқанда қолмен енгізілетін, енді барлық
    ағын мен аптаға бірдей, автоматты қолданылады. Әрдайым дұрыс мәнге қайта
    қойғандықтан (шартсыз UPDATE), бірнеше рет қайталап іске қосу қауіпсіз."""
    for slug in PROGRAM_MAX_SCORE:
        for category_slug, _label, _order in CATEGORIES:
            max_score, target_score = score_defaults_for(slug, category_slug)
            conn.execute(
                "UPDATE weeks SET max_score = ?, target_score = ? WHERE stream_id IN "
                "(SELECT s.id FROM streams s JOIN programs p ON p.id = s.program_id "
                " WHERE p.slug = ? AND s.category = ?)",
                (max_score, target_score, slug, category_slug),
            )
    conn.commit()


def _migrate_aylyq_test_monthly(conn):
    """АЙЛЫҚ ТЕСТ АНАЛИЗ санатында апта бөлінісі жоқ — әр айға бір ғана жазба
    ('N-АЙ'). Бұрын осы санатта да сабақ тапсыру анализі секілді 4 аптадан
    құрылған тор болатын; енді ол құрылым 1-айлық жазбаға қысқарады. Бұл
    санатта нақты дерек (нәтиже/импорт/куратор құжаты) әлі жоқ кезде ғана
    қауіпсіз, сондықтан week_number > 1 (ескі құрылым) қалғанда ғана бір рет
    іске қосылады — 1-айлық жазбаға көшкен соң бұл шарт бұдан былай орындалмайды."""
    old_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM weeks w JOIN streams s ON s.id = w.stream_id "
        "WHERE s.category = 'aylyq_test' AND w.week_number > 1"
    ).fetchone()["c"]
    if not old_rows:
        return
    conn.execute(
        "DELETE FROM weeks WHERE stream_id IN (SELECT id FROM streams WHERE category = 'aylyq_test')"
    )
    conn.commit()


def _migrate_stream_categories(conn):
    """streams кестесіне 'category' бағанын қосады және бірегейлік шектеуін
    UNIQUE(program_id, code)-тен UNIQUE(program_id, category, code)-ке
    кеңейтеді — сол арқылы бір потоктың коды (мыс. 'Т-01') әр санатта
    (сабақ тапсыру/айлық тест/байқау тест/live сабақ) бөлек, тәуелсіз жазба
    ретінде қайталана алады. Бар барлық жолдар автоматты түрде
    DEFAULT_CATEGORY-ға тіркеледі — ешбір нақты дерек жоғалмайды/көшірілмейді."""
    if getattr(conn, "backend", None) == "postgres":
        conn.execute(
            f"ALTER TABLE streams ADD COLUMN category TEXT NOT NULL DEFAULT '{DEFAULT_CATEGORY}'"
        )
        constraint_row = conn.execute(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_name = kcu.table_name
            WHERE tc.table_name = 'streams' AND tc.constraint_type = 'UNIQUE'
            GROUP BY tc.constraint_name
            HAVING array_agg(kcu.column_name::text ORDER BY kcu.ordinal_position) = ARRAY['program_id','code']
            """
        ).fetchone()
        if constraint_row:
            conn.execute(f'ALTER TABLE streams DROP CONSTRAINT "{constraint_row["constraint_name"]}"')
        conn.execute(
            "ALTER TABLE streams ADD CONSTRAINT streams_program_category_code_key "
            "UNIQUE (program_id, category, code)"
        )
        conn.commit()
    else:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """
            CREATE TABLE streams_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'sabaq_tapsyru',
                sort_order INTEGER DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(program_id, category, code)
            )
            """
        )
        conn.execute(
            f"""
            INSERT INTO streams_new (id, program_id, code, category, sort_order, created_at)
            SELECT id, program_id, code, '{DEFAULT_CATEGORY}', sort_order, created_at FROM streams
            """
        )
        conn.execute("DROP TABLE streams")
        conn.execute("ALTER TABLE streams_new RENAME TO streams")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_streams_program ON streams(program_id)")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


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

        for category_slug, _label, _corder in CATEGORIES:
            for i, code in enumerate(stream_codes):
                existing = conn.execute(
                    "SELECT id FROM streams WHERE program_id = ? AND category = ? AND code = ?",
                    (program_id, category_slug, code),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO streams (program_id, category, code, sort_order) VALUES (?, ?, ?, ?)",
                        (program_id, category_slug, code, i),
                    )
    conn.commit()


def _week_title(category_slug, month, week):
    """САБАҚ ТАПСЫРУ АНАЛИЗ санатында әр айдың соңғы (WEEKS_PER_MONTH-ші)
    аптасы бөлек СТ емес, сол айдың 1,2,3-апта нәтижелері мен анализдерін
    біріктіретін 'N-АЙ ОРТАҚ' торабы болады (басқа санаттарда — бұрынғыдай
    әдеттегі 'N-АЙ M-АПТА'). АЙЛЫҚ ТЕСТ АНАЛИЗ санатында апта бөлінісі жоқ —
    әр ай тек бір 'N-АЙ' жазбасы ретінде беріледі."""
    if category_slug in CATEGORY_WEEKS_PER_MONTH_OVERRIDE:
        return f"{month}-АЙ"
    if category_slug == DEFAULT_CATEGORY and week == WEEKS_PER_MONTH:
        return f"{month}-АЙ ОРТАҚ"
    return f"{month}-АЙ {week}-АПТА"


def _seed_weeks(conn):
    """Әр потокқа бекітілген оқу күнтізбесін алдын ала толтырады: Smart/Junior — 5 ай,
    әр ай 4 аптадан ('N-АЙ M-АПТА', соңғысы САБАҚ ТАПСЫРУ АНАЛИЗ-да 'N-АЙ ОРТАҚ').
    Максимум балл мен мақсат балл бағдарлама бойынша бірден бекітіледі
    (PROGRAM_MAX_SCORE/PROGRAM_TARGET_SCORE) — рейтинг қосу кезінде қайта
    енгізудің қажеті жоқ."""
    programs = conn.execute("SELECT * FROM programs").fetchall()
    for p in programs:
        months = PROGRAM_MONTHS.get(p["slug"])
        if not months:
            continue
        streams = conn.execute("SELECT * FROM streams WHERE program_id = ?", (p["id"],)).fetchall()
        for s in streams:
            existing_count = conn.execute(
                "SELECT COUNT(*) AS c FROM weeks WHERE stream_id = ?", (s["id"],)
            ).fetchone()["c"]
            if existing_count > 0:
                continue
            max_score, target_score = score_defaults_for(p["slug"], s["category"])
            weeks_per_month = CATEGORY_WEEKS_PER_MONTH_OVERRIDE.get(s["category"], WEEKS_PER_MONTH)
            for month in range(1, months + 1):
                for week in range(1, weeks_per_month + 1):
                    title = _week_title(s["category"], month, week)
                    conn.execute(
                        "INSERT INTO weeks (stream_id, month_number, week_number, title, max_score, target_score) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (s["id"], month, week, title, max_score, target_score),
                    )
    conn.commit()


def init_db():
    conn = get_connection()
    schema = SCHEMA_PG if getattr(conn, "backend", None) == "postgres" else SCHEMA_SQLITE
    conn.executescript(schema)
    conn.commit()
    _migrate(conn)
    _seed_defaults(conn)
    _seed_weeks(conn)
    conn.close()
