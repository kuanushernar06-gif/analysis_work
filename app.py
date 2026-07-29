import json
import os
import re
from datetime import timedelta

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, g, flash, session
from markupsafe import Markup, escape

load_dotenv()

import db
from analysis import compute_report
from sheets import fetch_workbook, rows_to_dicts, guess_columns, is_summary_row, is_template_sheet, SheetFetchError
from gdocs import (
    fetch_doc_text,
    DocFetchError,
    parse_weekly_plan,
    PlanParseError,
    classify_plan_sections,
    strip_template_entry,
)
from curator_analysis import generate_curator_analysis, build_summary_text, CuratorAnalysisError

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


@app.before_request
def require_login():
    if request.endpoint in ("login", "static") or request.endpoint is None:
        return
    if not session.get("logged_in"):
        return redirect(url_for("login", next=request.path))


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


@app.route("/")
def index():
    conn = get_db()
    programs = conn.execute("SELECT * FROM programs ORDER BY sort_order, id").fetchall()
    programs_with_counts = []
    for p in programs:
        stream_count = conn.execute(
            "SELECT COUNT(*) AS c FROM streams WHERE program_id = ?", (p["id"],)
        ).fetchone()["c"]
        week_count = conn.execute(
            "SELECT COUNT(*) AS c FROM weeks w JOIN streams s ON s.id = w.stream_id WHERE s.program_id = ?",
            (p["id"],),
        ).fetchone()["c"]
        programs_with_counts.append({"program": p, "stream_count": stream_count, "week_count": week_count})
    return render_template("index.html", programs=programs_with_counts)


@app.route("/programs/<slug>")
def program_detail(slug):
    conn = get_db()
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("index"))

    streams = conn.execute(
        "SELECT * FROM streams WHERE program_id = ? ORDER BY sort_order, id", (program["id"],)
    ).fetchall()
    streams_with_counts = []
    for s in streams:
        week_count = conn.execute(
            "SELECT COUNT(*) AS c FROM weeks WHERE stream_id = ?", (s["id"],)
        ).fetchone()["c"]
        streams_with_counts.append({"stream": s, "week_count": week_count})

    return render_template("program.html", program=program, streams=streams_with_counts)


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
                "(SELECT id FROM streams WHERE program_id = ?)",
                (content, month_num, week_num, program["id"]),
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
        "(SELECT id FROM streams WHERE program_id = ?)",
        (program["id"],),
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
    months = {}
    for w in weeks:
        result_count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (w["id"],)
        ).fetchone()["c"]
        note_count = 1 if w["curators_doc_url"] else 0
        months.setdefault(w["month_number"], []).append(
            {"week": w, "result_count": result_count, "note_count": note_count}
        )
    months_sorted = sorted(months.items(), key=lambda item: (item[0] is None, item[0]))

    return render_template("stream.html", stream=stream, program=program, months=months_sorted)


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

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = 1 if week["curators_doc_url"] else 0
    report = compute_report(conn, week_id)

    return render_template(
        "results.html",
        week=week,
        stream=stream,
        program=program,
        active_page="results",
        result_count=result_count,
        note_count=note_count,
        report=report,
    )


@app.route("/weeks/<int:week_id>/notes")
def week_notes(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

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
    )


# ---------------------------------------------------------------------------
# Actions (POST) — each redirects back to the relevant page
# ---------------------------------------------------------------------------

@app.route("/weeks/<int:week_id>/summary/generate", methods=["POST"])
def generate_summary(week_id):
    conn = get_db()
    week = get_week_or_404(conn, week_id)
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

    summary_text = build_summary_text(report, analysis)
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
    week = get_week_or_404(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    urls = [u.strip() for u in request.form.getlist("sheet_url")]
    max_scores = request.form.getlist("default_max_score")
    pairs = [
        (u, (max_scores[i].strip() if i < len(max_scores) else ""))
        for i, u in enumerate(urls)
        if u
    ]

    if not pairs:
        flash("Кемінде бір рейтинг сілтемесін қосыңыз.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    total_inserted = 0
    total_skipped = 0
    ok_count = 0
    failed = []

    for sheet_url, default_max_score in pairs:
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
                        max_score = float(default_max_score) if default_max_score else None
                else:
                    max_score = float(default_max_score) if default_max_score else None

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
            summary_text = build_summary_text(report, analysis)
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


@app.route("/weeks/<int:week_id>/target", methods=["POST"])
def update_week_target(week_id):
    conn = get_db()
    target = request.form.get("target_score", "").strip()
    conn.execute(
        "UPDATE weeks SET target_score = ? WHERE id = ?",
        (float(target) if target else None, week_id),
    )
    conn.commit()
    return redirect(url_for("week_import", week_id=week_id))


if __name__ == "__main__":
    # use_reloader=False: код өзгерген сайын серверді автоматты қайта іске
    # қоспайды (бұл кодты дамыту кезінде сайтта отырған адамды байланыс
    # үзіліп кетіп, басты бетке лақтырылуға әкелетін еді). Даму кезінде
    # серверді қайта қосу қажет болса, қолмен (мыс. Claude арқылы) қайта
    # іске қосылады.
    app.run(debug=True, port=5050, use_reloader=False)
