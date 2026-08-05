"""Апта нәтижелері бойынша ортақ анализ есептеу."""

import re
from collections import Counter

DEFAULT_PASSING_PERCENT = 50.0
DEFAULT_GOLD_PERCENT = 90.0
DEFAULT_SILVER_PERCENT = 70.0
WORST_TOPICS_LIMIT = 10

# Ағын статистикасында "тұрақты үздік/тұрақты нашар" оқушылар тізіміне
# оқушының барлық апталар бойынша ОРТАША пайызы осы шектерге сәйкес келсе
# ғана түседі (Smart-тың 15 балдық шкаласында 12-15 балл ≈ ≥80%, 0-5 балл
# ≈ ≤33% болатындай таңдалған — бағдарламаға қарамастан пайызбен есептеледі).
STREAM_STATS_TOP_PERCENT = 80.0
STREAM_STATS_BOTTOM_PERCENT = 33.0
STREAM_STATS_MIN_WEEKS = 2


def _percent(score, max_score):
    if score is None or max_score in (None, 0):
        return None
    try:
        return (float(score) / float(max_score)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _reference_max_score(results):
    """Апта ішінде ең жиі кездесетін максимум баллды анықтайды — лига
    шектерін (алтын/күміс/қола) енді пайызбен емес, нақты балл түрінде
    енгізу үшін, сол баллды пайызға айналдыруға қажет."""
    values = [r["max_score"] for r in results if r["max_score"] not in (None, 0)]
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def _raw_threshold_to_percent(raw_value, ref_max_score, default_percent):
    if raw_value is None:
        return default_percent
    if not ref_max_score:
        return default_percent
    try:
        return (float(raw_value) / float(ref_max_score)) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        return default_percent


def compute_report(conn, week_id, combine_week_ids=None):
    """Апта бойынша (немесе, combine_week_ids берілсе, бірнеше апта нәтижесін
    біріктіріп) есеп жасайды. combine_week_ids — айлық ортақ апта сияқты,
    нәтижелер басқа апталардан жиналатын жағдайда пайдаланылады: week_id
    әлі де шектер/мақсат (thresholds/target) үшін негізгі апта ретінде
    қолданылады, бірақ results тек combine_week_ids тізіміндегі апталардан
    алынады."""
    week = conn.execute("SELECT * FROM weeks WHERE id = ?", (week_id,)).fetchone()
    if week is None:
        return None

    week_ids = combine_week_ids if combine_week_ids else [week_id]
    placeholders = ",".join("?" * len(week_ids))
    results = conn.execute(
        f"SELECT * FROM results WHERE week_id IN ({placeholders}) ORDER BY subject, topic, student",
        week_ids,
    ).fetchall()

    ref_max_score = _reference_max_score(results)
    passing_percent = _raw_threshold_to_percent(week["passing_score"], ref_max_score, DEFAULT_PASSING_PERCENT)
    gold_percent = _raw_threshold_to_percent(week["gold_threshold"], ref_max_score, DEFAULT_GOLD_PERCENT)
    silver_percent = _raw_threshold_to_percent(week["silver_threshold"], ref_max_score, DEFAULT_SILVER_PERCENT)

    report = {
        "week": week,
        "passing_percent": passing_percent,
        "gold_percent": gold_percent,
        "silver_percent": silver_percent,
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
        "max_achiever_students": 0,
        "zero_students": 0,
        "gold_count": 0,
        "silver_count": 0,
        "bronze_count": 0,
        "gold_share": 0.0,
        "silver_share": 0.0,
        "bronze_share": 0.0,
        "worst_topics": [],
        "has_data": len(results) > 0,
        "has_targets": False,
        "target_achievement_percent": None,
    }

    if not results:
        return report

    report["has_targets"] = week["target_score"] is not None

    students = set()
    scores = []
    percents = []
    fail_students = set()
    max_achiever_students = set()
    student_percents = {}
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
            if pct is not None:
                student_percents.setdefault(student, []).append(pct)
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
            if student:
                max_achiever_students.add(student)

        if topic:
            t = topic_map.setdefault(topic, {"name": topic, "count": 0, "percents": []})
            t["count"] += 1
            if pct is not None:
                t["percents"].append(pct)

    report["unique_students"] = len(students)
    report["overall_avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
    report["overall_avg_percent"] = round(sum(percents) / len(percents), 1) if percents else None
    if week["target_score"] and report["overall_avg_score"] is not None:
        report["target_achievement_percent"] = round(
            report["overall_avg_score"] / week["target_score"] * 100, 1
        )
    report["fail_unique_students"] = len(fail_students)
    report["max_achiever_count"] = len(report["max_achievers"])
    report["max_achiever_students"] = len(max_achiever_students)

    zero_students = 0
    gold_count = silver_count = bronze_count = 0
    for student_pcts in student_percents.values():
        avg_pct = sum(student_pcts) / len(student_pcts)
        if avg_pct == 0:
            zero_students += 1
        if avg_pct >= gold_percent:
            gold_count += 1
        elif avg_pct >= silver_percent:
            silver_count += 1
        elif avg_pct >= passing_percent:
            bronze_count += 1

    total_students = len(student_percents)
    report["zero_students"] = zero_students
    report["gold_count"] = gold_count
    report["silver_count"] = silver_count
    report["bronze_count"] = bronze_count
    if total_students:
        report["gold_share"] = round(gold_count / total_students * 100, 1)
        report["silver_share"] = round(silver_count / total_students * 100, 1)
        report["bronze_share"] = round(bronze_count / total_students * 100, 1)

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

    return report


def compute_stream_stats(conn, stream_id, max_score, target_score):
    """Бір ағынның (поток) БАРЛЫҚ апталарындағы СТ нәтижелерін біріктіріп,
    жалпы жиынтық көрсеткіштерді және оқушылардың осыған дейінгі СТ
    нәтижелерінің ортақ балы бойынша тұрақты үздік/тұрақты нашар тізімдерін
    есептейді (STREAM_STATS_MIN_WEEKS-тен кем апта нәтижесі бар оқушы
    тізімге түспейді — бір ғана сәтті/сәтсіз апта "тұрақты" болмайды)."""
    rows = conn.execute(
        "SELECT r.student, r.score, r.week_id FROM results r "
        "JOIN weeks w ON w.id = r.week_id WHERE w.stream_id = ? AND r.score IS NOT NULL",
        (stream_id,),
    ).fetchall()

    stats = {
        "has_data": False,
        "overall_avg_score": None,
        "target_achievement_percent": None,
        "top_student": None,
        "bottom_student": None,
        "top_students": [],
        "bottom_students": [],
    }
    if not rows:
        return stats

    stats["has_data"] = True
    scores = [float(r["score"]) for r in rows]
    stats["overall_avg_score"] = round(sum(scores) / len(scores), 2)
    if target_score:
        stats["target_achievement_percent"] = round(stats["overall_avg_score"] / target_score * 100, 1)

    student_scores = {}
    student_weeks = {}
    for r in rows:
        student = (r["student"] or "").strip()
        if not student:
            continue
        student_scores.setdefault(student, []).append(float(r["score"]))
        student_weeks.setdefault(student, set()).add(r["week_id"])

    student_avgs = []
    for student, vals in student_scores.items():
        avg_score = sum(vals) / len(vals)
        avg_percent = (avg_score / max_score * 100) if max_score else None
        student_avgs.append(
            {
                "student": student,
                "avg_score": round(avg_score, 2),
                "avg_percent": round(avg_percent, 1) if avg_percent is not None else None,
                "weeks": len(student_weeks[student]),
            }
        )

    if student_avgs:
        stats["top_student"] = max(student_avgs, key=lambda s: s["avg_score"])
        stats["bottom_student"] = min(student_avgs, key=lambda s: s["avg_score"])

    qualifying = [s for s in student_avgs if s["weeks"] >= STREAM_STATS_MIN_WEEKS and s["avg_percent"] is not None]
    stats["top_students"] = sorted(
        (s for s in qualifying if s["avg_percent"] >= STREAM_STATS_TOP_PERCENT),
        key=lambda s: s["avg_score"],
        reverse=True,
    )
    stats["bottom_students"] = sorted(
        (s for s in qualifying if s["avg_percent"] <= STREAM_STATS_BOTTOM_PERCENT),
        key=lambda s: s["avg_score"],
    )
    return stats


def compute_program_overview(conn, program_id):
    """Бағдарламаның (Smart/Junior) барлық потоктары мен апталарындағы осы
    уақытқа дейінгі барлық нәтижелерін біріктіріп, басты беттегі шолу
    карточкасы үшін жинақтайды. Лига бөлінісі әр аптаның жеке шегіне емес,
    сайттың әдепкі пайыздық шектеріне (алтын 90%, күміс 70%, қола 50%)
    негізделеді, себебі апталардың max_score/шектері әр түрлі болуы мүмкін."""
    rows = conn.execute(
        """
        SELECT r.student, r.curator, r.score, r.max_score, w.target_score
        FROM results r
        JOIN weeks w ON w.id = r.week_id
        JOIN streams s ON s.id = w.stream_id
        WHERE s.program_id = ?
        """,
        (program_id,),
    ).fetchall()

    overview = {
        "total_entries": len(rows),
        "unique_students": 0,
        "overall_avg_score": None,
        "overall_avg_percent": None,
        "target_achievement_percent": None,
        "top_curator": None,
        "bottom_curator": None,
        "gold_count": 0,
        "silver_count": 0,
        "bronze_count": 0,
        "gold_share": 0.0,
        "silver_share": 0.0,
        "bronze_share": 0.0,
        "has_data": len(rows) > 0,
    }
    if not rows:
        return overview

    scores = []
    percents = []
    target_ratios = []
    student_percents = {}
    student_seen = set()
    curator_scores = {}

    for r in rows:
        student = (r["student"] or "").strip()
        curator = (r["curator"] or "").strip()
        score = r["score"]
        max_score = r["max_score"]
        target = r["target_score"]
        pct = _percent(score, max_score)

        if student:
            student_seen.add(student)
        if score is not None:
            scores.append(float(score))
            if curator:
                curator_scores.setdefault(curator, []).append(float(score))
        if pct is not None:
            percents.append(pct)
            if student:
                student_percents.setdefault(student, []).append(pct)
        if score is not None and target:
            try:
                target_ratios.append(float(score) / float(target) * 100.0)
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    overview["unique_students"] = len(student_seen)
    overview["overall_avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
    overview["overall_avg_percent"] = round(sum(percents) / len(percents), 1) if percents else None
    if target_ratios:
        overview["target_achievement_percent"] = round(sum(target_ratios) / len(target_ratios), 1)

    curator_averages = [
        {"curator": name, "avg_score": round(sum(vals) / len(vals), 2)}
        for name, vals in curator_scores.items()
    ]
    if curator_averages:
        overview["top_curator"] = max(curator_averages, key=lambda c: c["avg_score"])
        overview["bottom_curator"] = min(curator_averages, key=lambda c: c["avg_score"])

    gold = silver = bronze = 0
    for pcts in student_percents.values():
        avg_pct = sum(pcts) / len(pcts)
        if avg_pct >= DEFAULT_GOLD_PERCENT:
            gold += 1
        elif avg_pct >= DEFAULT_SILVER_PERCENT:
            silver += 1
        elif avg_pct >= DEFAULT_PASSING_PERCENT:
            bronze += 1

    total_students = len(student_percents)
    overview["gold_count"], overview["silver_count"], overview["bronze_count"] = gold, silver, bronze
    if total_students:
        overview["gold_share"] = round(gold / total_students * 100, 1)
        overview["silver_share"] = round(silver / total_students * 100, 1)
        overview["bronze_share"] = round(bronze / total_students * 100, 1)

    return overview


_NAME_SPLIT_RE = re.compile(r"[.\s]+")


def _curator_name_tokens(name):
    """Атты бас әріпке келтіріп, бос орын мен нүкте бойынша сөздерге бөледі
    (мыс. 'І.Нұрайым' -> {'І', 'НҰРАЙЫМ'}, 'Ілияс Нұрайым Қайратқызы' ->
    {'ІЛИЯС', 'НҰРАЙЫМ', 'ҚАЙРАТҚЫЗЫ'}) — рейтинг кестесіндегі куратор аты
    (әдетте бір ғана есім) мен мұғалім тіркеген толық аты-жөнді салыстыру
    үшін, дәл сәйкестікті емес, ортақ сөз бар-жоғын тексереміз."""
    return {t for t in _NAME_SPLIT_RE.split((name or "").upper()) if len(t) > 1}


def compute_teacher_stats(conn, stream_id):
    """Осы ағынның (поток) нәтижелері бойынша, әр мұғалімнің өз
    кураторларының (teacher_curators-та тіркелген) ортақ балдарының
    орташасын мұғалімнің ортақ балы ретінде қайтарады. Куратор аты рейтинг
    кестесінде қысқа (мыс. бір есім) жазылатындықтан, мұғалімге тіркелген
    толық аты-жөнмен ДӘЛ сәйкес келуін емес, ортақ сөз (аты/тегі) бар-жоғын
    тексереміз. Осы ағында бірде-бір куратор нәтижесі жоқ мұғалімдер тізімге
    кірмейді."""
    rows = conn.execute(
        "SELECT r.curator, r.score FROM results r JOIN weeks w ON w.id = r.week_id "
        "WHERE w.stream_id = ? AND r.score IS NOT NULL AND r.curator IS NOT NULL AND r.curator != ''",
        (stream_id,),
    ).fetchall()

    curator_scores = {}
    for r in rows:
        curator_scores.setdefault(r["curator"].strip(), []).append(float(r["score"]))
    curator_avg = {name: sum(vals) / len(vals) for name, vals in curator_scores.items() if vals}
    curator_avg_tokens = {name: _curator_name_tokens(name) for name in curator_avg}

    teacher_rows = conn.execute(
        "SELECT t.id, t.name, tc.curator_name FROM teacher_curators tc "
        "JOIN teachers t ON t.id = tc.teacher_id"
    ).fetchall()

    by_teacher = {}
    for r in teacher_rows:
        by_teacher.setdefault((r["id"], r["name"]), []).append(r["curator_name"])

    teachers = []
    for (teacher_id, name), curator_names in by_teacher.items():
        matched_scores = []
        used_curators = set()
        for cname in curator_names:
            cname_tokens = _curator_name_tokens(cname)
            for actual_name, actual_tokens in curator_avg_tokens.items():
                if actual_name in used_curators:
                    continue
                if cname_tokens & actual_tokens:
                    matched_scores.append(curator_avg[actual_name])
                    used_curators.add(actual_name)
                    break
        if not matched_scores:
            continue
        teachers.append(
            {
                "id": teacher_id,
                "name": name,
                "avg_score": round(sum(matched_scores) / len(matched_scores), 2),
                "curator_count": len(matched_scores),
                "total_curators": len(curator_names),
            }
        )
    teachers.sort(key=lambda t: t["avg_score"], reverse=True)
    return teachers
