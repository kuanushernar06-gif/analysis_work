import json
import os
import re
from datetime import datetime, timedelta

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, g, flash, session, Response
from markupsafe import Markup, escape

load_dotenv()

import db
from analysis import compute_report, compute_stream_stats, compute_teacher_stats, compute_teacher_stream_detail
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
    return render_template(
        "category_picker.html",
        category_slug=category_slug,
        category_name=category_name,
        programs=programs,
        mode=mode,
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


@app.route("/streams/<int:stream_id>/stats")
def stream_stats(stream_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    max_score, target_score = db.score_defaults_for(program["slug"], stream["category"])
    stats = compute_stream_stats(conn, stream_id, max_score=max_score, target_score=target_score)

    # Осы поток кодының (мыс. ТАРИХ-01) басқа санаттардағы (СТ/АТ) сыңар
    # жазбалары — сайдбардан санат ауыстырғанда сол потоктың статистикасына
    # тікелей өту үшін.
    stream_stats_categories = {
        row["category"]: row["id"]
        for row in conn.execute(
            "SELECT id, category FROM streams WHERE program_id = ? AND code = ?",
            (stream["program_id"], stream["code"]),
        ).fetchall()
    }

    return render_template(
        "stream_stats.html",
        stream=stream,
        program=program,
        stats=stats,
        stream_stats_categories=stream_stats_categories,
    )


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


@app.route("/teachers")
def teachers_list():
    conn = get_db()
    rows = conn.execute(
        "SELECT t.id, t.name, "
        "array_agg(tc.curator_name) AS curator_names "
        "FROM teachers t LEFT JOIN teacher_curators tc ON tc.teacher_id = t.id "
        "GROUP BY t.id, t.name ORDER BY t.name"
        if getattr(conn, "backend", None) == "postgres"
        else "SELECT id, name FROM teachers ORDER BY name"
    ).fetchall()

    if getattr(conn, "backend", None) == "postgres":
        teachers = [
            {"id": r["id"], "name": r["name"], "curator_names": [c for c in (r["curator_names"] or []) if c]}
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
                {"id": r["id"], "name": r["name"], "curator_names": [c["curator_name"] for c in curator_rows]}
            )

    return render_template("teachers.html", teachers=teachers)


@app.route("/teachers/add", methods=["POST"])
def add_teacher():
    conn = get_db()
    name = request.form.get("name", "").strip()
    curator_names_raw = request.form.get("curator_names", "")
    curator_names = [c.strip() for c in curator_names_raw.split("\n") if c.strip()]

    if not name:
        flash("Мұғалімнің атын енгізіңіз.", "error")
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
        "INSERT INTO teachers (name) VALUES (?) RETURNING id", (name,)
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
        return redirect(url_for("week_results", week_id=week_id))

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


@app.route("/weeks/<int:week_id>/results")
def week_results(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    is_summary = is_month_summary_week(week, stream)
    if is_summary:
        combine_ids = [w["id"] for w in get_month_component_weeks(conn, week)]
        if combine_ids:
            placeholders = ",".join("?" * len(combine_ids))
            result_count = conn.execute(
                f"SELECT COUNT(*) AS c FROM results WHERE week_id IN ({placeholders})", combine_ids
            ).fetchone()["c"]
            report = compute_report(conn, week_id, combine_week_ids=combine_ids)
        else:
            result_count = 0
            report = compute_report(conn, week_id)
    else:
        result_count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
        ).fetchone()["c"]
        report = compute_report(conn, week_id)
    note_count = 1 if week["curators_doc_url"] else 0

    return render_template(
        "results.html",
        week=week,
        stream=stream,
        program=program,
        active_page="results",
        result_count=result_count,
        note_count=note_count,
        report=report,
        is_month_summary=is_summary,
    )


@app.route("/weeks/<int:week_id>/notes")
def week_notes(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    if is_month_summary_week(week, stream):
        flash(
            "Бұл — айлық ортақ апта, кураторлар анализі осы айдың 1, 2, 3-апта "
            "анализдерінен автоматты түрде біріктіріледі. Толық көру үшін «Ортақ анализ» бетін ашыңыз.",
            "error",
        )
        return redirect(url_for("week_report", week_id=week_id))

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = 1 if week["curators_doc_url"] else 0
    curator_analysis = parse_curator_analysis(week)

    return render_template(
        "notes.html",
        week=week,
        stream=stream,
        program=program,
        active_page="notes",
        result_count=result_count,
        note_count=note_count,
        curator_analysis=curator_analysis,
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
        return redirect(url_for("week_results", week_id=week_id))

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
    return redirect(url_for("week_results", week_id=week_id))


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
        return redirect(url_for("week_notes", week_id=week_id))

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
    return redirect(url_for("week_notes", week_id=week_id))


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
    return redirect(url_for("week_notes", week_id=week_id))


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
    return redirect(url_for("week_results", week_id=week_id))


if __name__ == "__main__":
    # use_reloader=False: код өзгерген сайын серверді автоматты қайта іске
    # қоспайды (бұл кодты дамыту кезінде сайтта отырған адамды байланыс
    # үзіліп кетіп, басты бетке лақтырылуға әкелетін еді). Даму кезінде
    # серверді қайта қосу қажет болса, қолмен (мыс. Claude арқылы) қайта
    # іске қосылады.
    app.run(debug=True, port=5050, use_reloader=False)
