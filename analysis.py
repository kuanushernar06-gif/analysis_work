"""Апта нәтижелері бойынша ортақ анализ есептеу."""

import re
from collections import Counter
from datetime import datetime

from db import PROGRAM_MAX_SCORE

DEFAULT_PASSING_PERCENT = 50.0
DEFAULT_GOLD_PERCENT = 90.0
DEFAULT_SILVER_PERCENT = 70.0
WORST_TOPICS_LIMIT = 10

# ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатында өту шегі пәнге қарамастан 5 балл деп
# бекітілген (curator қолмен "Қола шегі" енгізетін UI сол санатта жоқ,
# сондықтан week['passing_score'] бос болғанда осы әдепкі қолданылады).
CATEGORY_PASSING_SCORE_DEFAULTS = {"baiqau_test": 5}


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


def compute_report(conn, week_id, combine_week_ids=None, subjects_filter=None, curators_filter=None):
    """Апта бойынша (немесе, combine_week_ids берілсе, бірнеше апта нәтижесін
    біріктіріп) есеп жасайды. combine_week_ids — айлық ортақ апта сияқты,
    нәтижелер басқа апталардан жиналатын жағдайда пайдаланылады: week_id
    әлі де шектер/мақсат (thresholds/target) үшін негізгі апта ретінде
    қолданылады, бірақ results тек combine_week_ids тізіміндегі апталардан
    алынады. subjects_filter берілсе (ДТ/БТ-да 'Шығармашылық' пен 'Тарих
    жалпы' бөлек талдау үшін), тек сол пән атауларының нәтижелері ғана
    есепке алынады. curators_filter берілсе (мұғалім бойынша бөлек есеп
    үшін), тек сол нақты куратор атауларының (results.curator баганындағы
    дәл жазылуымен) нәтижелері ғана есепке алынады."""
    week = conn.execute("SELECT * FROM weeks WHERE id = ?", (week_id,)).fetchone()
    if week is None:
        return None

    week_ids = combine_week_ids if combine_week_ids else [week_id]
    placeholders = ",".join("?" * len(week_ids))
    query = f"SELECT * FROM results WHERE week_id IN ({placeholders})"
    params = list(week_ids)
    if subjects_filter is not None:
        subj_placeholders = ",".join("?" * len(subjects_filter))
        query += f" AND subject IN ({subj_placeholders})"
        params.extend(subjects_filter)
    if curators_filter is not None:
        cur_placeholders = ",".join("?" * len(curators_filter))
        query += f" AND curator IN ({cur_placeholders})"
        params.extend(curators_filter)
    query += " ORDER BY subject, topic, student"
    results = conn.execute(query, params).fetchall()

    ref_max_score = _reference_max_score(results)

    passing_score = week["passing_score"]
    if passing_score is None and week["stream_id"] is not None:
        stream_row = conn.execute(
            "SELECT category FROM streams WHERE id = ?", (week["stream_id"],)
        ).fetchone()
        if stream_row is not None:
            passing_score = CATEGORY_PASSING_SCORE_DEFAULTS.get(stream_row["category"])

    passing_percent = _raw_threshold_to_percent(passing_score, ref_max_score, DEFAULT_PASSING_PERCENT)
    gold_percent = _raw_threshold_to_percent(week["gold_threshold"], ref_max_score, DEFAULT_GOLD_PERCENT)
    silver_percent = _raw_threshold_to_percent(week["silver_threshold"], ref_max_score, DEFAULT_SILVER_PERCENT)

    report = {
        "week": week,
        "passing_score": passing_score,
        "passing_percent": passing_percent,
        "gold_percent": gold_percent,
        "silver_percent": silver_percent,
        "ref_max_score": ref_max_score,
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


def compute_curator_extremes(conn, week_ids):
    """Осы апта(лар)дың өз нәтижелерін куратор бойынша топтап, ең жоғарғы
    және ең төменгі ортақ балл жинаған кураторды қайтарады. Шақырушы тарап
    мұны тек 2-айдан бастап көрсетуі керек — 1-айда куратор аты-жөндері
    рейтинг кестелерінде бірізді жазылмаған болатын."""
    if not week_ids:
        return None, None
    placeholders = ",".join("?" * len(week_ids))
    rows = conn.execute(
        f"SELECT curator, score FROM results WHERE week_id IN ({placeholders}) "
        "AND score IS NOT NULL AND curator IS NOT NULL AND curator != ''",
        week_ids,
    ).fetchall()

    curator_scores = {}
    for r in rows:
        curator_scores.setdefault(r["curator"].strip(), []).append(float(r["score"]))
    curator_avgs = [
        {"curator": name, "avg_score": round(sum(vals) / len(vals), 2)}
        for name, vals in curator_scores.items()
        if vals
    ]
    if not curator_avgs:
        return None, None
    best = max(curator_avgs, key=lambda c: c["avg_score"])
    worst = min(curator_avgs, key=lambda c: c["avg_score"])
    return best, worst


def compare_reports(report, prev_report):
    """Осы апта есебін (report) алдыңғы аптаның есебімен (prev_report)
    салыстырып, әр көрсеткіш бойынша прогресс/регресс дельтасын қайтарады.
    higher_is_better=False көрсеткіштерде (мыс. 0 балл жинаған оқушы саны)
    төмендеу — прогресс болып есептеледі."""

    def _cmp(cur, prev, higher_is_better=True):
        if cur is None or prev is None:
            return None
        delta = round(cur - prev, 2)
        if delta == 0:
            state = "same"
        elif (delta > 0) == higher_is_better:
            state = "up"
        else:
            state = "down"
        return {"delta": delta, "state": state}

    comparison = {
        "prev_title": prev_report["week"]["title"],
        "avg_score": _cmp(report["overall_avg_score"], prev_report["overall_avg_score"]),
        "zero_students": _cmp(report["zero_students"], prev_report["zero_students"], higher_is_better=False),
        "max_achiever_students": _cmp(report["max_achiever_students"], prev_report["max_achiever_students"]),
        "gold_share": _cmp(report["gold_share"], prev_report["gold_share"]),
        "silver_share": _cmp(report["silver_share"], prev_report["silver_share"]),
        "bronze_share": _cmp(report["bronze_share"], prev_report["bronze_share"]),
    }
    if report.get("has_targets") and prev_report.get("has_targets"):
        comparison["target_achievement"] = _cmp(
            report["target_achievement_percent"], prev_report["target_achievement_percent"]
        )
    return comparison


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

# Қазақ пернетақтасы жоқ адамдар қазақ әрпінің орнына соған ең жақын орыс
# әрпін теріп жібереді (мыс. 'Гүлжанат' орнына 'Гулжанат') — рейтинг
# кестесінде осындай алмастырулар жиі кездеседі. Сонымен қатар латын
# әрпі кирилл әрпіне сырты ұқсас болғандықтан (мыс. латын 'C' — кирилл
# 'С'), пернетақта тілі кездейсоқ ауысып қалғанда осылай араласып кетеді
# — көзге бірдей көрінгенімен, компьютер үшін бөлек таңба. Салыстыру
# кезінде екеуін бірдей әріп деп санау үшін осы кестемен қалыпқа
# келтіреміз.
_LOOKALIKE_TRANSLATION = str.maketrans({
    "Ә": "А", "Ғ": "Г", "Қ": "К", "Ң": "Н",
    "Ө": "О", "Ұ": "У", "Ү": "У", "І": "И", "Һ": "Х",
    # латын -> кирилл (сырты ұқсас әріптер)
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н",
    "K": "К", "M": "М", "O": "О", "P": "Р", "T": "Т",
    "X": "Х", "Y": "У",
})


def _compact_name(name):
    """Атты бас әріпке келтіріп, әріп еместердің бәрін (бос орын, нүкте,
    сан, сызықша) алып тастайды — мыс. 'Ернар Қ2' -> 'ЕРНАРК'. Осылай
    рейтинг парағының атауы топ нөмірімен/қосымша белгімен жазылса да
    (мыс. 'Нұрғазы2', 'НҰРҒАЗЫ-топ') салыстыруға дайын пішінге келеді.
    Қазақ әрпі мен оған ұқсас орыс әрпі (Ү/У, Қ/К, т.б.) де бірдей
    әріп ретінде қаралады — теру кезіндегі жиі кездесетін алмастыру."""
    upper = (name or "").upper().translate(_LOOKALIKE_TRANSLATION)
    return "".join(ch for ch in upper if ch.isalpha())


def _registered_name_candidates(name):
    """Мұғалім тіркеген 'Тегі Аты' пішініндегі атынан (мыс. 'Қуаныш Ернар')
    рейтинг парағынан ІЗДЕУ басымдығы бойынша кілт ТОПТАРЫН қайтарады —
    әр топтағы кілттердің кез келгені сәйкес келсе, сол топ 'тапты' деп
    есептеледі:
    1-топ (қатаң, бас әрпімен): рейтинг парағында куратор аты екі түрлі
       ретпен жазылуы мүмкін — 'Аты + Тегінің бас әрпі' (мыс. 'Ернар Қ')
       немесе 'Тегінің бас әрпі.Аты' (мыс. 'І.Нұрайым') — екеуі де осы
       топта бірдей басымдықпен тексеріледі. Тіркеу кезінде екі сөз қай
       ретпен жазылғаны (Тегі Аты ма, әлде Аты Тегі ме) әрдайым анық
       болмайтындықтан, ЕКІ сөзді де кезек-кезек 'аты' деп қабылдап,
       екеуінің комбинациясын да осы топқа қосамыз.
    2-топ (бос, соңғы амал): тек 'Аты' (екі сөздің де) — 1-топ ешбір
       нақты атаумен сәйкес келмесе ғана қолданылады.
    Бір сөзден тұратын атаулар үшін тек сол бір кілттен тұратын жалғыз
    топ қайтарылады."""
    parts = [p for p in _NAME_SPLIT_RE.split((name or "").strip()) if p]
    if len(parts) >= 2:
        a, b = _compact_name(parts[0]), _compact_name(parts[1])
        strict_group = []
        fallback_group = []
        for firstname_c, initial_c in ((b, a[:1]), (a, b[:1])):
            forward = firstname_c + initial_c  # "Ернар Қ" пішіні
            backward = initial_c + firstname_c  # "І.Нұрайым" пішіні
            for key in (forward, backward):
                if key and key not in strict_group:
                    strict_group.append(key)
            if firstname_c and firstname_c not in fallback_group:
                fallback_group.append(firstname_c)
        groups = [strict_group]
        fallback_group = [k for k in fallback_group if k not in strict_group]
        if fallback_group:
            groups.append(fallback_group)
        return groups
    if parts:
        return [[_compact_name(parts[0])]]
    return []


_PREFIX_MIN_LEN = 4
# Бас әрпі жоқ ('тобы'-мен ғана жазылған) кезеңде минимум ұзындықты сәл
# босаңсытамыз — бас әрпі жоқтықтан сигнал әлсіз болғанымен, іс жүзінде
# (нақты деректе) бұл 3 әріптен басталатын атаулар да (мыс. 'Аяу тобы' ->
# 'Аяулым', 'Нұр тобы' -> 'Нұрасыл') әрдайым жалғыз тіркелген атпен ғана
# сәйкес келеді екен — екіұшты болса, төмендегі тексеріс бәрібір өткізіп
# жібереді.
_BARE_PREFIX_MIN_LEN = 3
_GROUP_SUFFIX_COMPACT = "ТОБЫ"


def _split_name_initial(name):
    """Екі бөлектен тұратын атты (аты, бас әрпі) етіп бөледі — бөлектердің
    ТЕК біреуі бір әріптен тұрса ғана (мыс. 'Айым М.' -> ('АЙЫМ','М'),
    'І.Нұрайым' -> ('НҰРАЙЫМ','І')). Соңында 'тобы' сөзі тұрса (мыс.
    'Т.Сандуғаш тобы'), оны есептен шығарып тастаймыз. Сәйкес келмесе
    (екеуі де ұзын не екеуі де бір әріп болса, немесе бөлек саны 2
    болмаса) None қайтарады."""
    parts = [p for p in _NAME_SPLIT_RE.split((name or "").strip()) if p]
    if parts and _compact_name(parts[-1]) == _GROUP_SUFFIX_COMPACT:
        parts = parts[:-1]
    if len(parts) != 2:
        return None
    a, b = parts
    if len(a) == 1 and len(b) != 1:
        initial, firstname = a, b
    elif len(b) == 1 and len(a) != 1:
        firstname, initial = a, b
    else:
        return None
    return _compact_name(firstname), _compact_name(initial)


def _registered_firstname_and_initial(name):
    """Мұғалім тіркеген екі сөзді атынан (аты, бас әрпі) жұптарын қайтарады
    — қай сөз тегі, қай сөз аты екені әрдайым анық бола бермейтіндіктен,
    ЕКІ бағыт бойынша да ((2-сөз, 1-сөздің бас әрпі) және (1-сөз, 2-сөздің
    бас әрпі)) қайтарады."""
    parts = [p for p in _NAME_SPLIT_RE.split((name or "").strip()) if p]
    if len(parts) < 2:
        return []
    a, b = _compact_name(parts[0]), _compact_name(parts[1])
    pairs = [(b, a[:1]), (a, b[:1])]
    seen = []
    for pair in pairs:
        if pair not in seen:
            seen.append(pair)
    return seen


def _actual_bare_compact(name):
    """Нақты куратор атынан соңындағы 'тобы' сөзін алып тастап, қалған
    ЖАЛҒЫЗ сөзді қайтарады (бас әрпінсіз, мыс. 'Аяу тобы' -> 'АЯУ').
    Тобы алынған соң бір сөзден артық/кем қалса, None қайтарады —
    бұл кезең тек нағыз бас әрпінсіз, жалғыз сөзді атауларға арналған."""
    parts = [p for p in _NAME_SPLIT_RE.split((name or "").strip()) if p]
    if parts and _compact_name(parts[-1]) == _GROUP_SUFFIX_COMPACT:
        parts = parts[:-1]
    if len(parts) != 1:
        return None
    return _compact_name(parts[0])


def _registered_bare_names(name):
    """Мұғалім тіркеген екі сөзді атынан (бас әріпсіз) екі сөзді де бөлек
    қайтарады — рейтинг парағында куратор аты бас әрпінсіз, тіпті
    қысқартылып та жазылуы мүмкін (мыс. 'Аяулым' -> 'Аяу тобы')."""
    parts = [p for p in _NAME_SPLIT_RE.split((name or "").strip()) if p]
    names = [_compact_name(p) for p in parts if p]
    seen = []
    for n in names:
        if n and n not in seen:
            seen.append(n)
    return seen


def _match_teacher_curators(teacher_curator_names_by_id, actual_curator_names):
    """Әр мұғалімнің тіркелген куратор аттарын (teacher_id -> [cname, ...])
    рейтинг парағындағы НАҚТЫ куратор аттарымен (actual_curator_names)
    сәйкестендіреді. Кезең-кезеңмен жүреді: алдымен БАРЛЫҚ мұғалім үшін
    тек қатаң (тегінің бас әрпі бар) кілт тобын тексереді, содан кейін
    ғана әлі сәйкессіз қалғандарға бос ('тек Аты') кілтті қолданады —
    осылай бір нақты куратор аты кездейсоқ екі басқа мұғалімге қатар
    'ұрланып' кетпейді (қатаң сәйкестік әрқашан бос сәйкестіктен басым
    болады). Егер бос ('тек Аты') кілт осы жиынтықтағы БІРНЕШЕ түрлі
    тіркелген куратор атына бірдей сәйкес келсе (мыс. 'Советхан Мерей'
    мен 'Бактығали Мерей' екеуі де 'МЕРЕЙ' дейді), ол кілт екіұшты
    болғандықтан МҮЛДЕМ қолданылмайды — қате мұғалімге теліп қоюдан гөрі,
    сол куратордың нәтижесі есептен тыс қалғаны дұрыс. Осы екі кезеңнен
    кейін де сәйкессіз қалғандарға соңғы амал ретінде ҚЫСҚАРТЫЛҒАН аты
    тексеріледі — рейтинг парағында аты толық жазылмай қысқартылып
    кетуі мүмкін (мыс. 'Айымжан' орнына 'Айым М.'): тегінің бас әрпі
    ДӘЛ сәйкес келіп, аты бір-бірінің префиксі болса (кемінде 4 әріп)
    сәйкес деп есептеледі — бірақ ол да бірнеше тіркелген атпен бірдей
    сәйкес келсе, екіұшты болғандықтан өткізіп жіберіледі. Қайтарады:
    {teacher_id: {registered_cname: actual_curator_name}}."""
    actual_compact = {name: _compact_name(name) for name in actual_curator_names}
    entries = []
    fallback_owners = {}
    for teacher_id, cnames in teacher_curator_names_by_id.items():
        for cname in cnames:
            groups = _registered_name_candidates(cname)
            entries.append((teacher_id, cname, groups))
            if len(groups) > 1:
                for key in groups[1]:
                    fallback_owners.setdefault(key, set()).add((teacher_id, cname))

    claimed_by_actual = {}
    matches_by_teacher = {teacher_id: {} for teacher_id in teacher_curator_names_by_id}

    max_rounds = max((len(groups) for _, _, groups in entries), default=0)
    for round_idx in range(max_rounds):
        for teacher_id, cname, groups in entries:
            if cname in matches_by_teacher[teacher_id]:
                continue
            if round_idx >= len(groups):
                continue
            group = groups[round_idx]
            if round_idx >= 1:
                # Бос (fallback) деңгей: осы жиынтықта екіұшты кілттерді алып тастаймыз.
                group = [c for c in group if len(fallback_owners.get(c, ())) <= 1]
            group = [c for c in group if c]
            if not group:
                continue
            for actual_name in actual_curator_names:
                if actual_name in claimed_by_actual:
                    continue
                if any(actual_compact[actual_name].startswith(c) for c in group):
                    claimed_by_actual[actual_name] = teacher_id
                    matches_by_teacher[teacher_id][cname] = actual_name
                    break

    # Соңғы амал: қысқартылған аты (мыс. 'Айымжан' -> 'Айым М.') — тегінің
    # бас әрпі дәл сәйкес, аты бір-бірінің префиксі (кемінде 4 әріп). Тегі
    # мен аты қай ретпен тіркелгені әрдайым анық бола бермейтіндіктен, екі
    # бағытты да ((аты,тегінің бас әрпі) және керісінше) тексереміз.
    prefix_candidates = {}
    for teacher_id, cname, _groups in entries:
        if cname in matches_by_teacher[teacher_id]:
            continue
        for reg_firstname_c, reg_initial_c in _registered_firstname_and_initial(cname):
            if len(reg_firstname_c) < _PREFIX_MIN_LEN:
                continue
            for actual_name in actual_curator_names:
                if actual_name in claimed_by_actual:
                    continue
                actual_parts = _split_name_initial(actual_name)
                if not actual_parts:
                    continue
                actual_firstname_c, actual_initial_c = actual_parts
                if actual_initial_c != reg_initial_c or len(actual_firstname_c) < _PREFIX_MIN_LEN:
                    continue
                if reg_firstname_c.startswith(actual_firstname_c) or actual_firstname_c.startswith(reg_firstname_c):
                    prefix_candidates.setdefault(actual_name, set()).add((teacher_id, cname))

    for actual_name, owners in prefix_candidates.items():
        if actual_name in claimed_by_actual or len(owners) != 1:
            continue
        (teacher_id, cname), = owners
        claimed_by_actual[actual_name] = teacher_id
        matches_by_teacher[teacher_id][cname] = actual_name

    # Ең соңғы амал: бас әрпі де жоқ, тек қысқартылған жалғыз сөз (мыс.
    # 'Аяулым' -> 'Аяу тобы', бас әрпінсіз) — өте бос кілт болғандықтан
    # тек бір ғана тіркелген атпен сәйкес келсе ғана қолданылады.
    bare_candidates = {}
    for teacher_id, cname, _groups in entries:
        if cname in matches_by_teacher[teacher_id]:
            continue
        for reg_bare_c in _registered_bare_names(cname):
            if len(reg_bare_c) < _BARE_PREFIX_MIN_LEN:
                continue
            for actual_name in actual_curator_names:
                if actual_name in claimed_by_actual:
                    continue
                actual_bare = _actual_bare_compact(actual_name)
                if actual_bare is None or len(actual_bare) < _BARE_PREFIX_MIN_LEN:
                    continue
                if reg_bare_c.startswith(actual_bare) or actual_bare.startswith(reg_bare_c):
                    bare_candidates.setdefault(actual_name, set()).add((teacher_id, cname))

    for actual_name, owners in bare_candidates.items():
        if actual_name in claimed_by_actual or len(owners) != 1:
            continue
        (teacher_id, cname), = owners
        claimed_by_actual[actual_name] = teacher_id
        matches_by_teacher[teacher_id][cname] = actual_name

    return matches_by_teacher


WEAK_STUDENT_MAX_SCORE = 3
STRONG_STUDENT_MIN_SCORE = 13
# Junior бағдарламасының максимум баллы 15 емес, 10 (PROGRAM_MAX_SCORE) —
# сондықтан 'мықты оқушы' шегі де бөлек, төмендеу (7-10 аралығы).
STRONG_STUDENT_MIN_SCORE_BY_PROGRAM = {"junior": 7}


def compute_teacher_stats_for_week(conn, week_id, combine_week_ids=None):
    """Осы аптаның (немесе, combine_week_ids берілсе, бірнеше аптаның
    біріктірілген) нәтижелері бойынша, осы аптаның ағынына (stream)
    тіркелген БАРЛЫҚ мұғалімді қайтарады — кестеде әлі нәтиже жоқ мұғалім
    де тізімнен түспейді, тек avg_score-ы None болады ('нәтиже жоқ' деп
    көрсету үшін). Ортақ балл — мұғалімнің өз кураторларының
    (teacher_curators-та тіркелген) ортақ балдарының орташасы. Куратор аты
    рейтинг кестесінде қысқа (мыс. бір есім) жазылатындықтан, мұғалімге
    тіркелген толық аты-жөнмен ДӘЛ сәйкес келуін емес, ортақ сөз (аты/тегі)
    бар-жоғын тексереміз. Сонымен қатар әр мұғалімнің өз кураторларының
    оқушылары арасынан нашар (орташа балл ≤3) және мықты (орташа балл ≥13)
    оқушыларды, олардың кураторымен қоса, тізімдейді (weak_students /
    strong_students), сол мұғалімнің өз кураторлары бойынша ғана есептелген
    толық compute_report нәтижесін ('report') және сол топтың ішіндегі ең
    жоғарғы/ең төменгі ортақ балл куратор ('best_curator'/'worst_curator')
    қоса қайтарады."""
    week = conn.execute("SELECT stream_id FROM weeks WHERE id = ?", (week_id,)).fetchone()
    if week is None or week["stream_id"] is None:
        return []

    program_row = conn.execute(
        "SELECT p.slug FROM streams s JOIN programs p ON p.id = s.program_id WHERE s.id = ?",
        (week["stream_id"],),
    ).fetchone()
    program_slug = program_row["slug"] if program_row else None
    strong_min_score = STRONG_STUDENT_MIN_SCORE_BY_PROGRAM.get(program_slug, STRONG_STUDENT_MIN_SCORE)
    program_max_score = PROGRAM_MAX_SCORE.get(program_slug, 15)

    week_ids = combine_week_ids if combine_week_ids else [week_id]
    placeholders = ",".join("?" * len(week_ids))
    rows = conn.execute(
        f"SELECT curator, student, score FROM results WHERE week_id IN ({placeholders}) "
        "AND score IS NOT NULL AND curator IS NOT NULL AND curator != ''",
        week_ids,
    ).fetchall()

    curator_scores = {}
    curator_student_rows = {}
    for r in rows:
        cname = r["curator"].strip()
        score = float(r["score"])
        curator_scores.setdefault(cname, []).append(score)
        curator_student_rows.setdefault(cname, []).append((r["student"], score))
    curator_avg = {name: sum(vals) / len(vals) for name, vals in curator_scores.items() if vals}

    teacher_rows = conn.execute(
        "SELECT id, name FROM teachers WHERE stream_id = ? ORDER BY name", (week["stream_id"],)
    ).fetchall()
    curator_names_by_teacher = {}
    for r in conn.execute(
        "SELECT tc.teacher_id, tc.curator_name FROM teacher_curators tc "
        "JOIN teachers t ON t.id = tc.teacher_id WHERE t.stream_id = ?",
        (week["stream_id"],),
    ).fetchall():
        curator_names_by_teacher.setdefault(r["teacher_id"], []).append(r["curator_name"])
    # Барлық мұғалім үшін бос жазба (curator тізімі жоқ) болса да, глобал
    # сәйкестендіру функциясы дұрыс жұмыс істеуі үшін кілт ретінде болу керек.
    for t in teacher_rows:
        curator_names_by_teacher.setdefault(t["id"], [])

    matches_by_teacher = _match_teacher_curators(curator_names_by_teacher, list(curator_avg.keys()))

    teachers = []
    for t in teacher_rows:
        curator_names = curator_names_by_teacher.get(t["id"], [])
        teacher_matches = matches_by_teacher.get(t["id"], {})
        used_curators = set(teacher_matches.values())
        matched_scores = [curator_avg[actual_name] for actual_name in used_curators]

        student_scores = {}
        student_curator = {}
        for actual_name in used_curators:
            for student_raw, score in curator_student_rows.get(actual_name, []):
                student = (student_raw or "").strip()
                if not student:
                    continue
                student_scores.setdefault(student, []).append(score)
                student_curator.setdefault(student, actual_name)

        weak_students = []
        strong_students = []
        for student, scores in student_scores.items():
            avg = sum(scores) / len(scores)
            entry = {"student": student, "curator": student_curator[student], "avg_score": round(avg, 2)}
            if avg <= WEAK_STUDENT_MAX_SCORE:
                weak_students.append(entry)
            elif avg >= strong_min_score:
                strong_students.append(entry)
        weak_students.sort(key=lambda e: e["avg_score"])
        strong_students.sort(key=lambda e: e["avg_score"], reverse=True)

        matched_curator_names = list(used_curators)
        teacher_report = (
            compute_report(conn, week_id, combine_week_ids=combine_week_ids, curators_filter=matched_curator_names)
            if matched_curator_names
            else None
        )
        teacher_best_curator = teacher_worst_curator = None
        if matched_curator_names:
            curator_avgs_here = [
                {"curator": name, "avg_score": round(curator_avg[name], 2)}
                for name in matched_curator_names
                if name in curator_avg
            ]
            if curator_avgs_here:
                teacher_best_curator = max(curator_avgs_here, key=lambda c: c["avg_score"])
                teacher_worst_curator = min(curator_avgs_here, key=lambda c: c["avg_score"])

        teachers.append(
            {
                "id": t["id"],
                "name": t["name"],
                "avg_score": round(sum(matched_scores) / len(matched_scores), 2) if matched_scores else None,
                "curator_count": len(matched_scores),
                "total_curators": len(curator_names),
                "weak_students": weak_students,
                "strong_students": strong_students,
                "weak_max_score": WEAK_STUDENT_MAX_SCORE,
                "strong_min_score": strong_min_score,
                "program_max_score": program_max_score,
                "report": teacher_report,
                "best_curator": teacher_best_curator,
                "worst_curator": teacher_worst_curator,
            }
        )
    teachers.sort(key=lambda t: (t["avg_score"] is None, -(t["avg_score"] or 0), t["name"]))
    return teachers


# 'ШІЛДЕ' (ТАРИХ-01) ағынының өткен жылғы баламасы болмағандықтан (өткен
# жылы бұл ай ашылмаған), оның орнына 'ТАМЫЗ' (ТАРИХ-11) деректерімен
# салыстырылады.
PRIOR_YEAR_STREAM_FALLBACK = {"ТАРИХ-01": "ТАРИХ-11"}


def get_prior_year_comparison(conn, category_slug, stream_code, month_number):
    """Ағымдағы санат/поток/ай үшін өткен жылғы орташа баллды қайтарады.
    СТ санаты (sabaq_tapsyru) үшін бір мән, ал ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санаты
    (baiqau_test) үшін ДТ мен БТ бөлек қайтарылады — ағымдағы жүйеде бұл
    екеуі бір санатқа біріктірілген, бірақ өткен жылғы отчетте ДТ мен БТ
    бөлек парақ болған, сондықтан қайсысы екенін біле алмаймыз. Осы поток/ай
    үшін жол тіркелген, бірақ баллы жоқ болса (отчетте '-'), avg_score None
    болып қайтарылады — шақырушы тарап оны '-' деп көрсетуі керек, тайлды
    мүлде жасырмай. Ешбір жол тіркелмеген болса ғана None қайтарады."""
    lookup_code = PRIOR_YEAR_STREAM_FALLBACK.get(stream_code, stream_code)

    def _fetch(category):
        row = conn.execute(
            "SELECT academic_year, avg_score FROM prior_year_stats "
            "WHERE category = ? AND stream_code = ? AND month_number = ? "
            "ORDER BY academic_year DESC LIMIT 1",
            (category, lookup_code, month_number),
        ).fetchone()
        return (row["academic_year"], row["avg_score"]) if row else (None, None)

    if category_slug == "sabaq_tapsyru":
        year, avg = _fetch("sabaq_tapsyru")
        if year is None:
            return None
        return {"sabaq_tapsyru": {"academic_year": year, "avg_score": avg}}

    if category_slug == "baiqau_test":
        dt_year, dt_avg = _fetch("dt")
        bt_year, bt_avg = _fetch("bt")
        if dt_year is None and bt_year is None:
            return None
        result = {}
        if dt_year is not None:
            result["dt"] = {"academic_year": dt_year, "avg_score": dt_avg}
        if bt_year is not None:
            result["bt"] = {"academic_year": bt_year, "avg_score": bt_avg}
        return result

    return None


_LS_WEEK_SORT_RE = re.compile(r"(\d+)-АЙ (\d+)-АПТА")
_LS_TEACHER_MIN_MATCH_LEN = 5


def _ls_week_sort_key(label):
    m = _LS_WEEK_SORT_RE.match(label or "")
    return (int(m.group(1)), int(m.group(2))) if m else (999, 999)


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _ls_delta(current, prior):
    """app.py-дегі _delta-мен бірдей пішінде (delta, state) қайтарады —
    LS аптасының алдыңғы аптамен салыстырғандағы прогресс/регресін
    белгілеу үшін."""
    if current is None or prior is None:
        return None
    delta = round(current - prior, 1)
    if delta == 0:
        state = "same"
    elif delta > 0:
        state = "up"
    else:
        state = "down"
    return {"delta": delta, "state": state}


def _format_ls_session(row):
    """ls_sessions жолын шаблонға дайын сөздікке айналдырады — күнін
    'ДД.ММ.ЖЖЖЖ' пішінінде көрсету үшін бөлек өріс қосады (сұрыптау
    әлі толық ISO мәні бойынша, уақыт бөлігін қоса, жасалғандықтан бір
    күнде бірнеше session болса да реті дұрыс сақталады). combined_percent —
    ұнату мен қатысымның ортақ (екеуінің орташасы) пайызы, домалақ
    белгіде көрсету үшін — екеуі де бар болса орташасы, тек бірі болса
    сол, дәлдігі бұзылмайды (бүтін санға дейін дөңгеленбейді)."""
    date_display = None
    raw = row["session_date"]
    if raw:
        try:
            date_display = datetime.fromisoformat(raw).strftime("%d.%m.%Y")
        except ValueError:
            date_display = raw

    like = row["like_percent"]
    attendance = row["attendance_percent"]
    if like is not None and attendance is not None:
        combined = round((like + attendance) / 2, 1)
    else:
        combined = like if like is not None else attendance

    return {
        "like_percent": like,
        "attendance_percent": attendance,
        "combined_percent": combined,
        "date_display": date_display,
    }


def _compact_name_tokens(name):
    return [_compact_name(p) for p in re.split(r"\s+", (name or "").strip()) if p]


def _ls_names_fuzzy_equal(name_a, name_b):
    """Екі атты сөз-сөзбен салыстырады — сөз саны бірдей болып, әр
    жұптағы сөз я дәл бірдей, я бірі-бірінің префиксі болса (кемінде
    _LS_TEACHER_MIN_MATCH_LEN әріп), сәйкес деп есептеледі. Мыс.
    'Өмірзақ Саян' vs 'Өмірзақов Саян' — 'Өмірзақ'/'Өмірзақов' сөзінің
    ортасында емес, соңында ғана айырмашылық болғанда осылай ұсталады
    (толық аттың біріктірілген түрін салыстырғанда мұны байқау мүмкін
    емес еді, себебі айырмашылық сөздер аралығында қалып қалады)."""
    tokens_a = _compact_name_tokens(name_a)
    tokens_b = _compact_name_tokens(name_b)
    if not tokens_a or len(tokens_a) != len(tokens_b):
        return False
    for a, b in zip(tokens_a, tokens_b):
        if a == b:
            continue
        if len(a) >= _LS_TEACHER_MIN_MATCH_LEN and len(b) >= _LS_TEACHER_MIN_MATCH_LEN and (a.startswith(b) or b.startswith(a)):
            continue
        return False
    return True


def _merge_ls_teacher_names(raw_names):
    """Бір ағымдағы нақты (экзельдегі) 'мұғалім' атауларының жиынын
    топтастырады — сырты ғана бөлек жазылған (мыс. 'Өмірзақ Саян' vs
    'Өмірзақов Саян') аттар бір адам ретінде бір қанондық атқа
    бірігеді (соның ішіндегі ең ұзын жазылуы қанондық ат болады).
    Тіркелген мұғалім тізімі ЕМЕС, дәл экзельде жазылған кураторлар
    негізінде — ауыстырылған (замена) мұғалім болса да, оның нәтижесі
    осылай өз атымен көрінеді. Қайтарады: {raw_name: canonical_name}."""
    names = sorted({(n or "").strip() for n in raw_names if n and n.strip()}, key=len, reverse=True)
    canon_list = []
    mapping = {}
    for name in names:
        match = next((c for c in canon_list if _ls_names_fuzzy_equal(name, c)), None)
        if match is None:
            canon_list.append(name)
            mapping[name] = name
        else:
            mapping[name] = match
    return mapping


def compute_ls_teacher_data(conn):
    """Live сабақ (LS) статистикасын жинақтайды — мұғалім тізімі ТІРКЕЛГЕН
    мұғалімдер емес, дәл экзельдегі 'мұғалім' бағанынан алынады (ауыстырылған/
    замена мұғалімдердің нәтижесі де осылай көрінеді үшін). Әр мұғалім-ағым
    жұбы бойынша жалпы ортаа ұнату/қатысым пайызы + апта бойынша топталған
    (жасырын/ашылатын) session тізімі. Ағым коды бойынша топтастырылған
    сөздік қайтарады: {stream_code: [{"name":.., "avg_like":.., "avg_attendance":..,
    "session_count":.., "weeks": [{"label":.., "avg_like":.., "avg_attendance":..,
    "sessions": [...]}]}, ...]}."""
    # 'күні' бағанында сағат-минут жоқ (тек күн), сондықтан бір күнде
    # бірнеше session болса, олардың нақты реті осы бағанмен анықталмайды —
    # оның орнына id (жол қосылған рет, парақтағы қатардың өз ретімен
    # сәйкес келеді) бойынша сұрыптаймыз.
    session_rows = conn.execute(
        "SELECT id, session_date, teacher_name, stream_code, week_label, like_percent, attendance_percent "
        "FROM ls_sessions ORDER BY id"
    ).fetchall()

    # Екі көрсеткіші де (ұнату/қатысым) бос жол — нақты сабақ емес, әлі
    # бағаланбаған не бос үлгі жол — есептен шығарамыз.
    rows = [r for r in session_rows if not (r["like_percent"] is None and r["attendance_percent"] is None)]

    raw_names_by_stream = {}
    for r in rows:
        raw_names_by_stream.setdefault(r["stream_code"], set()).add((r["teacher_name"] or "").strip())
    name_map_by_stream = {code: _merge_ls_teacher_names(names) for code, names in raw_names_by_stream.items()}

    sessions_by_key = {}
    for row in rows:
        canonical = name_map_by_stream.get(row["stream_code"], {}).get((row["teacher_name"] or "").strip())
        if not canonical:
            continue
        key = (canonical, row["stream_code"])
        sessions_by_key.setdefault(key, []).append(row)

    by_stream = {}
    for (name, code), sessions in sessions_by_key.items():
        weeks_map = {}
        for s in sessions:
            weeks_map.setdefault(s["week_label"] or "Апта белгісіз", []).append(s)
        weeks = []
        for label in sorted(weeks_map.keys(), key=_ls_week_sort_key):
            wsessions = sorted(weeks_map[label], key=lambda s: s["id"])
            month_num, week_num = _ls_week_sort_key(label)

            date_groups_map = {}
            date_order = []
            for s in wsessions:
                formatted = _format_ls_session(s)
                key_date = formatted["date_display"] or "Күні белгісіз"
                if key_date not in date_groups_map:
                    date_groups_map[key_date] = []
                    date_order.append(key_date)
                date_groups_map[key_date].append(formatted)
            date_groups = [
                {"date_display": key_date, "sessions": date_groups_map[key_date]}
                for key_date in date_order
            ]

            avg_like = _avg([s["like_percent"] for s in wsessions])
            avg_attendance = _avg([s["attendance_percent"] for s in wsessions])
            if avg_like is not None and avg_attendance is not None:
                combined_avg = round((avg_like + avg_attendance) / 2, 1)
            else:
                combined_avg = avg_like if avg_like is not None else avg_attendance

            weeks.append({
                "label": label,
                "month": month_num,
                "week": week_num,
                "avg_like": avg_like,
                "avg_attendance": avg_attendance,
                "combined_avg": combined_avg,
                "date_groups": date_groups,
            })

        # Алдыңғы аптамен салыстырғанда прогресс/регресс — ұнату мен
        # қатысым ЕКЕУІ ДЕ БӨЛЕК-БӨЛЕК салыстырылады (біреуін ғана емес),
        # апта реті хронологиялық (label бойынша сұрыпталған) болғандықтан
        # тізімдегі алдыңғы элемент әрдайым шын мәнінде алдыңғы апта.
        for i, w in enumerate(weeks):
            prior_like = weeks[i - 1]["avg_like"] if i > 0 else None
            prior_attendance = weeks[i - 1]["avg_attendance"] if i > 0 else None
            w["like_comparison"] = _ls_delta(w["avg_like"], prior_like)
            w["attendance_comparison"] = _ls_delta(w["avg_attendance"], prior_attendance)

        by_stream.setdefault(code, []).append({
            "name": name,
            "avg_like": _avg([s["like_percent"] for s in sessions]),
            "avg_attendance": _avg([s["attendance_percent"] for s in sessions]),
            "session_count": len(sessions),
            "weeks": weeks,
        })

    for code, teachers in by_stream.items():
        teachers.sort(key=lambda t: t["name"])

    return by_stream


def compute_ls_stream_week_stats(conn):
    """Мұғалімге қарамай, тек ағым (stream_code) және апта бойынша
    жинақталған LS статистикасы — 'Жалпы нәтиже' бетіндегі диаграмма мен
    ай/апта бойынша сүзгі үшін. Апта реті хронологиялық. Қайтарады:
    {stream_code: [{"label":.., "month":.., "week":.., "avg_like":..,
    "avg_attendance":.., "combined_avg":.., "session_count":..}, ...]}."""
    rows = conn.execute(
        "SELECT stream_code, week_label, like_percent, attendance_percent FROM ls_sessions"
    ).fetchall()
    rows = [r for r in rows if not (r["like_percent"] is None and r["attendance_percent"] is None)]

    grouped = {}
    for r in rows:
        key = (r["stream_code"], r["week_label"] or "Апта белгісіз")
        grouped.setdefault(key, []).append(r)

    by_stream = {}
    for (code, label), group_rows in grouped.items():
        month_num, week_num = _ls_week_sort_key(label)
        avg_like = _avg([r["like_percent"] for r in group_rows])
        avg_attendance = _avg([r["attendance_percent"] for r in group_rows])
        if avg_like is not None and avg_attendance is not None:
            combined_avg = round((avg_like + avg_attendance) / 2, 1)
        else:
            combined_avg = avg_like if avg_like is not None else avg_attendance
        by_stream.setdefault(code, []).append({
            "label": label,
            "month": month_num,
            "week": week_num,
            "avg_like": avg_like,
            "avg_attendance": avg_attendance,
            "combined_avg": combined_avg,
            "session_count": len(group_rows),
        })

    for code, weeks in by_stream.items():
        weeks.sort(key=lambda w: _ls_week_sort_key(w["label"]))

    return by_stream

