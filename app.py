import base64
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, g, flash, session, Response, jsonify
from markupsafe import Markup, escape

load_dotenv()

import db
from analysis import (
    compute_report,
    compute_curator_extremes,
    compare_reports,
    count_students_at_or_above,
    compute_teacher_stats_for_week,
    get_prior_year_comparison,
    compute_ls_teacher_data,
    compute_ls_stream_week_stats,
    _match_teacher_curators,
    _registered_name_candidates,
    _compact_name,
)
from sheets import (
    fetch_workbook,
    rows_to_dicts,
    guess_columns,
    is_summary_row,
    is_template_sheet,
    is_template_row,
    parse_results_file,
    parse_prior_year_report,
    parse_ls_report,
    SheetFetchError,
    CREATIVE_SUBJECT_KEYWORD,
    CREATIVE_HISTORY_SUFFIX,
    CREATIVE_LITERACY_SUFFIX,
    LS_STREAM_DISPLAY_NAMES,
)
from gdocs import (
    fetch_doc_text,
    DocFetchError,
    parse_weekly_plan,
    PlanParseError,
    classify_plan_sections,
    strip_template_entry,
)
from curator_analysis import (
    generate_curator_analysis,
    generate_baiqau_results_analysis,
    build_summary_text,
    build_baiqau_summary_text,
    merge_analyses,
    CuratorAnalysisError,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "juz40-local-dev-secret")
app.permanent_session_lifetime = timedelta(days=30)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

db.init_db()

_SUMMARY_LABEL_RE = re.compile(r"^([^:\n]{1,80}):(.*)$")

# Мұғалімдер статистикасы (апта бойынша, өз кураторларының ортақ балы) тек
# осы екі санатта керек — САБАҚ ТАПСЫРУ мен АЙЛЫҚ ТЕСТ. ДЕҢГЕЙЛІК/БАЙҚАУ
# ТЕСТ санатында бұл нәрсе қажет емес.
TEACHER_STATS_CATEGORIES = ("sabaq_tapsyru", "aylyq_test")


@app.template_filter("kk_num")
def kk_num(value):
    """Ондық бөлшекті '.' емес, қазақша/орысша дәстүр бойынша ',' арқылы
    көрсету үшін — санды (немесе '%.2f'|format секілді дайын жолды) сол
    қалпында, тек нүктені үтірге ауыстырып қайтарады."""
    if value is None:
        return "—"
    return str(value).replace(".", ",")


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


@app.template_filter("ls_stream_label")
def ls_stream_label(stream_code):
    """LS беттерінде ағым кодын көрсету үшін — Junior-дың кейбір потоктары
    ('ZEREK'/'USHQYN') LS кестесінде брендтік атаумен жазылатындықтан,
    ішкі кодтың (JUNIOR-01/JUNIOR-11) орнына сол атауды көрсетеді."""
    return LS_STREAM_DISPLAY_NAMES.get(stream_code, stream_code)


