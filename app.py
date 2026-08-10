import base64
import json
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, g, flash, session, Response, jsonify
from markupsafe import Markup, escape

load_dotenv()

import db
from analysis import (
    compute_report,
    compute_curator_extremes,
    compare_reports,
    compute_teacher_stats,
    compute_teacher_stream_detail,
    find_teacher_home_stream,
)
from sheets import fetch_workbook, rows_to_dicts, guess_columns, is_summary_row, is_template_sheet, SheetFetchError
from gdocs import (
    fetch_doc_text,
    DocFetchError,
    parse_weekly_plan,
    PlanParseError,
    classify_plan_sections,
    strip_template_entry,
)
from curator_analysis import generate_curator_analysis, build_summary_text, merge_analyses, CuratorAnalysisError
from books_ingest import (
    download_drive_file,
    get_pdf_page_count,
    extract_chunk_pdf_bytes,
    read_chunk_with_claude,
    BookIngestError,
    PAGES_PER_CHUNK,
)
import material_check

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "juz40-local-dev-secret")
app.permanent_session_lifetime = timedelta(days=30)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

db.init_db()

_SUMMARY_LABEL_RE = re.compile(r"^([^:\n]{1,80}):(.*)$")


@app.template_filter("summary_html")
def format_summary_html(text):
    """Апта қорытындысындағы 'Белгі: мәтін' жолдарының белгісін жирный қылып көрсетеді."""
    if not text:
        return ""
    lines = []
    for line in text.split("\n"):
        m = _SUMMARY_LABEL_RE.match(line)
        if m:
            lines.append(f"<strong>{escape(m.group(1))}:</strong>{escape(m.group(2))}")
        else:
            lines.append(str(escape(line)))
    return Markup("<br>".join(lines))


@app.context_processor
def inject_category_label():
    return {
        "category_label": lambda slug: db.CATEGORY_LABELS.get(slug, slug),
        "category_stats_label": lambda slug: db.CATEGORY_STATS_LABELS.get(slug, slug),
        "sidebar_categories": db.SIDEBAR_CATEGORIES,
    }


@app.before_request
def require_login():
    if request.endpoint in ("login", "static", "favicon") or request.endpoint is None:
        return
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))


@app.errorhandler(413)
def handle_file_too_large(_e):
    flash("Файл тым үлкен — максимум 4МБ.", "error")
    return redirect(request.referrer or url_for("index"))


@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="img/favicon.ico"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("logged_in"):
        return redirect(url_for("index"))

    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return (
            "Кіру мүмкін емес: ADMIN_EMAIL / ADMIN_PASSWORD ортам айнымалылары "
            "орнатылмаған. .env файлында (немесе Vercel Environment Variables "
            "бөлімінде) осыларды қойыңыз.",
            500,
        )

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        next_url = request.form.get("next") or url_for("index")
        if email == ADMIN_EMAIL.lower() and password == ADMIN_PASSWORD:
            session.clear()
            session["logged_in"] = True
            session.permanent = True
            return redirect(next_url)
        flash("Email немесе құпия сөз қате.", "error")

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))

REQUIRED_FIELDS = [
    ("student", "Оқушы аты-жөні"),
    ("subject", "Пән"),
    ("topic", "Тақырып"),
    ("score", "Балл"),
    ("max_score", "Максимум балл"),
    ("curator", "Куратор"),
]


def get_db():
    if "db" not in g:
        g.db = db.get_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def get_week_or_404(conn, week_id):
    return conn.execute("SELECT * FROM weeks WHERE id = ?", (week_id,)).fetchone()


def parse_curator_analysis(week):
    raw = week["curators_analysis_json"]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def get_week_context(conn, week_id):
    """Аптаны, оның потогын және потоктың бағдарламасын бірге қайтарады
    (breadcrumb пен артқа қайту сілтемелері үшін)."""
    week = get_week_or_404(conn, week_id)
    if week is None:
        return None, None, None
    stream = None
    program = None
    if week["stream_id"] is not None:
        stream = conn.execute("SELECT * FROM streams WHERE id = ?", (week["stream_id"],)).fetchone()
        if stream is not None:
            program = conn.execute(
                "SELECT * FROM programs WHERE id = ?", (stream["program_id"],)
            ).fetchone()
    return week, stream, program


def is_month_summary_week(week, stream):
    """САБАҚ ТАПСЫРУ АНАЛИЗ санатындағы әр айдың соңғы (WEEKS_PER_MONTH-ші)
    аптасы — бөлек СТ емес, сол айдың алдыңғы апталарының нәтижесі мен
    анализін біріктіретін 'N-АЙ ОРТАҚ' торабы. Мұнда кесте импорттау/жеке
    куратор құжаты жоқ — бәрі 1,2,3-аптадан автоматты жиналады."""
    return (
        stream is not None
        and stream["category"] == db.DEFAULT_CATEGORY
        and week["week_number"] == db.WEEKS_PER_MONTH
    )


def get_month_component_weeks(conn, week):
    """Осы 'N-АЙ ОРТАҚ' аптасы біріктіретін, сол айдың 1,2,3-апталарын
    (week_number < WEEKS_PER_MONTH) ретімен қайтарады."""
    return conn.execute(
        "SELECT * FROM weeks WHERE stream_id = ? AND month_number = ? AND week_number < ? ORDER BY week_number",
        (week["stream_id"], week["month_number"], db.WEEKS_PER_MONTH),
    ).fetchall()


def find_previous_week_with_data(conn, week, stream):
    """Ағымдағы кезекті (айлық ортақ емес) аптаның алдында, дәл осы ағында
    нақты нәтижесі бар ЕҢ СОҢҚЫ аптаны табады. Тікелей алдыңғы апта бос
    болса (мыс. жаңа ай/жаңа айлық тест жаңа басталғанда), деректі
    тапқанша одан да бұрынғы апталарды қарастыра береді — сол арқылы
    'соңғы болған СТ/АТ нәтижесімен салыстыру' ережесі орындалады."""
    if stream is None:
        return None

    if stream["category"] == db.DEFAULT_CATEGORY:
        if week["week_number"] is None or week["week_number"] >= db.WEEKS_PER_MONTH:
            return None
        rows = conn.execute(
            "SELECT * FROM weeks WHERE stream_id = ? AND week_number < ? "
            "ORDER BY month_number DESC, week_number DESC",
            (week["stream_id"], db.WEEKS_PER_MONTH),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM weeks WHERE stream_id = ? ORDER BY month_number DESC, week_number DESC",
            (week["stream_id"],),
        ).fetchall()

    current_key = (week["month_number"] or 0, week["week_number"] or 0)
    for w in rows:
        key = (w["month_number"] or 0, w["week_number"] or 0)
        if key >= current_key:
            continue
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE week_id = ? AND score IS NOT NULL", (w["id"],)
        ).fetchone()["c"]
        if count > 0:
            return w
    return None


@app.route("/")
def index():
    return render_template("index.html")


BACKUP_TABLES = ["programs", "streams", "weeks", "imports", "results"]


@app.route("/backup")
def download_backup():
    """Барлық негізгі кестені (нәтижелер, апталар, ағындар, бағдарламалар,
    импорттар) бір JSON файл ретінде жүктеп береді — пайдаланушы өз қолымен
    кез келген сәтте сақтық көшірме алып қоя алатындай."""
    conn = get_db()
    data = {"generated_at": datetime.utcnow().isoformat() + "Z"}
    for table in BACKUP_TABLES:
        data[table] = [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]

    body = json.dumps(data, ensure_ascii=False, default=str, indent=2)
    filename = f"juz40-backup-{datetime.utcnow().strftime('%Y-%m-%d')}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/categories/<category_slug>")
