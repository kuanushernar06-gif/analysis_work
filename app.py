import os

from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, jsonify, g, flash

load_dotenv()

import db
from analysis import compute_report
from sheets import fetch_csv_rows, rows_to_dicts, SheetFetchError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "juz40-local-dev-secret")

db.init_db()

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


@app.route("/programs/<slug>/streams", methods=["POST"])
def add_stream(slug):
    conn = get_db()
    program = conn.execute("SELECT * FROM programs WHERE slug = ?", (slug,)).fetchone()
    if program is None:
        flash("Бағдарлама табылмады.", "error")
        return redirect(url_for("index"))

    code = request.form.get("code", "").strip()
    if not code:
        flash("Поток атауын енгізіңіз.", "error")
        return redirect(url_for("program_detail", slug=slug))

    existing = conn.execute(
        "SELECT id FROM streams WHERE program_id = ? AND code = ?", (program["id"], code)
    ).fetchone()
    if existing is not None:
        flash(f"«{code}» поток бұрыннан бар.", "error")
        return redirect(url_for("program_detail", slug=slug))

    max_order = conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) AS m FROM streams WHERE program_id = ?", (program["id"],)
    ).fetchone()["m"]
    conn.execute(
        "INSERT INTO streams (program_id, code, sort_order) VALUES (?, ?, ?)",
        (program["id"], code, max_order + 1),
    )
    conn.commit()
    flash(f"«{code}» поток қосылды.", "ok")
    return redirect(url_for("program_detail", slug=slug))


@app.route("/streams/<int:stream_id>")
def stream_detail(stream_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))
    program = conn.execute("SELECT * FROM programs WHERE id = ?", (stream["program_id"],)).fetchone()

    weeks = conn.execute(
        "SELECT * FROM weeks WHERE stream_id = ? ORDER BY id DESC", (stream_id,)
    ).fetchall()
    weeks_with_counts = []
    for w in weeks:
        result_count = conn.execute(
            "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (w["id"],)
        ).fetchone()["c"]
        note_count = conn.execute(
            "SELECT COUNT(*) AS c FROM curator_notes WHERE week_id = ?", (w["id"],)
        ).fetchone()["c"]
        weeks_with_counts.append({"week": w, "result_count": result_count, "note_count": note_count})

    return render_template("stream.html", stream=stream, program=program, weeks=weeks_with_counts)