@app.context_processor
def inject_category_label():
    return {
        "category_label": lambda slug: db.CATEGORY_LABELS.get(slug, slug),
        "sidebar_categories": db.SIDEBAR_CATEGORIES,
        "teacher_stats_categories": TEACHER_STATS_CATEGORIES,
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


def _delta(current, prior):
    """compare_reports ішіндегі _cmp-пен бірдей пішінде (delta, state)
    қайтарады — 'Талдау' тайлдарындағы ▲/▼ көрсеткішін prior_year
    салыстыруында да қайта пайдалану үшін."""
    if current is None or prior is None:
        return None
    delta = round(current - prior, 2)
    if delta == 0:
        state = "same"
    elif delta > 0:
        state = "up"
    else:
        state = "down"
    return {"delta": delta, "state": state}


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


def find_previous_month_summary_with_data(conn, week, stream):
    """find_previous_week_with_data-мен бірдей логика, тек 'N-АЙ ОРТАҚ'
    (айлық ортақ) апталары үшін — ағымдағы айдың алдында, дәл осы ағында
    нақты нәтижесі бар ЕҢ СОҢҒЫ айлық ортақ аптаны табады (алдыңғы ай бос
    болса, одан да бұрынғысын қарастыра береді)."""
    if stream is None or stream["category"] != db.DEFAULT_CATEGORY:
        return None
    if week["week_number"] != db.WEEKS_PER_MONTH:
        return None

    rows = conn.execute(
        "SELECT * FROM weeks WHERE stream_id = ? AND week_number = ? AND month_number < ? "
        "ORDER BY month_number DESC",
        (week["stream_id"], db.WEEKS_PER_MONTH, week["month_number"] or 0),
    ).fetchall()
    for w in rows:
        component_ids = [cw["id"] for cw in get_month_component_weeks(conn, w)]
        if not component_ids:
            continue
        placeholders = ",".join("?" * len(component_ids))
        count = conn.execute(
            f"SELECT COUNT(*) AS c FROM results WHERE week_id IN ({placeholders}) AND score IS NOT NULL",
            component_ids,
        ).fetchone()["c"]
        if count > 0:
            return w
    return None


# ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатындағы 'Жалпы қорытынды'-да Шығармашылық
# (макс. 30) баллы бойынша неше оқушы осы межеден асқанын көрсету үшін.
CREATIVE_THRESHOLD_BANDS = (30, 28, 25, 20)


def compute_baiqau_subject_reports(conn, week_id, report):
    """ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатында нәтиже бөлек топтарға бөлінеді:
    'Шығармашылық' (пән атауында CREATIVE_SUBJECT_KEYWORD түбірі бар —
    'Шығармашылық' немесе қысқартылған 'Шығарым' нұсқасын да қамтиды,
    бірақ пән бойынша бөлек CREATIVE_HISTORY_SUFFIX/CREATIVE_LITERACY_SUFFIX
    жұрнағы жоқ — олар өз алдына бөлек есептеледі) және 'Тарих жалпы'
    (қалғандары) — жылдық отчеттегі 'ШЫҒАРМ' мен 'БАРЛЫҚ КОМБ ТАРИХ'
    парақтарына сәйкес. Қайтарады: (creative_report, creative_history_report,
    creative_literacy_report, general_report, creative_subjects)."""
    all_subjects = [s["name"] for s in (report.get("subjects") or [])]
    creative_history_subjects = [s for s in all_subjects if s.endswith(CREATIVE_HISTORY_SUFFIX)]
    creative_literacy_subjects = [s for s in all_subjects if s.endswith(CREATIVE_LITERACY_SUFFIX)]
    creative_subjects = [
        s for s in all_subjects
        if CREATIVE_SUBJECT_KEYWORD in s.upper()
        and s not in creative_history_subjects
        and s not in creative_literacy_subjects
    ]
    general_subjects = [
        s for s in all_subjects
        if s not in creative_subjects
        and s not in creative_history_subjects
        and s not in creative_literacy_subjects
    ]
    creative_report = (
        compute_report(conn, week_id, subjects_filter=creative_subjects) if creative_subjects else None
    )
    creative_history_report = (
        compute_report(conn, week_id, subjects_filter=creative_history_subjects)
        if creative_history_subjects else None
    )
    creative_literacy_report = (
        compute_report(conn, week_id, subjects_filter=creative_literacy_subjects)
        if creative_literacy_subjects else None
    )
    general_report = (
        compute_report(conn, week_id, subjects_filter=general_subjects) if general_subjects else None
    )
    return creative_report, creative_history_report, creative_literacy_report, general_report, creative_subjects


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ls")
def ls_home():
    return redirect(url_for("ls_overview"))


LS_PROGRAM_LABELS = {"smart": "Смарт", "junior": "Джуниор"}


def _run_ls_import(conn, sheet_url, program):
    """Берілген Google Sheets сілтемесінен, көрсетілген бағдарламаға (smart/
    junior) арналған LS деректерін оқып, сол бағдарламаның ескі деректерінің
    орнына жаңасын сақтайды (басқа бағдарламаның деректерін қозғамайды).
    Сәтті болса жазба санын, сәтсіз болса SheetFetchError қайтарады
    (шақырушы флэш хабарды өзі көрсетеді)."""
    entries = parse_ls_report(sheet_url, program)
    conn.execute("DELETE FROM ls_imports WHERE program = ?", (program,))
    import_id = conn.execute(
        "INSERT INTO ls_imports (sheet_url, row_count, program) VALUES (?, ?, ?) RETURNING id",
        (sheet_url, len(entries), program),
    ).fetchone()["id"]
    for e in entries:
        conn.execute(
            "INSERT INTO ls_sessions "
            "(import_id, session_date, teacher_name, stream_code, week_label, like_percent, attendance_percent) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                import_id, e["session_date"], e["teacher_name"], e["stream_code"],
                e["week_label"], e["like_percent"], e["attendance_percent"],
            ),
        )
    conn.commit()
    return len(entries)