def category_picker(category_slug):
    conn = get_db()
    if category_slug not in db.CATEGORY_LABELS:
        flash("Санат табылмады.", "error")
        return redirect(url_for("index"))

    mode = request.args.get("mode")
    category_name = db.CATEGORY_STATS_LABELS[category_slug] if mode == "stats" else db.CATEGORY_LABELS[category_slug]
    programs = conn.execute("SELECT * FROM programs ORDER BY sort_order, id").fetchall()
    stream_counts = {
        row["program_id"]: row["c"]
        for row in conn.execute(
            "SELECT program_id, COUNT(*) AS c FROM streams WHERE category = ? GROUP BY program_id",
            (category_slug,),
        ).fetchall()
    }
    return render_template(
        "category_picker.html",
        category_slug=category_slug,
        category_name=category_name,
        programs=programs,
        mode=mode,
        stream_counts=stream_counts,
    )


@app.route("/programs/<slug>")
def program_detail(slug):
    conn = get_db()
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("index"))

    # Санат бетінен (category_picker) келгенде тек сол санаттың ағындары
    # көрсетіледі — СТ мен АТ бір-біріне мүлде араласпайды. Санатсыз тікелей
    # кірсе (ескі сілтеме/бетбелгі), бұрынғыдай барлық санат қатар көрінеді.
    category_slug = request.args.get("category")
    if category_slug not in db.CATEGORY_LABELS:
        category_slug = None

    streams = conn.execute(
        "SELECT * FROM streams WHERE program_id = ? ORDER BY sort_order, id", (program["id"],)
    ).fetchall()
    week_counts = {
        row["stream_id"]: row["c"]
        for row in conn.execute(
            "SELECT stream_id, COUNT(*) AS c FROM weeks WHERE stream_id IN "
            "(SELECT id FROM streams WHERE program_id = ?) GROUP BY stream_id",
            (program["id"],),
        ).fetchall()
    }
    by_category = {}
    for s in streams:
        by_category.setdefault(s["category"], []).append(
            {"stream": s, "week_count": week_counts.get(s["id"], 0)}
        )

    categories_to_show = (
        [(category_slug, db.CATEGORY_LABELS[category_slug], 0)] if category_slug else db.CATEGORIES
    )
    columns = [
        {"slug": cslug, "label": label, "streams": by_category.get(cslug, [])}
        for cslug, label, _order in categories_to_show
    ]
    mode = request.args.get("mode")
    return render_template(
        "program.html",
        program=program,
        columns=columns,
        current_category=category_slug,
        show_plan_card=mode != "stats" and (category_slug is None or category_slug == db.DEFAULT_CATEGORY),
        mode=mode,
    )


@app.route("/programs/<slug>/plan", methods=["POST"])
def save_program_plan(slug):
    conn = get_db()
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("index"))

    doc_url = request.form.get("plan_doc_url", "").strip()
    if not doc_url:
        flash("Google Docs сілтемесін енгізіңіз.", "error")
        return redirect(url_for("program_detail", slug=slug))

    fetch_error = None
    plan_map = {}
    try:
        doc_text = fetch_doc_text(doc_url, max_chars=0)
        plan_map = parse_weekly_plan(doc_text)
        for (month_num, week_num), content in plan_map.items():
            conn.execute(
                "UPDATE weeks SET plan_text = ? "
                "WHERE month_number = ? AND week_number = ? AND stream_id IN "
                "(SELECT id FROM streams WHERE program_id = ? AND category = ?)",
                (content, month_num, week_num, program["id"], db.DEFAULT_CATEGORY),
            )
    except (DocFetchError, PlanParseError) as e:
        fetch_error = str(e)

    conn.execute(
        "UPDATE programs SET plan_doc_url = ?, plan_doc_fetch_error = ? WHERE id = ?",
        (doc_url, fetch_error, program["id"]),
    )
    conn.commit()

    if fetch_error:
        flash(f"Жоспарды жүктеу сәтсіз аяқталды: {fetch_error}", "error")
    else:
        flash(f"Жоспар жаңартылды — {len(plan_map)} апта, барлық потоктарға қолданылды.", "ok")
    return redirect(url_for("program_detail", slug=slug))


@app.route("/programs/<slug>/plan/remove", methods=["POST"])
def remove_program_plan(slug):
    conn = get_db()
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("index"))

    conn.execute(
        "UPDATE weeks SET plan_text = NULL WHERE stream_id IN "
        "(SELECT id FROM streams WHERE program_id = ? AND category = ?)",
        (program["id"], db.DEFAULT_CATEGORY),
    )
    conn.execute(
        "UPDATE programs SET plan_doc_url = NULL, plan_doc_fetch_error = NULL WHERE id = ?",
        (program["id"],),
    )
    conn.commit()
    flash("Жоспар сілтемесі және барлық апталардың жоспар мәтіні алынып тасталды.", "ok")
    return redirect(url_for("program_detail", slug=slug))