@app.route("/streams/<int:stream_id>/weeks", methods=["POST"])
def create_week(stream_id):
    conn = get_db()
    stream = conn.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if stream is None:
        flash("Поток табылмады.", "error")
        return redirect(url_for("index"))

    title = request.form.get("title", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    passing_score = request.form.get("passing_score", "").strip()

    if not title:
        flash("Апта атауын енгізіңіз.", "error")
        return redirect(url_for("stream_detail", stream_id=stream_id))

    week_id = conn.execute(
        "INSERT INTO weeks (stream_id, title, start_date, end_date, passing_score) "
        "VALUES (?, ?, ?, ?, ?) RETURNING id",
        (stream_id, title, start_date or None, end_date or None, float(passing_score) if passing_score else None),
    ).fetchone()["id"]
    conn.commit()
    return redirect(url_for("week_overview", week_id=week_id))


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
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = conn.execute(
        "SELECT COUNT(*) AS c FROM curator_notes WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    report = compute_report(conn, week_id)

    return render_template(
        "overview.html",
        week=week,
        stream=stream,
        program=program,
        active_page="overview",
        result_count=result_count,
        note_count=note_count,
        report=report,
    )


@app.route("/weeks/<int:week_id>/import")
def week_import(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    imports = conn.execute(
        "SELECT * FROM imports WHERE week_id = ? ORDER BY id DESC", (week_id,)
    ).fetchall()
    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = conn.execute(
        "SELECT COUNT(*) AS c FROM curator_notes WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]

    return render_template(
        "import.html",
        week=week,
        stream=stream,
        program=program,
        active_page="import",
        imports=imports,
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

    results = conn.execute(
        "SELECT * FROM results WHERE week_id = ? ORDER BY id DESC LIMIT 300", (week_id,)
    ).fetchall()
    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = conn.execute(
        "SELECT COUNT(*) AS c FROM curator_notes WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]

    return render_template(
        "results.html",
        week=week,
        stream=stream,
        program=program,
        active_page="results",
        results=results,
        result_count=result_count,
        note_count=note_count,
    )


@app.route("/weeks/<int:week_id>/notes")
def week_notes(week_id):
    conn = get_db()
    week, stream, program = get_week_context(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    notes = conn.execute(
        "SELECT * FROM curator_notes WHERE week_id = ? ORDER BY id DESC", (week_id,)
    ).fetchall()
    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = len(notes)

    return render_template(
        "notes.html",
        week=week,
        stream=stream,
        program=program,
        active_page="notes",
        notes=notes,
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

    result_count = conn.execute(
        "SELECT COUNT(*) AS c FROM results WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    note_count = conn.execute(
        "SELECT COUNT(*) AS c FROM curator_notes WHERE week_id = ?", (week_id,)
    ).fetchone()["c"]
    report = compute_report(conn, week_id)

    return render_template(
        "report.html",
        week=week,
        stream=stream,
        program=program,
        active_page="report",
        report=report,
        result_count=result_count,
        note_count=note_count,
    )


# ---------------------------------------------------------------------------
# Actions (POST) — each redirects back to the relevant page
# ---------------------------------------------------------------------------

@app.route("/weeks/<int:week_id>/summary", methods=["POST"])
def save_summary(week_id):
    conn = get_db()
    summary = request.form.get("summary", "")
    conn.execute("UPDATE weeks SET summary = ? WHERE id = ?", (summary, week_id))
    conn.commit()
    flash("Жалпы қорытынды сақталды.", "ok")
    return redirect(url_for("week_report", week_id=week_id))


@app.route("/api/sheet-preview")
def sheet_preview():
    sheet_url = request.args.get("sheet_url", "")
    try:
        rows = fetch_csv_rows(sheet_url)
    except SheetFetchError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    header, body = rows_to_dicts(rows)
    preview_rows = body[:5]
    return jsonify(
        {
            "ok": True,
            "header": header,
            "preview_rows": preview_rows,
            "total_rows": len(body),
        }
    )


@app.route("/weeks/<int:week_id>/import", methods=["POST"])
def import_sheet(week_id):
    conn = get_db()
    week = get_week_or_404(conn, week_id)
    if week is None:
        flash("Апта табылмады.", "error")
        return redirect(url_for("index"))

    sheet_url = request.form.get("sheet_url", "").strip()
    fixed_curator = request.form.get("fixed_curator", "").strip()
    col_student = request.form.get("col_student", "")
    col_subject = request.form.get("col_subject", "")
    col_topic = request.form.get("col_topic", "")
    col_score = request.form.get("col_score", "")
    col_max_score = request.form.get("col_max_score", "")
    col_curator = request.form.get("col_curator", "")
    default_max_score = request.form.get("default_max_score", "").strip()

    try:
        rows = fetch_csv_rows(sheet_url)
    except SheetFetchError as e:
        flash(str(e), "error")
        return redirect(url_for("week_import", week_id=week_id))

    header, body = rows_to_dicts(rows)

    def col_index(name):
        if not name:
            return None
        try:
            return header.index(name)
        except ValueError:
            return None

    idx_student = col_index(col_student)
    idx_subject = col_index(col_subject)
    idx_topic = col_index(col_topic)
    idx_score = col_index(col_score)
    idx_max_score = col_index(col_max_score)
    idx_curator = col_index(col_curator)

    if idx_student is None or idx_score is None:
        flash("Кемінде 'Оқушы аты-жөні' және 'Балл' бағандарын таңдау керек.", "error")
        return redirect(url_for("week_import", week_id=week_id))

    inserted = 0
    skipped = 0
    import_id = conn.execute(
        "INSERT INTO imports (week_id, sheet_url, curator, row_count, skipped_count) VALUES (?, ?, ?, 0, 0) RETURNING id",
        (week_id, sheet_url, fixed_curator or None),
    ).fetchone()["id"]

    for row in body:
        def cell(idx):
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        student = cell(idx_student)
        if not student:
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
        curator = cell(idx_curator) or fixed_curator or None

        conn.execute(
            "INSERT INTO results (week_id, import_id, curator, student, subject, topic, score, max_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (week_id, import_id, curator, student, subject, topic, score, max_score),
        )
        inserted += 1

    conn.execute(
        "UPDATE imports SET row_count = ?, skipped_count = ? WHERE id = ?",
        (inserted, skipped, import_id),
    )
    conn.commit()

    if skipped:
        flash(f"{inserted} жол сәтті қосылды, {skipped} жол балл дұрыс емес болғандықтан өткізіп жіберілді.", "ok")
    else:
        flash(f"{inserted} жол сәтті қосылды.", "ok")
    return redirect(url_for("week_results", week_id=week_id))


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
def add_note(week_id):
    conn = get_db()
    curator = request.form.get("curator", "").strip()
    if not curator:
        flash("Куратор атын енгізіңіз.", "error")
        return redirect(url_for("week_notes", week_id=week_id))

    conn.execute(
        """INSERT INTO curator_notes
           (week_id, curator, max_score_reasons, mistaken_topics, prep_factors, low_score_reasons, general_comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            week_id,
            curator,
            request.form.get("max_score_reasons", "").strip(),
            request.form.get("mistaken_topics", "").strip(),
            request.form.get("prep_factors", "").strip(),
            request.form.get("low_score_reasons", "").strip(),
            request.form.get("general_comment", "").strip(),
        ),
    )
    conn.commit()
    flash(f"{curator} кураторының анализі қосылды.", "ok")
    return redirect(url_for("week_notes", week_id=week_id))


@app.route("/weeks/<int:week_id>/notes/<int:note_id>/delete", methods=["POST"])
def delete_note(week_id, note_id):
    conn = get_db()
    conn.execute("DELETE FROM curator_notes WHERE id = ? AND week_id = ?", (note_id, week_id))
    conn.commit()
    return redirect(url_for("week_notes", week_id=week_id))


@app.route("/weeks/<int:week_id>/passing-score", methods=["POST"])
def update_passing_score(week_id):
    conn = get_db()
    passing_score = request.form.get("passing_score", "").strip()
    conn.execute(
        "UPDATE weeks SET passing_score = ? WHERE id = ?",
        (float(passing_score) if passing_score else None, week_id),
    )
    conn.commit()
    return redirect(url_for("week_report", week_id=week_id))


if __name__ == "__main__":
    app.run(debug=True, port=5050)
