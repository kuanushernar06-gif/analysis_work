"""Апта нәтижелері бойынша ортақ анализ есептеу."""

DEFAULT_PASSING_PERCENT = 50.0
WORST_TOPICS_LIMIT = 10


def _percent(score, max_score):
    if score is None or max_score in (None, 0):
        return None
    try:
        return (float(score) / float(max_score)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def compute_report(conn, week_id):
    week = conn.execute("SELECT * FROM weeks WHERE id = ?", (week_id,)).fetchone()
    if week is None:
        return None

    results = conn.execute(
        "SELECT * FROM results WHERE week_id = ? ORDER BY subject, topic, student",
        (week_id,),
    ).fetchall()

    passing_percent = week["passing_score"] if week["passing_score"] is not None else DEFAULT_PASSING_PERCENT

    report = {
        "week": week,
        "passing_percent": passing_percent,
        "total_entries": len(results),
        "unique_students": 0,
        "overall_avg_score": None,
        "overall_avg_percent": None,
        "subjects": [],
        "fail_count_entries": 0,
        "fail_unique_students": 0,
        "fail_rows": [],
        "max_achievers": [],
        "max_achiever_count": 0,
        "worst_topics": [],
        "curator_notes": [],
        "notes_by_field": {
            "max_score_reasons": [],
            "mistaken_topics": [],
            "prep_factors": [],
            "low_score_reasons": [],
            "general_comment": [],
        },
        "has_data": len(results) > 0,
    }

    if not results:
        notes = conn.execute(
            "SELECT * FROM curator_notes WHERE week_id = ? ORDER BY curator", (week_id,)
        ).fetchall()
        report["curator_notes"] = notes
        for n in notes:
            for field in report["notes_by_field"]:
                val = n[field]
                if val and val.strip():
                    report["notes_by_field"][field].append({"curator": n["curator"], "text": val.strip()})
        return report

    students = set()
    scores = []
    percents = []
    fail_students = set()
    subject_map = {}
    topic_map = {}

    for r in results:
        student = (r["student"] or "").strip()
        subject = (r["subject"] or "Белгісіз пән").strip() or "Белгісіз пән"
        topic = (r["topic"] or "").strip()
        score = r["score"]
        max_score = r["max_score"]
        pct = _percent(score, max_score)

        if student:
            students.add(student)
        if score is not None:
            scores.append(float(score))
        if pct is not None:
            percents.append(pct)

        subj = subject_map.setdefault(
            subject, {"name": subject, "count": 0, "scores": [], "percents": [], "fail": 0}
        )
        subj["count"] += 1
        if score is not None:
            subj["scores"].append(float(score))
        if pct is not None:
            subj["percents"].append(pct)

        if pct is not None and pct < passing_percent:
            subj["fail"] += 1
            report["fail_count_entries"] += 1
            if student:
                fail_students.add(student)
            report["fail_rows"].append(
                {"student": student, "subject": subject, "topic": topic, "score": score, "max_score": max_score, "percent": round(pct, 1)}
            )

        if score is not None and max_score is not None and float(score) >= float(max_score) and float(max_score) > 0:
            report["max_achievers"].append({"student": student, "subject": subject, "topic": topic})

        if topic:
            t = topic_map.setdefault(topic, {"name": topic, "count": 0, "percents": []})
            t["count"] += 1
            if pct is not None:
                t["percents"].append(pct)

    report["unique_students"] = len(students)
    report["overall_avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
    report["overall_avg_percent"] = round(sum(percents) / len(percents), 1) if percents else None
    report["fail_unique_students"] = len(fail_students)
    report["max_achiever_count"] = len(report["max_achievers"])

    subj_list = []
    for s in subject_map.values():
        subj_list.append(
            {
                "name": s["name"],
                "count": s["count"],
                "avg_score": round(sum(s["scores"]) / len(s["scores"]), 2) if s["scores"] else None,
                "avg_percent": round(sum(s["percents"]) / len(s["percents"]), 1) if s["percents"] else None,
                "fail": s["fail"],
            }
        )
    subj_list.sort(key=lambda x: x["name"])
    report["subjects"] = subj_list

    topic_list = []
    for t in topic_map.values():
        if not t["percents"]:
            continue
        avg = sum(t["percents"]) / len(t["percents"])
        topic_list.append({"name": t["name"], "count": t["count"], "avg_percent": round(avg, 1)})
    topic_list.sort(key=lambda x: x["avg_percent"])
    report["worst_topics"] = topic_list[:WORST_TOPICS_LIMIT]

    notes = conn.execute(
        "SELECT * FROM curator_notes WHERE week_id = ? ORDER BY curator", (week_id,)
    ).fetchall()
    report["curator_notes"] = notes
    for n in notes:
        for field in report["notes_by_field"]:
            val = n[field]
            if val and val.strip():
                report["notes_by_field"][field].append({"curator": n["curator"], "text": val.strip()})

    return report
