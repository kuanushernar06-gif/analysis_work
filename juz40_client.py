"""Juz40 (juz40-edu.kz) платформасының headteacher API-мен сөйлесу үшін
клиент. Ешбір функция кілт/пароль мәнін логке/қайтарылатын мәнге
шығармайды — тек JUZ40_USERNAME/JUZ40_PASSWORD орта айнымалыларынан
оқиды (.env немесе Vercel Environment Variables).

Сайттың нақты "поток → Juz40 курсы" сәйкестігі АТАУ БОЙЫНША ДӘЛ (толық,
артық сөзсіз) сәйкестендіріледі — ешқашан жуықтап/болжап таңдамайды.
Сәйкестік табылмаса (курс әлі ашылмаған болуы мүмкін) None қайтарады."""
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
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


_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0


def _call(url, method="GET", body=None, token=None, timeout=15):
    """Көп топты параллель сұраған кезде Juz40 API кейде уақытша (желі/
    жүктеме) қатесі қайтарады — соны 'деректің жоқтығымен' шатастырмас
    үшін, уақытша қателерде бірнеше рет қайталап көреді."""
    headers = {"content-type": "application/json", "user-agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None

    last_error = None
    for attempt in range(_MAX_RETRIES):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                last_error = Juz40Error(f"Juz40 API қатесі (HTTP {e.code}) {url}: {detail[:300]}")
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise Juz40Error(f"Juz40 API қатесі (HTTP {e.code}) {url}: {detail[:300]}") from e
        except urllib.error.URLError as e:
            if attempt < _MAX_RETRIES - 1:
                last_error = Juz40Error(f"Juz40 API-ге қосыла алмадым: {e.reason}")
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise Juz40Error(f"Juz40 API-ге қосыла алмадым: {e.reason}") from e
        except TimeoutError as e:
            if attempt < _MAX_RETRIES - 1:
                last_error = Juz40Error(f"Juz40 API сұранысының уақыты бітті: {url}")
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise Juz40Error(f"Juz40 API сұранысының уақыты бітті: {url}") from e
    raise last_error or Juz40Error(f"Juz40 API-ге сұраныс сәтсіз аяқталды: {url}")


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
    """Осы аккаунтқа көрінетін БАРЛЫҚ ағым/курсты (беттеп) жинап қайтарады.

    ЕСКЕРТУ: бұл платформадағы БАРЛЫҚ (мыңдаған) курсты бір-бірлеп парақтап
    оқиды — өте баяу (ондаған секунд/бетте). Курс іздеу үшін
    find_course_id() енді бұны ШАҚЫРМАЙДЫ, оның орнына жылдам, пән
    бойынша сүзілген _search_courses()-ты пайдаланады. Бұл функция тек
    сирек диагностика/ескі жол ретінде қалдырылған."""
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


# "Қазақстан тарихы" пәнінің Juz40-тағы ID-і (пайдаланушының методист
# дашбордының URL-інен расталған: /methodist/main?subject=...).
JUZ40_SUBJECT_ID = "2f9a8bf5-4a39-4c5f-aa32-4c7ae09521b2"


def _search_courses(token, search_word, size=50):
    """Пән бойынша сүзілген, сервер жағында атау бойынша іздейтін ЖЫЛДАМ
    курс іздеу (толық каталогты парақтаудан әлдеқайда тез)."""
    q = urllib.parse.quote(search_word)
    body = _call(
        f"{API_BASE}/v2/headteacher/subjects/{JUZ40_SUBJECT_ID}/courses"
        f"?size={size}&page=0&searchWord={q}&sort=year,DESC&sort=month,DESC",
        token=token,
    )
    return (body or {}).get("content", [])


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


def find_course_id(token, program_slug, stream_code):
    """stream_code (мыс. 'ТАРИХ-01' немесе 'JUNIOR-21') үшін, Juz40-тағы
    БІРЕГЕЙ, дәл сәйкес келетін курсты іздейді. Дәл бір сәйкестік
    табылмаса (0 немесе 2+) — None қайтарады, ЕШҚАШАН болжамайды.

    Толық каталогты парақтаудың орнына, пән бойынша сүзілген жылдам
    іздеуді (_search_courses) пайдаланады — нәтижесінде табылған
    үміткерлер бәрібір АТАУ БОЙЫНША ДӘЛ сүзгіден (_COURSE_NAME_RE)
    өтеді, сондықтан қауіпсіздігі (ешқашан жуықтап таңдамау) сақталады."""
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

    search_word = f"Қазақстан тарихы {expected_label} {month} {expected_grade} СЫНЫП"
    candidates = _search_courses(token, search_word)

    found = []
    for c in candidates:
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
    # URL-дегі кириллица/бос орын секілді таңбалар urllib-тің шикі
    # (ASCII) HTTP сұранысына сыймайды — percent-encode қажет. safe=":/?&=%"
    # URL-дің құрылымдық бөліктерін (протокол, жол бөлгіштер) бүлдірмейді.
    safe_url = urllib.parse.quote(url, safe=":/?&=%")
    req = urllib.request.Request(safe_url, headers={"user-agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CONTEXT) as resp:
        return resp.read()


_PDF_HEADER_RE = re.compile(r"^[^\n]*\|\s*Сабақ тапсыру\s*\n?", re.MULTILINE)
_PDF_STANDALONE_NUM_RE = re.compile(r"^\s*\d{1,2}\s*$", re.MULTILINE)


def _clean_pdf_question_text(raw_text):
    """PDF беті: тақырыпша ('... | Сабақ тапсыру') және сұрақ нөмірінің
    үлкен графикасы (беттің ортасында, сөйлемді бөліп тұратын жеке '14'
    секілді жол) кездеседі — соларды алып тастап, оқылатын мәтін қалдырады."""
    text = _PDF_HEADER_RE.sub("", raw_text)
    lines = [ln for ln in text.split("\n") if not _PDF_STANDALONE_NUM_RE.match(ln)]
    cleaned = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def extract_pdf_questions(pdf_bytes):
    """СТ нұсқасының (variant) PDF-індегі әр сұрақ бетінің мәтінін тізім
    етіп қайтарады. 1-бет — мұқаба (нұсқа №, пән, дәйексөз), одан кейінгі
    әр бет — бір сұрақ (oralPassingDto.scores-пен индекс бойынша сәйкес
    келеді: questions[0] <-> scores[0], т.с.с.).

    pdfplumber осы функция ІШІНДЕ ғана импортталады — модуль деңгейінде
    импорттасақ, осы кітапхана орнатылмаса/бұзылса БҮКІЛ сайт (логин
    беті де қоса) құлап қалады, тек осы (сирек шақырылатын) мүмкіндік
    емес."""
    import pdfplumber

    questions = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[1:]:
            raw_text = (page.extract_text() or "").strip()
            questions.append(_clean_pdf_question_text(raw_text))
    return questions