@app.route("/weeks/<int:week_id>/plan")
def week_plan(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = 1 if week["curators_doc_url"] else 0
    plan_sections = classify_plan_sections(week["plan_text"]) if week["plan_text"] else None

    return render_template(
        "plan.html",
        week=week,
        stream=stream,
        program=program,
        active_page="plan",
        result_count=result_count,
        note_count=note_count,
        plan_sections=plan_sections,
        is_month_summary=is_month_summary_week(week, stream),
    )


@app.route("/streams/<int:stream_id>")
def stream_detail(stream_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    weeks = conn.execute(
        "SELECT * FROM weeks WHERE stream_id = ? ORDER BY month_number, week_number, id", (stream_id,)
    ).fetchall()
    result_counts = {
        row["week_id"]: row["c"]
        for row in conn.execute(
            "SELECT week_id, COUNT(*) AS c FROM results WHERE week_id IN "
            "(SELECT id FROM weeks WHERE stream_id = ?) GROUP BY week_id",
            (stream_id,),
        ).fetchall()
    }
    weeks_by_month = {}
    for w in weeks:
        weeks_by_month.setdefault(w["month_number"], []).append(w)

    months = {}
    for w in weeks:
        if is_month_summary_week(w, stream):
            component_weeks = [
                cw for cw in weeks_by_month.get(w["month_number"], [])
                if cw["week_number"] < db.WEEKS_PER_MONTH
            ]
            result_count = sum(result_counts.get(cw["id"], 0) for cw in component_weeks)
            note_count = 1 if any(cw["curators_doc_url"] for cw in component_weeks) else 0
        else:
            result_count = result_counts.get(w["id"], 0)
            note_count = 1 if w["curators_doc_url"] else 0
        months.setdefault(w["month_number"], []).append(
            {"week": w, "result_count": result_count, "note_count": note_count}
        )
    months_sorted = sorted(months.items(), key=lambda item: (item[0] is None, item[0]))

    return render_template("stream.html", stream=stream, program=program, months=months_sorted)


@app.route("/streams/<int:stream_id>/teachers")
def stream_teachers(stream_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    teachers = compute_teacher_stats(conn, stream_id)
    stream_stats_categories = {
        row["category"]: row["id"]
        for row in conn.execute(
            "SELECT id, category FROM streams WHERE program_id = ? AND code = ?",
            (stream["program_id"], stream["code"]),
        ).fetchall()
    }

    return render_template(
        "stream_teachers.html",
        stream=stream,
        program=program,
        teachers=teachers,
        stream_stats_categories=stream_stats_categories,
    )


@app.route("/streams/<int:stream_id>/teachers/<int:teacher_id>")
def teacher_detail(stream_id, teacher_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    sibling_streams = conn.execute(
        "SELECT id, category FROM streams WHERE program_id = ? AND code = ?",
        (stream["program_id"], stream["code"]),
    ).fetchall()

    category_streams = {}
    for row in sibling_streams:
        max_score, _target_score = db.score_defaults_for(program["slug"], row["category"])
        category_streams[row["category"]] = {"stream_id": row["id"], "max_score": max_score}

    detail = compute_teacher_stream_detail(conn, teacher_id, category_streams)
    if detail["teacher"] is None:
        flash("Мұғалім табылмады.", "error")
        return redirect(url_for("stream_teachers", stream_id=stream_id))

    stream_stats_categories = {row["category"]: row["id"] for row in sibling_streams}

    return render_template(
        "teacher_detail.html",
        stream=stream,
        program=program,
        detail=detail,
        stream_stats_categories=stream_stats_categories,
    )


def _teacher_stream_picker_data(conn):
    """Мұғалім қосу пішініндегі 'Бағдарлама → Поток' таңдауы үшін деректер:
    әр бағдарламаның потоктарын (СТ/АТ жұбының СТ жағы жеткілікті — кодтары
    ортақ) сол бағдарламаның слагы бойынша топтастырып қайтарады."""
    rows = conn.execute(
        "SELECT s.id, s.code, p.slug AS program_slug, p.name AS program_name "
        "FROM streams s JOIN programs p ON p.id = s.program_id "
        "WHERE s.category = ? ORDER BY p.sort_order, p.id, s.sort_order, s.id",
        (db.DEFAULT_CATEGORY,),
    ).fetchall()
    programs = conn.execute("SELECT slug, name FROM programs ORDER BY sort_order, id").fetchall()
    streams_by_program = {}
    for r in rows:
        streams_by_program.setdefault(r["program_slug"], []).append({"id": r["id"], "code": r["code"]})
    return programs, streams_by_program


@app.route("/teachers")
def teachers_list():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.id, t.name, t.stream_id, "
        "array_agg(tc.curator_name) AS curator_names "
        "FROM teachers t LEFT JOIN teacher_curators tc ON tc.teacher_id = t.id "
        "GROUP BY t.id, t.name, t.stream_id ORDER BY t.name"
        if getattr(conn, "backend", None) == "postgres"
        else "SELECT id, name, stream_id FROM teachers ORDER BY name"
    ).fetchall()

    stream_labels = {
        r["id"]: f"{r['program_name']} | {r['code']}"
        for r in conn.execute(
            "SELECT s.id, s.code, p.name AS program_name FROM streams s JOIN programs p ON p.id = s.program_id"
        ).fetchall()
    }

    if getattr(conn, "backend", None) == "postgres":
        teachers = [
            {
                "id": r["id"],
                "name": r["name"],
                "curator_names": [c for c in (r["curator_names"] or []) if c],
                "stream_label": stream_labels.get(r["stream_id"]),
            }
            for r in rows
        ]
    else:
        teachers = []
        for r in rows:
            curator_rows = conn.execute(
                "SELECT curator_name FROM teacher_curators WHERE teacher_id = ? ORDER BY curator_name",
                (r["id"],),
            ).fetchall()
            teachers.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "curator_names": [c["curator_name"] for c in curator_rows],
                    "stream_label": stream_labels.get(r["stream_id"]),
                }
            )

    programs, streams_by_program = _teacher_stream_picker_data(conn)

    return render_template(
        "teachers.html",
        teachers=teachers,
        all_teachers=teachers,
        active_teacher_id=None,
        programs=programs,
        streams_by_program=streams_by_program,
    )


@app.route("/teachers/<int:teacher_id>")
def teacher_home(teacher_id):
    conn = get_db()
    teacher = conn.execute("SELECT * FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
    if teacher is None:
        flash("Мұғалім табылмады.", "error")
        return redirect(url_for("teachers_list"))

    stream_id = teacher["stream_id"] or find_teacher_home_stream(conn, teacher_id)
    if stream_id is None:
        flash("Бұл мұғалімнің кураторларының нәтижесі әлі табылған жоқ.", "error")
        return redirect(url_for("teachers_list"))

    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    sibling_streams = conn.execute(
        "SELECT id, category FROM streams WHERE program_id = ? AND code = ?",
        (stream["program_id"], stream["code"]),
    ).fetchall()
    category_streams = {}
    for row in sibling_streams:
        max_score, _target_score = db.score_defaults_for(program["slug"], row["category"])
        category_streams[row["category"]] = {"stream_id": row["id"], "max_score": max_score}

    detail = compute_teacher_stream_detail(conn, teacher_id, category_streams)

    all_teachers = conn.execute("SELECT id, name FROM teachers ORDER BY name").fetchall()

    return render_template(
        "teacher_detail.html",
        stream=stream,
        program=program,
        detail=detail,
        all_teachers=all_teachers,
        active_teacher_id=teacher_id,
    )


@app.route("/teachers/<int:teacher_id>/edit", methods=["GET", "POST"])
def edit_teacher(teacher_id):
    conn = get_db()
    teacher = conn.execute("SELECT * FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
    if teacher is None:
        flash("Мұғалім табылмады.", "error")
        return redirect(url_for("teachers_list"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        curator_names_raw = request.form.get("curator_names", "")
        curator_names = list(dict.fromkeys(c.strip() for c in curator_names_raw.split("\n") if c.strip()))
        stream_id_raw = request.form.get("stream_id", "").strip()

        if not name:
            flash("Мұғалімнің атын енгізіңіз.", "error")
            return redirect(url_for("edit_teacher", teacher_id=teacher_id))
        if not stream_id_raw.isdigit():
            flash("Потокты таңдаңыз.", "error")
            return redirect(url_for("edit_teacher", teacher_id=teacher_id))
        stream_id = int(stream_id_raw)
        if conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone() is None:
            flash("Таңдалған поток табылмады.", "error")
            return redirect(url_for("edit_teacher", teacher_id=teacher_id))

        # add_teacher-дегідей: UNIQUE қатесінен транзакцияны қорғау үшін
        # алдымен осы мұғалімнің ескі кураторларын өшіріп, содан кейін ғана
        # жаңа тізімнің басқа мұғалімге тіркелмегенін тексереміз.
        conn.execute("DELETE FROM teacher_curators WHERE teacher_id = ?", (teacher_id,))
        existing = set()
        if curator_names:
            placeholders = ",".join("?" * len(curator_names))
            existing = {
                row["curator_name"]
                for row in conn.execute(
                    f"SELECT curator_name FROM teacher_curators WHERE curator_name IN ({placeholders})",
                    curator_names,
                ).fetchall()
            }
        to_insert = [c for c in curator_names if c not in existing]
        skipped = [c for c in curator_names if c in existing]

        conn.execute("UPDATE teachers SET name = ?, stream_id = ? WHERE id = ?", (name, stream_id, teacher_id))
        for curator_name in to_insert:
            conn.execute(
                "INSERT INTO teacher_curators (teacher_id, curator_name) VALUES (?, ?)",
                (teacher_id, curator_name),
            )
        conn.commit()

        if skipped:
            flash(
                "Мұғалім жаңартылды, бірақ мына кураторлар басқа мұғалімге тіркелген болғандықтан қосылмады: "
                + ", ".join(skipped),
                "error",
            )
        else:
            flash("Мұғалім жаңартылды.", "ok")
        return redirect(url_for("teachers_list"))

    curator_rows = conn.execute(
        "SELECT curator_name FROM teacher_curators WHERE teacher_id = ? ORDER BY curator_name", (teacher_id,)
    ).fetchall()
    curator_names_text = "\n".join(r["curator_name"] for r in curator_rows)

    current_program_slug = None
    if teacher["stream_id"] is not None:
        row = conn.execute(
            "SELECT p.slug FROM streams s JOIN programs p ON p.id = s.program_id WHERE s.id = ?",
            (teacher["stream_id"],),
        ).fetchone()
        current_program_slug = row["slug"] if row else None

    programs, streams_by_program = _teacher_stream_picker_data(conn)
    all_teachers = conn.execute("SELECT id, name FROM teachers ORDER BY name").fetchall()

    return render_template(
        "teacher_edit.html",
        teacher=teacher,
        curator_names_text=curator_names_text,
        programs=programs,
        streams_by_program=streams_by_program,
        current_program_slug=current_program_slug,
        all_teachers=all_teachers,
        active_teacher_id=teacher_id,
    )


@app.route("/teachers/add", methods=["POST"])
def add_teacher():
    conn = get_db()
    name = request.form.get("name", "").strip()
    curator_names_raw = request.form.get("curator_names", "")
    curator_names = [c.strip() for c in curator_names_raw.split("\n") if c.strip()]
    stream_id_raw = request.form.get("stream_id", "").strip()

    if not name:
        flash("Мұғалімнің атын енгізіңіз.", "error")
        return redirect(url_for("teachers_list"))

    if not stream_id_raw.isdigit():
        flash("Потокты таңдаңыз.", "error")
        return redirect(url_for("teachers_list"))
    stream_id = int(stream_id_raw)
    if conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone() is None:
        flash("Таңдалған поток табылмады.", "error")
        return redirect(url_for("teachers_list"))

    # Кураторлар атауы UNIQUE болғандықтан, INSERT-ті орындамас бұрын бар-жоғын
    # тексереміз — Postgres-те бір INSERT қатесі бүкіл транзакцияны "бұзып",
    # содан кейінгі барлық сұранысты іске асырмай қалдырар еді.
    curator_names = list(dict.fromkeys(curator_names))
    existing = set()
    if curator_names:
        placeholders = ",".join("?" * len(curator_names))
        existing = {
            row["curator_name"]
            for row in conn.execute(
                f"SELECT curator_name FROM teacher_curators WHERE curator_name IN ({placeholders})",
                curator_names,
            ).fetchall()
        }
    to_insert = [c for c in curator_names if c not in existing]
    skipped = [c for c in curator_names if c in existing]

    teacher_id = conn.execute(
        "INSERT INTO teachers (name, stream_id) VALUES (?, ?) RETURNING id", (name, stream_id)
    ).fetchone()["id"]
    for curator_name in to_insert:
        conn.execute(
            "INSERT INTO teacher_curators (teacher_id, curator_name) VALUES (?, ?)",
            (teacher_id, curator_name),
        )
    conn.commit()

    if skipped:
        flash(
            "Мұғалім қосылды, бірақ мына кураторлар басқа мұғалімге тіркелген болғандықтан қосылмады: "
            + ", ".join(skipped),
            "error",
        )
    else:
        flash("Мұғалім қосылды.", "ok")
    return redirect(url_for("teachers_list"))


@app.route("/teachers/<int:teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    conn = get_db()
    conn.execute("DELETE FROM teachers WHERE id = ?", (teacher_id,))
    conn.commit()
    flash("Мұғалім жойылды.", "ok")
    return redirect(url_for("teachers_list"))


@app.route("/materials")
def materials_list():
    conn = get_db()
    programs = conn.execute("SELECT * FROM programs ORDER BY sort_order, id").fetchall()
    return render_template("materials_programs.html", programs=programs)


def _material_program_or_404(conn, slug):
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
    return program


@app.route("/materials/<slug>")
def materials_program_page(slug):
    conn = get_db()
    program = _material_program_or_404(conn, slug)
    if program is None:
        return redirect(url_for("materials_list"))

    rows = conn.execute(
        "SELECT * FROM material_checks WHERE program_id = ? ORDER BY material_type, created_at, id",
        (program["id"],),
    ).fetchall()

    run_rows = conn.execute(
        "SELECT id, material_id, status, processed_pages, total_pages, error_text, result_json "
        "FROM material_check_runs ORDER BY id DESC"
    ).fetchall()
    latest_run_by_material = {}
    for r in run_rows:
        latest_run_by_material.setdefault(r["material_id"], r)

    by_type = {slug: [] for slug, _label in db.MATERIAL_TYPES}
    for r in rows:
        entry = dict(r)
        run = latest_run_by_material.get(entry["id"])
        entry["latest_run"] = run
        entry["latest_run_results"] = None
        if run and run["result_json"]:
            try:
                entry["latest_run_results"] = json.loads(run["result_json"])
            except (TypeError, ValueError):
                entry["latest_run_results"] = None
        by_type.setdefault(entry["material_type"], []).append(entry)

    sections = [
        {
            "slug": slug,
            "label": label,
            "entries": [
                e for e in by_type.get(slug, [])
                if not (e["latest_run"] and e["latest_run"]["status"] == "done")
            ],
            "checked_entries": [
                e for e in by_type.get(slug, []) if e["latest_run"] and e["latest_run"]["status"] == "done"
            ],
        }
        for slug, label in db.MATERIAL_TYPES
    ]

    books_all = conn.execute(
        "SELECT id, title FROM books WHERE link IS NOT NULL AND link != '' ORDER BY title"
    ).fetchall()

    plan_weeks = []
    if program["material_plan_text"]:
        parsed = material_check.parse_plan_weeks(program["material_plan_text"])
        plan_weeks = [
            {"month": m, "week": w, "topic": info["topic"], "has_pages": info["page_start"] is not None}
            for (m, w), info in sorted(parsed.items())
        ]

    return render_template(
        "materials.html",
        program=program,
        sections=sections,
        books_all=books_all,
        plan_weeks=plan_weeks,
    )


@app.route("/materials/<slug>/plan/save", methods=["POST"])
def save_material_plan(slug):
    conn = get_db()
    program = _material_program_or_404(conn, slug)
    if program is None:
        return redirect(url_for("materials_list"))

    plan_url = request.form.get("material_plan_url", "").strip()
    if not plan_url:
        flash("Жоспар сілтемесін енгізіңіз.", "error")
        return redirect(url_for("materials_program_page", slug=slug))

    plan_text = None
    fetch_error = None
    try:
        plan_text = fetch_doc_text(plan_url, max_chars=200_000)
    except DocFetchError as e:
        fetch_error = str(e)

    conn.execute(
        "UPDATE programs SET material_plan_url = ?, material_plan_text = ?, material_plan_fetch_error = ? "
        "WHERE id = ?",
        (plan_url, plan_text, fetch_error, program["id"]),
    )
    conn.commit()
    if fetch_error:
        flash(f"Жоспар сілтемесі сақталды, бірақ жүктеу сәтсіз аяқталды: {fetch_error}", "error")
    else:
        flash("Жоспар сілтемесі сақталды және жүктелді.", "ok")
    return redirect(url_for("materials_program_page", slug=slug))


@app.route("/materials/<slug>/plan/remove", methods=["POST"])
def remove_material_plan(slug):
    conn = get_db()
    program = _material_program_or_404(conn, slug)
    if program is None:
        return redirect(url_for("materials_list"))

    conn.execute(
        "UPDATE programs SET material_plan_url = NULL, material_plan_text = NULL, "
        "material_plan_fetch_error = NULL WHERE id = ?",
        (program["id"],),
    )
    conn.commit()
    flash("Жоспар сілтемесі алып тасталды.", "ok")
    return redirect(url_for("materials_program_page", slug=slug))


def _create_material_check_run(conn, material, mode, book_ids, month=None, week=None):
    """material үшін жаңа тексеру жазбасын бастайды. book_ids — таңдалған
    кітаптардың id тізімі (targeted режимде бірнешеу болуы мүмкін, барлығы
    бір біріктірілген нәтижеге салыстырылады; full режимде тек біріншісі
    қолданылады). Табысты болса (True, run_id), сәтсіз болса (False, error_text)
    қайтарады."""
    book_ids = [b for b in (book_ids or []) if b]
    if not book_ids:
        return False, "Кітапты таңдаңыз."

    if mode == "targeted":
        books = conn.execute(
            f"SELECT * FROM books WHERE id IN ({','.join('?' * len(book_ids))}) AND link IS NOT NULL",
            book_ids,
        ).fetchall()
        if len(books) != len(book_ids):
            return False, "Таңдалған кітаптардың бірі табылмады немесе сілтемесі жоқ."
        program = conn.execute("SELECT * FROM programs WHERE id = ?", (material["program_id"],)).fetchone()
        if not program or not program["material_plan_text"]:
            return False, "Алдымен жоспар сілтемесін қосыңыз."

        plan_weeks = material_check.parse_plan_weeks(program["material_plan_text"])
        info = plan_weeks.get((month, week))
        if not info or info["page_start"] is None:
            return False, (
                "Осы тақырып үшін жоспардан бет ауқымын таба алмадым "
                "('Оқулық бет' жолын тексеріңіз)."
            )

        conn.execute(
            "INSERT INTO material_check_runs "
            "(material_id, book_id, book_ids_json, status, mode, target_month, target_week, target_topic, "
            "target_page_start, target_page_end, total_pages) "
            "VALUES (?, ?, ?, 'running', 'targeted', ?, ?, ?, ?, ?, ?)",
            (
                material["id"], book_ids[0], json.dumps(book_ids), month, week, info["topic"],
                info["page_start"], info["page_end"],
                info["page_end"] - info["page_start"] + 1,
            ),
        )
    else:
        book = conn.execute(
            "SELECT * FROM books WHERE id = ? AND ingest_status = 'done'", (book_ids[0],)
        ).fetchone()
        if book is None:
            return False, "Кітапты таңдаңыз (толық оқылған болу керек)."

        conn.execute(
            "INSERT INTO material_check_runs (material_id, book_id, book_ids_json, status, mode, total_pages) "
            "VALUES (?, ?, ?, 'running', 'full', ?)",
            (material["id"], book_ids[0], json.dumps([book_ids[0]]), book["total_pages"]),
        )

    conn.commit()
    run_id = conn.execute(
        "SELECT id FROM material_check_runs WHERE material_id = ? ORDER BY id DESC LIMIT 1",
        (material["id"],),
    ).fetchone()["id"]
    return True, run_id


@app.route("/materials/add", methods=["POST"])
def add_material():
    conn = get_db()
    program_id = request.form.get("program_id", type=int)
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (program_id,)).fetchone() if program_id else None
    material_type = request.form.get("material_type", "").strip()
    label = request.form.get("label", "").strip()
    link = request.form.get("link", "").strip()
    book_ids = request.form.getlist("book_ids", type=int)
    week_key = request.form.get("week_key", "").strip()

    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("materials_list"))
    if material_type not in db.MATERIAL_TYPE_LABELS:
        flash("Материал түрін таңдаңыз.", "error")
        return redirect(url_for("materials_program_page", slug=program["slug"]))
    if not label:
        flash("Жазба атауын енгізіңіз.", "error")
        return redirect(url_for("materials_program_page", slug=program["slug"]))
    if not book_ids:
        flash("Кемінде бір кітап таңдаңыз.", "error")
        return redirect(url_for("materials_program_page", slug=program["slug"]))
    try:
        month_s, week_s = week_key.split("-", 1)
        month, week = int(month_s), int(week_s)
    except (ValueError, AttributeError):
        flash("Тақырыпты таңдаңыз.", "error")
        return redirect(url_for("materials_program_page", slug=program["slug"]))

    conn.execute(
        "INSERT INTO material_checks (program_id, material_type, label, link) VALUES (?, ?, ?, ?)",
        (program["id"], material_type, label, link or None),
    )
    conn.commit()
    material = conn.execute(
        "SELECT * FROM material_checks WHERE program_id = ? AND material_type = ? AND label = ? "
        "ORDER BY id DESC LIMIT 1",
        (program["id"], material_type, label),
    ).fetchone()

    ok, result = _create_material_check_run(conn, material, "targeted", book_ids, month, week)
    if not ok:
        flash(f"Жазба қосылды, бірақ тексеру басталмады: {result}", "error")
    else:
        flash("Жазба қосылды, тексеру басталды.", "ok")
    return redirect(url_for("materials_program_page", slug=program["slug"]))


def _material_redirect_target(conn, material_id):
    row = conn.execute(
        "SELECT p.slug FROM material_checks m JOIN programs p ON p.id = m.program_id WHERE m.id = ?",
        (material_id,),
    ).fetchone()
    if row is None:
        return redirect(url_for("materials_list"))
    return redirect(url_for("materials_program_page", slug=row["slug"]))


@app.route("/materials/<int:material_id>/delete", methods=["POST"])
def delete_material(material_id):
    conn = get_db()
    target = _material_redirect_target(conn, material_id)
    conn.execute("DELETE FROM material_checks WHERE id = ?", (material_id,))
    conn.commit()
    flash("Жазба жойылды.", "ok")
    return target


@app.route("/materials/check_runs/<int:run_id>/step", methods=["POST"])
def step_material_check(run_id):
    """Материалды кітаппен салыстыру процесінің БІР қадамы: кезектегі бет
    тобын (PAGES_PER_BATCH бет) тексереді де, нәтижесін жинақтайды. Барлық
    бет өткен соң, жиналған тізімді тағы бір рет қарап шығатын қорытынды
    қадам жүреді. Frontend бұл маршрутты дайын болғанша қайта-қайта
    шақырады (кітапты оқу процесіндегі сияқты)."""
    conn = get_db()
    run = conn.execute("SELECT * FROM material_check_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return jsonify({"status": "error", "error": "Тексеру табылмады."}), 404
    if run["status"] in ("done", "error"):
        return jsonify(
            {"status": run["status"], "processed_pages": run["processed_pages"], "total_pages": run["total_pages"]}
        )

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        api_key = "".join(ch for ch in api_key.strip() if ch.isascii() and ch.isprintable())
    if not api_key:
        err = "ANTHROPIC_API_KEY орнатылмаған."
        conn.execute("UPDATE material_check_runs SET status = 'error', error_text = ? WHERE id = ?", (err, run_id))
        conn.commit()
        return jsonify({"status": "error", "error": err})

    material = conn.execute("SELECT * FROM material_checks WHERE id = ?", (run["material_id"],)).fetchone()
    criteria = material_check.CRITERIA_BY_TYPE.get(material["material_type"]) if material else None
    if not criteria:
        err = "Бұл материал түрі үшін тексеру критерийі анықталмаған."
        conn.execute("UPDATE material_check_runs SET status = 'error', error_text = ? WHERE id = ?", (err, run_id))
        conn.commit()
        return jsonify({"status": "error", "error": err})

    try:
        if run["material_content"] is None:
            kind, content = material_check.fetch_material_content(material["link"])
            content_to_store = base64.standard_b64encode(content).decode("ascii") if kind == "pdf" else content
            conn.execute(
                "UPDATE material_check_runs SET material_content = ?, material_content_kind = ? WHERE id = ?",
                (content_to_store, kind, run_id),
            )
            conn.commit()
            run = conn.execute("SELECT * FROM material_check_runs WHERE id = ?", (run_id,)).fetchone()

        kind = run["material_content_kind"]
        content = base64.standard_b64decode(run["material_content"]) if kind == "pdf" else run["material_content"]

        total_pages = run["total_pages"]
        processed = run["processed_pages"]

        if processed >= total_pages:
            findings = json.loads(run["findings_json"] or "[]")
            final = material_check.final_review(findings, criteria, api_key)
            conn.execute(
                "UPDATE material_check_runs SET status = 'done', result_json = ? WHERE id = ?",
                (json.dumps(final, ensure_ascii=False), run_id),
            )
            conn.commit()
            return jsonify(
                {"status": "done", "processed_pages": total_pages, "total_pages": total_pages, "results": final}
            )

        if run["mode"] == "targeted":
            book_ids = json.loads(run["book_ids_json"]) if run["book_ids_json"] else [run["book_id"]]
            page_start = run["target_page_start"]
            page_end = run["target_page_end"]
            book_pdf_items = []
            for bid in book_ids:
                book = conn.execute("SELECT * FROM books WHERE id = ?", (bid,)).fetchone()
                if book is None or not book["link"]:
                    raise material_check.MaterialCheckError("Кітап табылмады немесе сілтемесі жоқ.")
                pdf_bytes = download_drive_file(book["link"])
                book_pdf_items.append((book["title"], extract_chunk_pdf_bytes(pdf_bytes, page_start, page_end)))
            batch_findings = material_check.check_targeted(
                kind, content, book_pdf_items, page_start, page_end, run["target_topic"], criteria, api_key
            )
        else:
            page_start = processed + 1
            page_end = min(page_start + material_check.PAGES_PER_BATCH - 1, total_pages)
            chunks = conn.execute(
                "SELECT content_text FROM book_chunks WHERE book_id = ? AND page_start >= ? AND page_end <= ? "
                "ORDER BY page_start",
                (run["book_id"], page_start, page_end),
            ).fetchall()
            book_segment = "\n\n".join(c["content_text"] for c in chunks)
            batch_findings = material_check.check_batch(
                kind, content, book_segment, page_start, page_end, criteria, api_key
            )

        # targeted режимде бүкіл ауқым бір қадамда өтеді, сол себепті
        # processed_pages-ты (қатысты санауыш, 0..total_pages) толық деп
        # белгілейміз — page_end (кітаптағы абсолютті бет нөмірі) емес.
        new_processed = total_pages if run["mode"] == "targeted" else page_end

        existing = json.loads(run["findings_json"] or "[]")
        existing.extend(batch_findings)
        conn.execute(
            "UPDATE material_check_runs SET findings_json = ?, processed_pages = ? WHERE id = ?",
            (json.dumps(existing, ensure_ascii=False), new_processed, run_id),
        )
        conn.commit()

        return jsonify({"status": "running", "processed_pages": new_processed, "total_pages": total_pages})
    except (material_check.MaterialCheckError, BookIngestError) as e:
        conn.execute("UPDATE material_check_runs SET status = 'error', error_text = ? WHERE id = ?", (str(e), run_id))
        conn.commit()
        return jsonify({"status": "error", "error": str(e)})


def _books_back_url(conn, slug):
    if slug:
        program = _material_program_or_404(conn, slug)
        if program is not None:
            return url_for("materials_program_page", slug=slug)
    return url_for("materials_list")


@app.route("/books")
def books_list():
    conn = get_db()
    books = conn.execute(
        "SELECT id, title, link, total_pages, ingest_status, ingest_error, created_at "
        "FROM books ORDER BY created_at, id"
    ).fetchall()
    from_slug = request.args.get("from", "").strip()
    return render_template(
        "books.html",
        books=books,
        from_slug=from_slug,
        back_url=_books_back_url(conn, from_slug),
    )


@app.route("/books/add", methods=["POST"])
def add_book():
    conn = get_db()
    title = request.form.get("title", "").strip()
    link = request.form.get("link", "").strip()
    from_slug = request.form.get("from", "").strip()

    if not title:
        flash("Кітап атауын енгізіңіз.", "error")
        return redirect(url_for("books_list", **({"from": from_slug} if from_slug else {})))

    conn.execute(
        "INSERT INTO books (title, link) VALUES (?, ?)",
        (title, link or None),
    )
    conn.commit()
    flash("Кітап қосылды.", "ok")
    return redirect(url_for("books_list", **({"from": from_slug} if from_slug else {})))


@app.route("/books/<int:book_id>/ingest/step", methods=["POST"])
def ingest_book_step(book_id):
    """Кітапты оқу процесінің БІР қадамы: не PDF-ті Drive-тан жүктеп
    бет санын анықтайды, не кезектегі бөлікті (PAGES_PER_CHUNK бет) Claude-пен
    оқып, нәтижесін сақтайды. Бөліктің бет аралығы соңғы сақталған
    book_chunks.page_end-тен есептеледі (тұрақты санмен емес), сол себепті
    PAGES_PER_CHUNK мәні болашақта өзгерсе де, жартылай оқылған кітаптардың
    прогресі бұзылмайды. Frontend бұл маршрутты дайын болғанша қайта-қайта
    шақырады — осылай ұзақ оқу процесі бір ғана веб-сұранысқа сыймай, көп
    қысқа сұраныстарға бөлінеді (Vercel-дің сұраныс уақыты шегіне соқтықпас
    үшін)."""
    conn = get_db()
    book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        return jsonify({"status": "error", "error": "Кітап табылмады."}), 404

    if book["ingest_status"] == "done":
        return jsonify({"status": "done", "total_pages": book["total_pages"], "done_pages": book["total_pages"]})

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        api_key = "".join(ch for ch in api_key.strip() if ch.isascii() and ch.isprintable())
    if not api_key:
        err = "ANTHROPIC_API_KEY орнатылмаған."
        conn.execute("UPDATE books SET ingest_status = 'error', ingest_error = ? WHERE id = ?", (err, book_id))
        conn.commit()
        return jsonify({"status": "error", "error": err})

    try:
        if book["total_pages"] is None:
            if not book["link"]:
                raise BookIngestError("Кітаптың Google Drive сілтемесі жоқ.")
            pdf_bytes = download_drive_file(book["link"])
            total_pages = get_pdf_page_count(pdf_bytes)
            conn.execute(
                "UPDATE books SET total_pages = ?, ingest_status = 'in_progress', raw_pdf_data = ? WHERE id = ?",
                (total_pages, pdf_bytes, book_id),
            )
            conn.commit()
        else:
            total_pages = book["total_pages"]

        progress = conn.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(page_end), 0) AS last_page FROM book_chunks WHERE book_id = ?",
            (book_id,),
        ).fetchone()
        done_count, last_page = progress["c"], progress["last_page"]

        if last_page >= total_pages:
            conn.execute(
                "UPDATE books SET ingest_status = 'done', raw_pdf_data = NULL WHERE id = ?", (book_id,)
            )
            conn.commit()
            return jsonify({"status": "done", "total_pages": total_pages, "done_pages": total_pages})

        page_start = last_page + 1
        page_end = min(page_start + PAGES_PER_CHUNK - 1, total_pages)

        pdf_row = conn.execute("SELECT raw_pdf_data FROM books WHERE id = ?", (book_id,)).fetchone()
        pdf_bytes = pdf_row["raw_pdf_data"]
        if not isinstance(pdf_bytes, (bytes, bytearray)):
            pdf_bytes = bytes(pdf_bytes)

        chunk_pdf = extract_chunk_pdf_bytes(pdf_bytes, page_start, page_end)
        content = read_chunk_with_claude(chunk_pdf, page_start, page_end, api_key)

        try:
            conn.execute(
                "INSERT INTO book_chunks (book_id, chunk_index, page_start, page_end, content_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (book_id, done_count + 1, page_start, page_end, content),
            )
            conn.commit()
        except Exception as e:
            # Бір кітапты бірнеше қойынды/пайдаланушы бір мезгілде оқи
            # бастаса, екі сұраныс та бір last_page-ды оқып, бір chunk_index-ке
            # жазуға тырысуы мүмкін — бұл нақты қате емес, жай "басқа сұраныс
            # бұл бөлікті менен бұрын жазып үлгерді" деген жағдай, сол себепті
            # қатеге шығармай, жай ағымдағы прогресті қайтарамыз.
            conn.rollback()
            if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                raise
            new_last_page = conn.execute(
                "SELECT COALESCE(MAX(page_end), 0) AS last_page FROM book_chunks WHERE book_id = ?", (book_id,)
            ).fetchone()["last_page"]
            if new_last_page >= total_pages:
                conn.execute(
                    "UPDATE books SET ingest_status = 'done', raw_pdf_data = NULL WHERE id = ?", (book_id,)
                )
                conn.commit()
                return jsonify({"status": "done", "total_pages": total_pages, "done_pages": total_pages})
            return jsonify({"status": "in_progress", "total_pages": total_pages, "done_pages": new_last_page})

        done_pages = page_end
        if done_pages >= total_pages:
            conn.execute(
                "UPDATE books SET ingest_status = 'done', raw_pdf_data = NULL WHERE id = ?", (book_id,)
            )
            conn.commit()
            return jsonify({"status": "done", "total_pages": total_pages, "done_pages": total_pages})

        return jsonify({"status": "in_progress", "total_pages": total_pages, "done_pages": done_pages})
    except BookIngestError as e:
        conn.execute(
            "UPDATE books SET ingest_status = 'error', ingest_error = ?, raw_pdf_data = NULL WHERE id = ?",
            (str(e), book_id),
        )
        conn.commit()
        return jsonify({"status": "error", "error": str(e)})


@app.route("/books/<int:book_id>/delete", methods=["POST"])
def delete_book(book_id):
    conn = get_db()
    from_slug = request.form.get("from", "").strip()
    conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    conn.commit()
    flash("Кітап жойылды.", "ok")
    return redirect(url_for("books_list", **({"from": from_slug} if from_slug else {})))


@app.route("/weeks/<int:week_id>/delete", methods=["POST"])
def delete_week(week_id):
    conn = get_db()
    week = get_week_or_404(conn, week_id)
    stream_id = week["stream_id"] if week is not None else None
    conn.execute("DELETE FROM weeks WHERE id = ?", (week_id,))
    conn.commit()
    flash("Апта жойылды.", "ok")
    if stream_id is not None:
        return redirect(url_for("stream_detail", stream_id=stream_id))
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Week sub-pages (each a real page, sharing the sidebar layout)
# ---------------------------------------------------------------------------

@app.route("/weeks/<int:week_id>")
def week_overview(week_id):
    # "Шолу" беті алынып тасталды — апта ашылғанда тікелей импорт бетіне өтеді.
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/weeks/<int:week_id>/import")
def week_import(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    if is_month_summary_week(week, stream):
        flash(
            "Бұл — айлық ортақ апта, кестені осында импорттаудың қажеті жоқ. "
            "Нәтижелер осы айдың 1, 2, 3-апта импорттарынан автоматты түрде біріктіріледі.",
            "error",
        )
        return redirect(url_for("week_report", week_id=week_id))

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = 1 if week["curators_doc_url"] else 0

    return render_template(
        "import.html",
        week=week,
        stream=stream,
        program=program,
        active_page="import",
        result_count=result_count,
        note_count=note_count,
    )


@app.route("/weeks/<int:week_id>/report")
def week_report(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    is_summary = is_month_summary_week(week, stream)
    summary_text_override = None

    if is_summary:
        component_weeks = get_month_component_weeks(conn, week)
        combine_ids = [w["id"] for w in component_weeks]
        report = (
            compute_report(conn, week_id, combine_week_ids=combine_ids)
            if combine_ids
            else compute_report(conn, week_id)
        )
        result_count = report["total_entries"] if report else 0
        note_count = sum(1 for cw in component_weeks if cw["curators_doc_url"])
        curator_week_ids = combine_ids

        analyses = []
        for cw in component_weeks:
            if cw["curators_analysis_json"]:
                try:
                    analyses.append(json.loads(cw["curators_analysis_json"]))
                except (TypeError, ValueError):
                    pass
        curator_analysis = merge_analyses(analyses) if analyses else None
        if report and (report.get("has_data") or curator_analysis):
            summary_text_override = build_summary_text(report, curator_analysis)
    else:
        result_count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
        ).fetchone()["c"]
        note_count = 1 if week["curators_doc_url"] else 0
        report = compute_report(conn, week_id)
        curator_analysis = parse_curator_analysis(week)
        curator_week_ids = [week_id]

    # Ең жоғарғы/ең төменгі ортақ балл куратор — тек 2-айдан бастап: 1-айда
    # рейтинг кестелерінде куратор аты-жөндері бірізді жазылмаған болатын.
    best_curator = worst_curator = None
    if report and report.get("has_data") and week["month_number"] is not None and week["month_number"] >= 2:
        best_curator, worst_curator = compute_curator_extremes(conn, curator_week_ids)

    comparison = None
    if not is_summary and report and report.get("has_data"):
        prev_week = find_previous_week_with_data(conn, week, stream)
        if prev_week is not None:
            prev_report = compute_report(conn, prev_week["id"])
            if prev_report and prev_report.get("has_data"):
                comparison = compare_reports(report, prev_report)
                comparison["is_aylyq_test"] = bool(stream and stream["category"] == "aylyq_test")

    return render_template(
        "report.html",
        week=week,
        stream=stream,
        program=program,
        active_page="report",
        report=report,
        result_count=result_count,
        note_count=note_count,
        curator_analysis=curator_analysis,
        is_month_summary=is_summary,
        summary_text_override=summary_text_override,
        best_curator=best_curator,
        worst_curator=worst_curator,
        comparison=comparison,
    )


# ---------------------------------------------------------------------------
# Actions (POST) — each redirects back to the relevant page
# ---------------------------------------------------------------------------

@app.route("/weeks/<int:week_id>/summary/generate", methods=["POST"])
def generate_summary(week_id):
    conn = get_db()
    week, stream, _program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    if not week["curators_doc_text"]:
        flash("Алдымен «Куратор анализі» бетінде Google Docs сілтемесін қосыңыз.", "error")
        return redirect(url_for("week_report", week_id=week_id))

    try:
        report = compute_report(conn, week_id)
        analysis = generate_curator_analysis(week["curators_doc_text"], report=report)
    except CuratorAnalysisError as e:
        flash(f"Қорытынды анализ жасау сәтсіз аяқталды: {e}", "error")
        return redirect(url_for("week_report", week_id=week_id))

    label = "АТ" if stream and stream["category"] == "aylyq_test" else "СТ"
    summary_text = build_summary_text(report, analysis, label=label)
    conn.execute(
        "UPDATE weeks SET curators_analysis_json = ?, curators_analysis_error = NULL, summary = ? WHERE id = ?",
        (json.dumps(analysis, ensure_ascii=False), summary_text, week_id),
    )
    conn.commit()
    flash("Жалпы қорытынды AI арқылы жасалды.", "ok")
    return redirect(url_for("week_report", week_id=week_id))


@app.route("/weeks/<int:week_id>/import", methods=["POST"])
def import_sheet(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    if is_month_summary_week(week, stream):
        flash("Бұл — айлық ортақ апта, кестені осында импорттаудың қажеті жоқ.", "error")
        return redirect(url_for("week_report", week_id=week_id))

    urls = [u.strip() for u in request.form.getlist("sheet_url") if u.strip()]

    if not urls:
        flash("Кемінде бір рейтинг сілтемесін қосыңыз.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    default_max_score = db.score_defaults_for(program["slug"], stream["category"])[0] if program and stream else None

    total_inserted = 0
    total_skipped = 0
    ok_count = 0
    failed = []

    for sheet_url in urls:
        try:
            sheets = fetch_workbook(sheet_url)
        except SheetFetchError as e:
            failed.append((sheet_url, str(e)))
            continue

        curator_sheet_count = sum(1 for name, _ in sheets if not is_template_sheet(name))
        import_id = conn.execute(
            "INSERT INTO imports (week_id, sheet_url, sheet_count, row_count, skipped_count) "
            "VALUES (?, ?, ?, 0, 0) RETURNING id",
            (week_id, sheet_url, curator_sheet_count),
        ).fetchone()["id"]

        inserted = 0
        skipped = 0
        empty_sheets = 0

        for sheet_name, rows in sheets:
            if is_template_sheet(sheet_name):
                continue
            header, body = rows_to_dicts(rows)
            cols = guess_columns(header, body)
            idx_student = cols["student"]
            idx_subject = cols["subject"]
            idx_topic = cols["topic"]
            idx_score = cols["score"]
            idx_max_score = cols["max_score"]

            if idx_student is None or idx_score is None:
                empty_sheets += 1
                continue

            for row in body:
                def cell(idx):
                    if idx is None or idx >= len(row):
                        return ""
                    return row[idx].strip()

                student = cell(idx_student)
                if not student or is_summary_row(student):
                    continue

                score_raw = cell(idx_score)
                try:
                    score = float(score_raw.replace(",", "."))
                except ValueError:
                    skipped += 1
                    continue

                max_score_raw = cell(idx_max_score)
                if max_score_raw:
                    try:
                        max_score = float(max_score_raw.replace(",", "."))
                    except ValueError:
                        max_score = default_max_score
                else:
                    max_score = default_max_score

                subject = cell(idx_subject) or None
                topic = cell(idx_topic) or None

                conn.execute(
                    "INSERT INTO results (week_id, import_id, curator, student, subject, topic, score, max_score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (week_id, import_id, sheet_name, student, subject, topic, score, max_score),
                )
                inserted += 1

        conn.execute(
            "UPDATE imports SET row_count = ?, skipped_count = ? WHERE id = ?",
            (inserted, skipped, import_id),
        )
        total_inserted += inserted
        total_skipped += skipped
        ok_count += 1

    conn.commit()

    if failed:
        for url, err in failed:
            flash(f"{url}: {err}", "error")

    if ok_count:
        summary = f"{ok_count} рейтинг өңделді, {total_inserted} жол қосылды."
        if total_skipped:
            summary += f" {total_skipped} жол балл дұрыс емес болғандықтан өткізіп жіберілді."
        flash(summary, "ok")
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/weeks/<int:week_id>/results/clear", methods=["POST"])
def clear_results(week_id):
    conn = get_db()
    conn.execute("DELETE FROM results WHERE week_id = ?", (week_id,))
    conn.execute("DELETE FROM imports WHERE week_id = ?", (week_id,))
    conn.commit()
    flash("Осы аптаның барлық нәтижелері тазаланды.", "ok")
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/weeks/<int:week_id>/results/<int:result_id>/delete", methods=["POST"])
def delete_result(week_id, result_id):
    conn = get_db()
    conn.execute("DELETE FROM results WHERE id = ? AND week_id = ?", (result_id, week_id))
    conn.commit()
    return redirect(url_for("week_report", week_id=week_id))


@app.route("/weeks/<int:week_id>/notes", methods=["POST"])
def save_curators_doc(week_id):
    conn = get_db()
    week_row, stream_row, _program_row = get_week_context(conn, week_id)
    if week_row is not None and is_month_summary_week(week_row, stream_row):
        flash("Бұл — айлық ортақ апта, куратор құжатын осында қосудың қажеті жоқ.", "error")
        return redirect(url_for("week_report", week_id=week_id))

    doc_url = request.form.get("doc_url", "").strip()

    if not doc_url:
        flash("Google Docs сілтемесін енгізіңіз.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    doc_text = None
    fetch_error = None
    try:
        doc_text = strip_template_entry(fetch_doc_text(doc_url))
    except DocFetchError as e:
        fetch_error = str(e)

    analysis_json = None
    analysis_error = None
    summary_text = None
    if doc_text:
        try:
            report = compute_report(conn, week_id)
            analysis = generate_curator_analysis(doc_text, report=report)
            analysis_json = json.dumps(analysis, ensure_ascii=False)
            label = "АТ" if stream_row and stream_row["category"] == "aylyq_test" else "СТ"
            summary_text = build_summary_text(report, analysis, label=label)
        except CuratorAnalysisError as e:
            analysis_error = str(e)

    conn.execute(
        "UPDATE weeks SET curators_doc_url = ?, curators_doc_text = ?, curators_doc_fetch_error = ?, "
        "curators_analysis_json = ?, curators_analysis_error = ? WHERE id = ?",
        (doc_url, doc_text, fetch_error, analysis_json, analysis_error, week_id),
    )
    if summary_text:
        week = conn.execute("SELECT summary FROM weeks WHERE id = ?", (week_id,)).fetchone()
        if not week["summary"]:
            conn.execute("UPDATE weeks SET summary = ? WHERE id = ?", (summary_text, week_id))
    conn.commit()

    if fetch_error:
        flash(f"Сілтеме сақталды, бірақ құжатты жүктеу сәтсіз аяқталды: {fetch_error}", "error")
    elif analysis_error:
        flash(f"Мәтін жүктелді, бірақ талдау жасау сәтсіз аяқталды: {analysis_error}", "error")
    else:
        flash("Кураторлар анализі жаңартылды.", "ok")
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/weeks/<int:week_id>/notes/remove", methods=["POST"])
def remove_curators_doc(week_id):
    conn = get_db()
    conn.execute(
        "UPDATE weeks SET curators_doc_url = NULL, curators_doc_text = NULL, curators_doc_fetch_error = NULL, "
        "curators_analysis_json = NULL, curators_analysis_error = NULL, summary = NULL WHERE id = ?",
        (week_id,),
    )
    conn.commit()
    flash("Сілтеме алынып тасталды.", "ok")
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/weeks/<int:week_id>/summary/delete", methods=["POST"])
def delete_summary(week_id):
    conn = get_db()
    conn.execute("UPDATE weeks SET summary = NULL WHERE id = ?", (week_id,))
    conn.commit()
    flash("Жалпы қорытынды өшірілді.", "ok")
    return redirect(url_for("week_report", week_id=week_id))


@app.route("/weeks/<int:week_id>/league-thresholds", methods=["POST"])
def update_league_thresholds(week_id):
    conn = get_db()
    gold = request.form.get("gold_threshold", "").strip()
    silver = request.form.get("silver_threshold", "").strip()
    bronze = request.form.get("passing_score", "").strip()
    conn.execute(
        "UPDATE weeks SET gold_threshold = ?, silver_threshold = ?, passing_score = ? WHERE id = ?",
        (
            float(gold) if gold else None,
            float(silver) if silver else None,
            float(bronze) if bronze else None,
            week_id,
        ),
    )
    conn.commit()
    return redirect(url_for("week_report", week_id=week_id))


if __name__ == "__main__":
    # use_reloader=False: код өзгерген сайын серверді автоматты қайта іске
    # қоспайды (бұл кодты дамыту кезінде сайтта отырған адамды байланыс
    # үзіліп кетіп, басты бетке лақтырылуға әкелетін еді). Даму кезінде
    # серверді қайта қосу қажет болса, қолмен (мыс. Claude арқылы) қайта
    # іске қосылады.
    app.run(debug=True, port=5050, use_reloader=False)