@app.route("/ls/import", methods=["GET", "POST"])
def ls_import():
    conn = get_db()
    if request.method == "POST":
        program = request.form.get("program", "").strip()
        if program not in LS_PROGRAM_LABELS:
            flash("Бөлім анықталмады.", "error")
            return redirect(url_for("ls_import"))
        sheet_url = request.form.get("sheet_url", "").strip()
        if not sheet_url:
            flash("Сілтемені енгізіңіз.", "error")
            return redirect(url_for("ls_import"))
        try:
            count = _run_ls_import(conn, sheet_url, program)
        except SheetFetchError as e:
            flash(str(e), "error")
            return redirect(url_for("ls_import"))
        flash(f"{LS_PROGRAM_LABELS[program]}: {count} Live сабақ жазбасы импортталды.", "ok")
        return redirect(url_for("ls_import"))

    last_imports = {}
    for program in LS_PROGRAM_LABELS:
        last_imports[program] = conn.execute(
            "SELECT * FROM ls_imports WHERE program = ? ORDER BY id DESC LIMIT 1", (program,)
        ).fetchone()
    return render_template(
        "ls_import.html", ls_page=True, active_page="ls_import",
        last_import_smart=last_imports["smart"], last_import_junior=last_imports["junior"],
    )


@app.route("/ls/import/refresh", methods=["POST"])
def ls_import_refresh():
    """Смарт пен джуниордың соңғы жүктелген сілтемелерін қайта оқып,
    деректерін жаңартады — экзельге жаңа нәтиже қосылған сайын, пайдаланушы
    сілтемені қайта теріп жатпай, осы батырманы басу арқылы жаңартады."""
    conn = get_db()
    updated, errors = [], []
    for program, label in LS_PROGRAM_LABELS.items():
        row = conn.execute(
            "SELECT sheet_url FROM ls_imports WHERE program = ? ORDER BY id DESC LIMIT 1", (program,)
        ).fetchone()
        if row is None or not row["sheet_url"]:
            continue
        try:
            count = _run_ls_import(conn, row["sheet_url"], program)
            updated.append(f"{label}: {count}")
        except SheetFetchError as e:
            errors.append(f"{label}: {e}")

    if not updated and not errors:
        flash("Алдымен LS бағалау экзелінің сілтемесін жүктеңіз.", "error")
        return redirect(url_for("ls_import"))
    if updated:
        flash("Жаңартылды — " + "; ".join(updated), "ok")
    if errors:
        flash("Қате — " + "; ".join(errors), "error")
    return redirect(request.referrer or url_for("ls_teachers_page"))


@app.route("/ls/import/clear", methods=["POST"])
def ls_import_clear():
    conn = get_db()
    program = request.form.get("program", "").strip()
    if program in LS_PROGRAM_LABELS:
        conn.execute("DELETE FROM ls_imports WHERE program = ?", (program,))
        flash(f"{LS_PROGRAM_LABELS[program]} Live сабақ деректері тазаланды.", "ok")
    else:
        conn.execute("DELETE FROM ls_sessions")
        conn.execute("DELETE FROM ls_imports")
        flash("Live сабақ деректері тазаланды.", "ok")
    conn.commit()
    return redirect(url_for("ls_import"))


