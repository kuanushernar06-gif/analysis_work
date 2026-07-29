import io
import re
import urllib.error

import openpyxl

from netfetch import urlopen

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")


class SheetFetchError(Exception):
    pass


def _export_url(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url:
        raise SheetFetchError("Сілтеме бос болмауы керек.")
    if "format=xlsx" in raw_url:
        return raw_url
    match = SHEET_ID_RE.search(raw_url)
    if not match:
        raise SheetFetchError(
            "Google Sheets сілтемесін тани алмадым. "
            "Сілтеме '/spreadsheets/d/...' түрінде болу керек."
        )
    sheet_id = match.group(1)
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"


def fetch_workbook(raw_url: str):
    """Google Sheets кестесінің БАРЛЫҚ парақтарын (лист/tab) жүктеп қайтарады.
    Әр парақ бір куратордың кестесі деп есептеледі — парақ атауы куратор аты
    ретінде қолданылады. Қайтарады: [(sheet_name, rows), ...] тізімі."""
    export_url = _export_url(raw_url)
    try:
        with urlopen(export_url, timeout=30) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SheetFetchError(
                "Кестеге қол жеткізе алмадым (рұқсат жоқ). Google Sheets-те "
                "'Файл → Ортаққа бөлу → Сілтемесі бар кез келген адам → Көруші' "
                "етіп кестені ашық етіп қойыңыз."
            ) from e
        raise SheetFetchError(f"Кестені жүктеу сәтсіз аяқталды (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise SheetFetchError(f"Кестені жүктеу сәтсіз аяқталды: {e.reason}") from e

    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw_bytes), data_only=True, read_only=True)
    except Exception as e:
        raise SheetFetchError(f"Кестені оқу сәтсіз аяқталды: {e}") from e

    sheets = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(["" if v is None else str(v).strip() for v in row])
        while rows and all(cell == "" for cell in rows[-1]):
            rows.pop()
        if rows:
            sheets.append((ws.title, rows))

    if not sheets:
        raise SheetFetchError("Кестеде деректер табылмады.")
    return sheets


def rows_to_dicts(rows):
    header = [h.strip() for h in rows[0]]
    body = rows[1:]
    return header, body


STUDENT_KEYWORDS = ["аты-жөні", "оқушы", "фио", "тегі", "аты", "name", "student"]
SCORE_KEYWORDS = ["балл", "ұпай", "score", "нәтиже", "ст"]
MAX_SCORE_KEYWORDS = ["максимум", "максимал", "жалпы балл", "max"]
SUBJECT_KEYWORDS = ["пән", "предмет", "subject"]
TOPIC_KEYWORDS = ["тақырып", "тема", "topic"]
ROW_NUMBER_HEADERS = {"№", "n", "no", "нөмір", "рет саны", "п/п", "р/с"}
NUMERIC_FALLBACK_MIN_RATIO = 0.7
NUMERIC_SAMPLE_ROWS = 50

SUMMARY_ROW_KEYWORDS = [
    "орташа",
    "орта балл",
    "ортақ балл",
    "ортақ ұпай",
    "жиынтық",
    "қорытынды",
    "барлығы",
    "барлыгы",
    "итого",
    "всего",
    "average",
    "total",
    "сумма",
]


def is_summary_row(name: str) -> bool:
    """Кестенің соңындағы 'Орташа ұпайы', 'Барлығы' сияқты қорытынды жолдарды
    (нақты оқушы емес) анықтайды — олар импорттан тыс қалдырылады."""
    lower = (name or "").strip().lower()
    if not lower:
        return False
    return any(kw in lower for kw in SUMMARY_ROW_KEYWORDS)


def is_template_sheet(name: str) -> bool:
    """Кураторларға парақты қалай толтыру керектігін көрсету үшін қалдырылған
    'ҮЛГІ' атты парақты (нақты куратор емес) анықтайды — импортқа қосылмайды."""
    return (name or "").strip().lower() == "үлгі"


def _find_by_keywords(lower_header, keywords, exclude=()):
    for i, h in enumerate(lower_header):
        if i in exclude:
            continue
        if h in keywords:
            return i
    for i, h in enumerate(lower_header):
        if i in exclude:
            continue
        if any(kw in h for kw in keywords):
            return i
    return None


def _is_sequential_index(values):
    """Баған мәндері рет нөмірі (1, 2, 3, ...) ме — тексереді, топ ортасында
    (мыс., бір парақта бірнеше сынып біріктірілгенде) 1-ден қайта басталуы
    мүмкін екенін де ескереді. Кестенің бірінші бағаны көбіне '№'/'рет саны'
    болғанымен, кейде тақырыпсыз (бос атаумен) келеді, сондықтан баған
    атауына қарамай, мәндерінің өзінен де осындай рет нөмірі екенін
    анықтаймыз (әйтпесе ол баллдың орнына қате таңдалып қалуы мүмкін)."""
    if len(values) < 5:
        return False
    try:
        nums = [float(v.replace(",", ".")) for v in values]
    except ValueError:
        return False
    if any(n != round(n) for n in nums):
        return False
    if nums[0] != 1:
        return False
    for prev, cur in zip(nums, nums[1:]):
        if cur != prev + 1 and cur != 1:
            return False
    return True


def _find_numeric_column(header, body, exclude):
    lower_header = [h.lower().strip() for h in header]
    best_idx, best_ratio = None, 0.0
    for i in range(len(header)):
        if i in exclude or lower_header[i] in ROW_NUMBER_HEADERS:
            continue
        numeric = 0
        total = 0
        values = []
        for row in body[:NUMERIC_SAMPLE_ROWS]:
            if i >= len(row) or row[i] == "":
                continue
            total += 1
            try:
                float(row[i].replace(",", "."))
                numeric += 1
                values.append(row[i])
            except ValueError:
                pass
        if total == 0 or _is_sequential_index(values):
            continue
        ratio = numeric / total
        if ratio > best_ratio:
            best_ratio, best_idx = ratio, i
    if best_ratio >= NUMERIC_FALLBACK_MIN_RATIO:
        return best_idx
    return None


def guess_columns(header, body):
    """Баған атаулары бойынша (немесе сан басым баған эвристикасымен) оқушы,
    балл, пән, тақырып, максимум балл бағандарын автоматты анықтайды —
    пайдаланушыдан қолмен таңдауды талап етпейді."""
    lower = [h.lower().strip() for h in header]

    student_idx = _find_by_keywords(lower, STUDENT_KEYWORDS)
    exclude = {student_idx} if student_idx is not None else set()

    score_idx = _find_by_keywords(lower, SCORE_KEYWORDS, exclude=exclude)
    if score_idx is None:
        score_idx = _find_numeric_column(header, body, exclude=exclude)
    if score_idx is not None:
        exclude = exclude | {score_idx}

    max_score_idx = _find_by_keywords(lower, MAX_SCORE_KEYWORDS, exclude=exclude)
    subject_idx = _find_by_keywords(lower, SUBJECT_KEYWORDS, exclude=exclude)
    topic_idx = _find_by_keywords(lower, TOPIC_KEYWORDS, exclude=exclude)

    return {
        "student": student_idx,
        "score": score_idx,
        "max_score": max_score_idx,
        "subject": subject_idx,
        "topic": topic_idx,
    }
