"""Juz40 (juz40-edu.kz) платформасының headteacher API-мен сөйлесу үшін
клиент. Ешбір функция кілт/пароль мәнін логке/қайтарылатын мәнге
шығармайды — тек JUZ40_USERNAME/JUZ40_PASSWORD орта айнымалыларынан
оқиды (.env немесе Vercel Environment Variables).

Сайттың нақты "поток → Juz40 курсы" сәйкестігі АТАУ БОЙЫНША ДӘЛ (толық,
артық сөзсіз) сәйкестендіріледі — ешқашан жуықтап/болжап таңдамайды.
Сәйкестік табылмаса (курс әлі ашылмаған болуы мүмкін) None қайтарады."""
import json
import os
import re
import time
import urllib.error
import urllib.request

from netfetch import SSL_CONTEXT, USER_AGENT

API_BASE = "https://api.juz40-edu.kz"


class Juz40Error(Exception):
    pass


def _get_credentials():
    username = os.environ.get("JUZ40_USERNAME")
    password = os.environ.get("JUZ40_PASSWORD")
    if not username or not password:
        raise Juz40Error(
            "JUZ40_USERNAME/JUZ40_PASSWORD орнатылмаған "
            "(.env файлына немесе Vercel Environment Variables бөліміне қосыңыз)."
        )
    return username, password


def _call(url, method="GET", body=None, token=None, timeout=30):
    headers = {"content-type": "application/json", "user-agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
            raw = resp.read()
            return json.loads(raw.decode()) if raw else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise Juz40Error(f"Juz40 API қатесі (HTTP {e.code}) {url}: {detail[:300]}") from e
    except urllib.error.URLError as e:
        raise Juz40Error(f"Juz40 API-ге қосыла алмадым: {e.reason}") from e


def login():
    """Juz40-ға кіріп, Bearer токен қайтарады."""
    username, password = _get_credentials()
    body = _call(
        f"{API_BASE}/v1/auth/signin",
        method="POST",
        body={"username": username, "password": password, "fcmToken": ""},
    )
    token = body.get("token")
    if not token:
        raise Juz40Error("Juz40 логин жауабында токен табылмады.")
    return token


def get_all_streams(token, max_pages=40):
    """Осы аккаунтқа көрінетін БАРЛЫҚ ағым/курсты (беттеп) жинап қайтарады."""
    all_streams = []
    page = 0
    while page < max_pages:
        body = _call(
            f"{API_BASE}/v2/headteacher/streams"
            f"?role=HEAD_TEACHER&page={page}&size=100&sort=createdAt,desc"
            "&onlyThisAcademicYearStreams=false",
            token=token,
        )
        content = body.get("content", [])
        all_streams.extend(content)
        if body.get("last") or not content:
            break
        page += 1
        time.sleep(0.15)
    return all_streams


# Sheets.py-дегі PRIOR_YEAR_MONTH_TO_STREAM-мен бірдей ай->нөмір сәйкестігі —
# Juz40-та курс аты осы айдың атауын қамтиды.
_MONTH_TO_STREAM_NUM = {
    "ШІЛДЕ": "01", "ТАМЫЗ": "11", "ҚЫРКҮЙЕК": "21", "ҚАЗАН": "31",
    "ҚАРАША": "41", "ЖЕЛТОҚСАН": "51", "ҚАҢТАР": "61", "АҚПАН": "71",
    "НАУРЫЗ": "81", "СӘУІР": "91", "МАМЫР": "101",
}
_STREAM_NUM_TO_MONTH = {v: k for k, v in _MONTH_TO_STREAM_NUM.items()}
_PROGRAM_GRADE = {"smart": "11", "junior": "10"}
_PROGRAM_LABEL = {"smart": "SMART", "junior": "JUNIOR"}

_COURSE_NAME_RE = re.compile(r"^Қазақстан тарихы (SMART|JUNIOR) (\S+) (11|10) СЫНЫП (\d{4})$")


def find_course_id(all_streams, program_slug, stream_code):
    """stream_code (мыс. 'ТАРИХ-01' немесе 'JUNIOR-21') үшін, Juz40-тағы
    БІРЕГЕЙ, дәл сәйкес келетін курсты іздейді. Дәл бір сәйкестік
    табылмаса (0 немесе 2+) — None қайтарады, ЕШҚАШАН болжамайды."""
    m = re.match(r"^(ТАРИХ|JUNIOR)-(\d+)$", stream_code)
    if not m:
        return None
    num = m.group(2)
    month = _STREAM_NUM_TO_MONTH.get(num)
    if month is None:
        return None
    expected_label = _PROGRAM_LABEL.get(program_slug)
    expected_grade = _PROGRAM_GRADE.get(program_slug)
    if expected_label is None:
        return None

    found = []
    for s in all_streams:
        for c in s.get("courses", []):
            name = (c.get("name") or "").strip()
            cm = _COURSE_NAME_RE.match(name)
            if not cm:
                continue
            program, cmonth, grade, _year = cm.groups()
            if program == expected_label and cmonth == month and grade == expected_grade:
                found.append(c.get("id"))
    if len(found) == 1:
        return found[0]
    return None


def get_groups(token, course_id):
    """Курстың куратор топтарын қайтарады: [{id, name, curator:{firstname,lastname}, ...}, ...]."""
    body = _call(f"{API_BASE}/v1/headteacher/courses/{course_id}/groups", token=token)
    return body or []


def get_group_themes(token, group_id, month, week):
    """Сол топтың (group) осы ай/аптадағы тақырыптар (theme) тізімі."""
    body = _call(
        f"{API_BASE}/v1/headteacher/groups/{group_id}/themes?week={week}&month={month}",
        token=token,
    )
    return (body or {}).get("themes", [])


def find_theme_by_name_part(themes, name_part):
    """themes тізімінен, атауында name_part бар (мыс. 'САБАҚ ТАПСЫРУ')
    жалғыз тақырыпты табады. Дәл бір сәйкестік болмаса None."""
    matches = [t for t in themes if name_part in (t.get("themeName") or "")]
    return matches[0] if len(matches) == 1 else None


def get_theme_lessons(token, theme_id):
    body = _call(f"{API_BASE}/v2/headteacher/themes/{theme_id}/lessons", token=token)
    return body or []


def get_lesson_progresses(token, group_id, lesson_id):
    """Сол сабақ бойынша БАРЛЫҚ оқушының жиынтық үлгерімі (жалпы балл,
    статус, вариант нөмірі)."""
    body = _call(
        f"{API_BASE}/v2/headteacher/groups/{group_id}/lessons/{lesson_id}/progresses",
        token=token,
    )
    return body or []


def get_oral_student_progress(token, group_id, lesson_id, student_id):
    """Бір оқушының жеке сұрақ-сұрақ балы (oralPassingDto.scores) және
    сол вариант PDF материалы."""
    return _call(
        f"{API_BASE}/v1/headteacher/groups/{group_id}/orals/{lesson_id}/students/{student_id}/progresses",
        token=token,
    )


def download_material(url):
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
        return resp.read()