@app.route("/ls/overview")
def ls_overview():
    conn = get_db()
    rows = conn.execute(
        "SELECT stream_code, week_label, like_percent, attendance_percent FROM ls_sessions"
    ).fetchall()
    rows = [r for r in rows if not (r["like_percent"] is None and r["attendance_percent"] is None)]

    overall_like = _avg_percent([r["like_percent"] for r in rows])
    overall_attendance = _avg_percent([r["attendance_percent"] for r in rows])

    by_stream_rows = {}
    for r in rows:
        by_stream_rows.setdefault(r["stream_code"], []).append(r)
    stream_stats = []
    for code in sorted(by_stream_rows.keys()):
        stream_rows = by_stream_rows[code]
        stream_stats.append({
            "code": code,
            "session_count": len(stream_rows),
            "avg_like": _avg_percent([r["like_percent"] for r in stream_rows]),
            "avg_attendance": _avg_percent([r["attendance_percent"] for r in stream_rows]),
        })

    stream_week_stats = compute_ls_stream_week_stats(conn)
    # JS диаграммасына тікелей беру үшін — {stream_code: [{label, month, week, avg_like, avg_attendance}, ...]}
    chart_data = {
        code: [
            {
                "label": w["label"], "month": w["month"], "week": w["week"],
                "avg_like": w["avg_like"], "avg_attendance": w["avg_attendance"],
                "session_count": w["session_count"],
            }
            for w in weeks
        ]
        for code, weeks in stream_week_stats.items()
    }

    # Мұғалім бойынша (ағымға қарамай, барлық потоктағы мұғалімдер бір
    # тізімде) — Жалпы нәтиже бетіндегі 'Мұғалімдер бойынша' салыстыру
    # диаграммасы мен әр мұғалімнің үлкен ортақ көрсеткіші үшін.
    teacher_by_stream = compute_ls_teacher_data(conn)
    teacher_stats = []
    teacher_chart_data = {}
    for stream_code, teachers in teacher_by_stream.items():
        for t in teachers:
            key = f"{t['name']} ({stream_code})"
            teacher_stats.append({
                "key": key, "name": t["name"], "stream_code": stream_code,
                "avg_like": t["avg_like"], "avg_attendance": t["avg_attendance"],
                "session_count": t["session_count"],
            })
            teacher_chart_data[key] = [
                {"month": w["month"], "week": w["week"], "avg_like": w["avg_like"], "avg_attendance": w["avg_attendance"]}
                for w in t["weeks"]
            ]
    teacher_stats.sort(key=lambda t: (t["stream_code"], t["name"]))

    last_import = conn.execute("SELECT * FROM ls_imports ORDER BY id DESC LIMIT 1").fetchone()
    return render_template(
        "ls_overview.html", ls_page=True, active_page="ls_overview",
        session_count=len(rows), overall_like=overall_like, overall_attendance=overall_attendance,
        stream_stats=stream_stats,
        chart_data_json=json.dumps(chart_data, ensure_ascii=False),
        stream_stats_json=json.dumps(stream_stats, ensure_ascii=False),
        teacher_stats=teacher_stats,
        teacher_chart_data_json=json.dumps(teacher_chart_data, ensure_ascii=False),
        teacher_stats_json=json.dumps(teacher_stats, ensure_ascii=False),
        stream_display_names_json=json.dumps(LS_STREAM_DISPLAY_NAMES, ensure_ascii=False),
        last_import=last_import,
    )


# Мұғалімнің LS суреті — атымен сәйкестендіріледі (register_teacher/LS
# экзеліндегі мұғалім атауы сәл өзгеше жазылуы мүмкін болғандықтан,
# _compact_name арқылы салыстырамыз).
LS_TEACHER_PHOTOS = {
    "Шора Абай": "shora-abay.jpg",
    "Әбдіразақ Бердібек": "abdirazak-berdibek.jpg",
    "Әмірханов Әділет": "amirkhanov-adilet.jpg",
    "Өмірзақов Саян": "omirzakov-sayan.jpg",
    "Дарханбек Ермұхамед": "darkhanbek-ermukhamed.jpg",
    "Рзатаев Жантілек": "rzataev-zhantilek.jpg",
}
_LS_TEACHER_PHOTOS_COMPACT = {_compact_name(name): fname for name, fname in LS_TEACHER_PHOTOS.items()}


@app.route("/ls/teachers")
def ls_teachers_page():
    conn = get_db()
    by_stream = compute_ls_teacher_data(conn)
    for teachers in by_stream.values():
        for t in teachers:
            fname = _LS_TEACHER_PHOTOS_COMPACT.get(_compact_name(t["name"]))
            t["photo_url"] = url_for("static", filename=f"img/teachers/{fname}") if fname else None
    return render_template(
        "ls_teachers.html", ls_page=True, active_page="ls_teachers", by_stream=by_stream,
    )


def _avg_percent(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


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

    category_name = db.CATEGORY_LABELS[category_slug]
    programs = conn.execute("SELECT * FROM programs ORDER BY sort_order, id").fetchall()
    return render_template(
        "category_picker.html",
        category_slug=category_slug,
        category_name=category_name,
        programs=programs,
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
    return render_template(
        "program.html",
        program=program,
        columns=columns,
        current_category=category_slug,
        show_plan_card=category_slug is None or category_slug == db.DEFAULT_CATEGORY,
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


def _teachers_with_curators(conn):
    """Барлық мұғалімдерді, олардың кураторлар тізімі мен поток атауымен қоса
    қайтарады, поток (содан кейін аты) бойынша сұрыпталған — сайдбардағы
    топтастыру мен негізгі кестенің реті бірдей болу үшін."""
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

    teachers.sort(key=lambda t: (t["stream_label"] or "￿", t["name"]))
    return teachers


@app.route("/teachers")
def teachers_list():
    conn = get_db()
    teachers = _teachers_with_curators(conn)

    back_url = url_for("index")
    referrer = request.referrer
    if referrer:
        parsed = urlparse(referrer)
        if parsed.netloc == request.host:
            back_url = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    return render_template(
        "teachers.html",
        teachers=teachers,
        all_teachers=teachers,
        active_teacher_id=None,
        back_url=back_url,
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
    all_teachers = _teachers_with_curators(conn)

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


def _teacher_redirect_target():
    """/weeks/<id>/teachers сияқты жерлерден келген add/delete әрекеттері
    сол бетке қайта оралу үшін — қауіпсіздік үшін тек сайт ішіндегі
    салыстырмалы жолдарды ғана қабылдайды (ашық-редирект болмас үшін)."""
    next_url = request.form.get("next", "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return redirect(next_url)
    return redirect(url_for("teachers_list"))


@app.route("/teachers/add", methods=["POST"])
def add_teacher():
    conn = get_db()
    name = request.form.get("name", "").strip()
    curator_names_raw = request.form.get("curator_names", "")
    curator_names = [c.strip() for c in curator_names_raw.split("\n") if c.strip()]
    stream_id_raw = request.form.get("stream_id", "").strip()

    if not name:
        flash("Мұғалімнің атын енгізіңіз.", "error")
        return _teacher_redirect_target()

    if not stream_id_raw.isdigit():
        flash("Потокты таңдаңыз.", "error")
        return _teacher_redirect_target()
    stream_id = int(stream_id_raw)
    if conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone() is None:
        flash("Таңдалған поток табылмады.", "error")
        return _teacher_redirect_target()

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
    return _teacher_redirect_target()


@app.route("/teachers/<int:teacher_id>/delete", methods=["POST"])
def delete_teacher(teacher_id):
    conn = get_db()
    conn.execute("DELETE FROM teachers WHERE id = ?", (teacher_id,))
    conn.commit()
    flash("Мұғалім жойылды.", "ok")
    return _teacher_redirect_target()




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

    # Ең жоғарғы/ең төменгі ортақ балл куратор — ТАРИХ-01 ағымының (Шілде)
    # 1-айынан басқа әрдайым көрсетіледі: сол ағымда рейтинг кестелерінде
    # куратор аты-жөндері бірізді жазылмаған болатын (бағдарлама алғаш
    # іске қосылған кез), басқа барлық ағымдардың (Тамыз, Қыркүйек, т.б.)
    # 1-айында бұл мәселе жоқ.
    is_first_month_of_shilde = (
        stream is not None and stream["code"] == "ТАРИХ-01" and week["month_number"] == 1
    )
    best_curator = worst_curator = None
    if report and report.get("has_data") and week["month_number"] is not None and not is_first_month_of_shilde:
        best_curator, worst_curator = compute_curator_extremes(conn, curator_week_ids)

    comparison = None
    if not is_summary and report and report.get("has_data"):
        prev_week = find_previous_week_with_data(conn, week, stream)
        if prev_week is not None:
            prev_report = compute_report(conn, prev_week["id"])
            if prev_report and prev_report.get("has_data"):
                comparison = compare_reports(report, prev_report)
                comparison["is_aylyq_test"] = bool(stream and stream["category"] == "aylyq_test")
    elif is_summary and report and report.get("has_data"):
        prev_summary_week = find_previous_month_summary_with_data(conn, week, stream)
        if prev_summary_week is not None:
            prev_component_ids = [cw["id"] for cw in get_month_component_weeks(conn, prev_summary_week)]
            prev_report = (
                compute_report(conn, prev_summary_week["id"], combine_week_ids=prev_component_ids)
                if prev_component_ids
                else None
            )
            if prev_report and prev_report.get("has_data"):
                comparison = compare_reports(report, prev_report)
                comparison["is_month_summary"] = True

    creative_report = general_report = None
    creative_history_report = creative_literacy_report = None
    if stream and stream["category"] == "baiqau_test" and report and report.get("has_data"):
        creative_report, creative_history_report, creative_literacy_report, general_report, _creative_subjects = (
            compute_baiqau_subject_reports(conn, week_id, report)
        )

    prior_year = None
    if stream and week["month_number"] is not None and stream["category"] in ("sabaq_tapsyru", "baiqau_test"):
        prior_year = get_prior_year_comparison(conn, stream["category"], stream["code"], week["month_number"])
        if prior_year:
            if stream["category"] == "sabaq_tapsyru":
                current_score = report.get("overall_avg_score") if report and report.get("has_data") else None
            else:
                current_score = creative_report.get("overall_avg_score") if creative_report else None
            for entry in prior_year.values():
                entry["delta"] = _delta(current_score, entry["avg_score"])

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
        prior_year=prior_year,
        creative_report=creative_report,
        creative_history_report=creative_history_report,
        creative_literacy_report=creative_literacy_report,
        general_report=general_report,
    )


@app.route("/weeks/<int:week_id>/teachers")
def week_teachers(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))
    if stream is None or stream["category"] not in TEACHER_STATS_CATEGORIES:
        flash("Бұл санатта мұғалімдер статистикасы жоқ.", "error")
        return redirect(url_for("week_report", week_id=week_id))

    is_summary = is_month_summary_week(week, stream)
    if is_summary:
        combine_ids = [w["id"] for w in get_month_component_weeks(conn, week)]
        teachers = compute_teacher_stats_for_week(conn, week_id, combine_week_ids=combine_ids) if combine_ids else []
    else:
        teachers = compute_teacher_stats_for_week(conn, week_id)

    return render_template(
        "week_teachers.html",
        week=week,
        stream=stream,
        program=program,
        active_page="teachers",
        is_month_summary=is_summary,
        teachers=teachers,
        all_teachers=teachers,
    )


@app.route("/admin/debug-teacher-curators/<int:week_id>/<int:teacher_id>")
def debug_teacher_curators(week_id, teacher_id):
    """Куратор сәйкестендіру неге белгілі бір атауды таппай жатқанын
    диагностикалау үшін уақытша бет: осы аптадағы НАҚТЫ куратор
    аттарының бәрін және мұғалімнің тіркелген әр атауының қай кілтпен
    (candidates) іздегенін, нәтижесін дәл көрсетеді."""
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        return "Апта табылмады", 404

    week_ids = [week_id]
    if is_month_summary_week(week, stream):
        component_ids = [w["id"] for w in get_month_component_weeks(conn, week)]
        if component_ids:
            week_ids = component_ids

    placeholders = ",".join("?" * len(week_ids))
    actual_rows = conn.execute(
        f"SELECT DISTINCT curator FROM results WHERE week_id IN ({placeholders}) "
        "AND curator IS NOT NULL AND curator != ''",
        week_ids,
    ).fetchall()
    actual_names = sorted({r["curator"].strip() for r in actual_rows})

    teacher = conn.execute("SELECT * FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
    stream_id = stream["id"] if stream else (teacher["stream_id"] if teacher else None)

    curator_names_by_teacher = {}
    for r in conn.execute(
        "SELECT tc.teacher_id, tc.curator_name FROM teacher_curators tc "
        "JOIN teachers t ON t.id = tc.teacher_id WHERE t.stream_id = ?",
        (stream_id,),
    ).fetchall():
        curator_names_by_teacher.setdefault(r["teacher_id"], []).append(r["curator_name"])

    matches = _match_teacher_curators(curator_names_by_teacher, actual_names)
    teacher_matches = matches.get(teacher_id, {})
    registered = curator_names_by_teacher.get(teacher_id, [])
    all_claimed = {name for m in matches.values() for name in m.values()}

    lines = [
        f"Апта(лар) ID: {week_ids}",
        f"Мұғалім: {teacher['name'] if teacher else teacher_id} (id={teacher_id})",
        "",
        f"=== Осы апта(лар)дағы БАРЛЫҚ нақты куратор аттары ({len(actual_names)}) ===",
    ]
    for n in actual_names:
        claimed_note = "" if n in all_claimed else "   <<< ЕШКІМГЕ СӘЙКЕСТЕНДІРІЛМЕГЕН"
        lines.append(f"  {n!r}{claimed_note}")

    lines.append("")
    lines.append(f"=== {teacher['name'] if teacher else teacher_id} тіркелген кураторлары ({len(registered)}) ===")
    for cname in registered:
        matched = teacher_matches.get(cname)
        candidates = _registered_name_candidates(cname)
        status = f"САЙКЕС ТАПТЫ -> {matched!r}" if matched else "ТАБЫЛМАДЫ"
        lines.append(f"  {cname!r}: {status}")
        lines.append(f"      ізделген кілттер: {candidates}")

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


@app.route("/admin/debug-week-subjects/<int:week_id>")
def debug_week_subjects(week_id):
    """ДТ/БТ аптасында 'Шығармашылық' парағы неге көрінбей жатқанын
    диагностикалау үшін уақытша бет: осы аптадағы БАРЛЫҚ пән (subject)
    атауларын, әрқайсысының жол санын және max_score-ын көрсетеді."""
    conn = get_db()
    week, stream, _program = get_week_context(conn, week_id)
    if week is None:
        return "Апта табылмады", 404

    rows = conn.execute(
        "SELECT subject, max_score, COUNT(*) AS c FROM results "
        "WHERE week_id = ? GROUP BY subject, max_score ORDER BY subject",
        (week_id,),
    ).fetchall()

    lines = [f"Апта ID: {week_id} ({week['title']})", f"=== Пәндер ({len(rows)}) ==="]
    for r in rows:
        creative = CREATIVE_SUBJECT_KEYWORD in (r["subject"] or "").upper()
        lines.append(f"  {r['subject']!r}: {r['c']} жол, max_score={r['max_score']}, шығармашылық={creative}")

    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8")


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


@app.route("/weeks/<int:week_id>/summary/generate-from-results", methods=["POST"])
def generate_results_summary(week_id):
    """ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатының 'Жалпы қорытындысы' — report.html-де
    тек stream['category'] == 'baiqau_test' болғанда ғана шақырылады.
    Шығармашылық пен Жалпы тарихты бір мидай орташаға араластырмай, бөлек
    сандық көрсеткіштермен (build_baiqau_summary_text) құрастырады."""
    conn = get_db()
    week, _stream, _program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    report = compute_report(conn, week_id)
    if not report or not report.get("has_data"):
        flash("Алдымен кестені импорттаңыз.", "error")
        return redirect(url_for("week_report", week_id=week_id))

    creative_report, creative_history_report, creative_literacy_report, general_report, creative_subjects = (
        compute_baiqau_subject_reports(conn, week_id, report)
    )
    creative_thresholds = (
        count_students_at_or_above(conn, week_id, creative_subjects, CREATIVE_THRESHOLD_BANDS)
        if creative_subjects else None
    )

    try:
        analysis_text = generate_baiqau_results_analysis(
            creative_report, creative_history_report, creative_literacy_report, general_report, creative_thresholds
        )
    except CuratorAnalysisError as e:
        flash(f"Қорытынды анализ жасау сәтсіз аяқталды: {e}", "error")
        return redirect(url_for("week_report", week_id=week_id))

    summary_text = build_baiqau_summary_text(
        creative_report, creative_history_report, creative_literacy_report, general_report,
        creative_thresholds, analysis_text,
    )

    conn.execute("UPDATE weeks SET summary = ? WHERE id = ?", (summary_text, week_id))
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
                if not student or is_summary_row(student) or is_template_row(student):
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


@app.route("/weeks/<int:week_id>/results/upload", methods=["POST"])
def upload_results_file(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))
    if stream is None or stream["category"] != "baiqau_test":
        flash("Бұл әрекет тек ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатында қолжетімді.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    file = request.files.get("results_file")
    if file is None or not file.filename:
        flash("Файл таңдаңыз.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    try:
        entries = parse_results_file(file.read())
    except SheetFetchError as e:
        flash(str(e), "error")
        return redirect(url_for("week_import", week_id=week_id))

    default_max_score = db.score_defaults_for(program["slug"], stream["category"])[0] if program else None

    import_id = conn.execute(
        "INSERT INTO imports (week_id, sheet_url, sheet_count, row_count, skipped_count) "
        "VALUES (?, ?, 1, 0, 0) RETURNING id",
        (week_id, file.filename),
    ).fetchone()["id"]

    for entry in entries:
        conn.execute(
            "INSERT INTO results (week_id, import_id, curator, student, subject, topic, score, max_score) "
            "VALUES (?, ?, NULL, ?, ?, NULL, ?, ?)",
            (week_id, import_id, entry["student"], entry["subject"], entry["score"], entry.get("max_score", default_max_score)),
        )
    conn.execute("UPDATE imports SET row_count = ? WHERE id = ?", (len(entries), import_id))
    conn.commit()

    flash(f"Файл өңделді, {len(entries)} нәтиже қосылды.", "ok")
    return redirect(url_for("week_import", week_id=week_id))


@app.route("/admin/import-prior-year", methods=["POST"])
def import_prior_year():
    """Жылдық отчет (Google Sheets) кестесінен СТ/ДТ/БТ поток-ай орташа
    баллдарын оқып, prior_year_stats кестесіне сақтайды — Ортақ анализ
    беттеріндегі 'өткен жылмен салыстыру' осыдан алынады. UI-де форма жоқ,
    admin өзі curl-мен sheet_url + academic_year жіберіп іске қосады."""
    sheet_url = request.form.get("sheet_url", "").strip()
    academic_year = request.form.get("academic_year", "").strip()
    if not sheet_url or not academic_year:
        flash("Сілтеме мен оқу жылын көрсетіңіз.", "error")
        return redirect(url_for("index"))

    try:
        stats = parse_prior_year_report(sheet_url)
    except SheetFetchError as e:
        flash(f"Жылдық отчетті оқу сәтсіз аяқталды: {e}", "error")
        return redirect(url_for("index"))

    conn = get_db()
    conn.execute("DELETE FROM prior_year_stats WHERE academic_year = ?", (academic_year,))
    for category, stream_code, month_number, avg_score in stats:
        conn.execute(
            "INSERT INTO prior_year_stats (academic_year, category, stream_code, month_number, avg_score) "
            "VALUES (?, ?, ?, ?, ?)",
            (academic_year, category, stream_code, month_number, avg_score),
        )
    conn.commit()

    flash(f"Жылдық отчет импортталды: {len(stats)} жол ({academic_year}).", "ok")
    return redirect(url_for("index"))


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


