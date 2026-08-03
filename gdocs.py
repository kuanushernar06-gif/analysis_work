import re
import urllib.error

from netfetch import urlopen

DOC_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9-_]+)")

MAX_DOC_CHARS = 50_000  # бір құжаттан алынатын мәтіннің шегі (қорытынды бетінің шектен тыс ұзармауы үшін)

# Google Docs-тан .txt экспорттағанда кейде көзге көрінбейтін "zero-width"
# таңбалар ілесіп қалады (мыс. көшіріп-жапсырғанда) — олар \s регексіне
# сәйкес келмейді, сондықтан "6.1.1.1 ..." секілді жолдардың басындағы осындай
# таңба оқу мақсаты кодын тани алмай қалуға (тақырыпқа қате жіктелуге) немесе
# ҮЛГІ жазбасының шекарасын тани алмауға әкеледі. Сол себепті құжат
# жүктелген сәтте бірден тазартамыз — төмендегі логиканың барлығы содан кейін
# осы таза мәтінмен жұмыс істейді.
_INVISIBLE_CHARS_RE = re.compile("[​‌‍⁠﻿]")


def _strip_invisible_chars(text: str) -> str:
    return _INVISIBLE_CHARS_RE.sub("", text)


class DocFetchError(Exception):
    pass


_TEMPLATE_NEXT_ENTRY_RE = re.compile(r"\n\t([А-ЯӘҒҚҢӨҰҮҺІ][^\n\t]{1,40})\n\t-", re.UNICODE)
_CURATOR_ENTRY_RE = re.compile(r"(?:^|\n)\t([А-ЯӘҒҚҢӨҰҮҺІ][^\n\t]{1,40})\n\t-", re.UNICODE)


def strip_template_entry(text: str) -> str:
    """Кураторлар анализі құжатында кураторларға толтыру үлгісі ретінде
    қалдырылған 'ҮЛГІ' деп белгіленген мысал жазбаны толық алып тастайды —
    ол нақты куратордың деректері емес, талдауға (AI-ге де, көрсетуге де)
    қосылмауы керек."""
    normalized = _strip_invisible_chars(text.replace("\r\n", "\n").replace("\r", "\n"))
    marker = re.search(r"ҮЛГІ", normalized)
    if not marker:
        return normalized
    next_entry = _TEMPLATE_NEXT_ENTRY_RE.search(normalized, marker.end())
    if not next_entry:
        return normalized
    # ҮЛГІ жолының алдындағы табуляцияны (prefix) және келесі жазбаның өз
    # табуляциясын (suffix, next_entry.start() — сол жазбаның '\n'-інен
    # басталады) қосарлап қалдырмау керек — әйтпесе одан кейінгі куратордың
    # аты '\n\t\tАты' болып, "\n\t<Аты>" үлгісіне сәйкес келмей, есептен
    # (санаудан да, AI-ден де) түсіп қалады.
    return normalized[: marker.start()].rstrip("\t") + normalized[next_entry.start() :]


def count_curator_entries(text: str) -> int:
    """Құжаттағы нақты (қайталанбайтын) куратор жазбаларының санын мәтін
    құрылымынан (аты + '\\t-' басталатын жазба) тікелей санайды — AI-дің
    еркін мәтіннен болжамдап санауына (үлкен құжатта дәл болмауы мүмкін)
    сенбей, дәл сан алу үшін. ҮЛГІ жазбасы бұл функцияға жеткенше
    strip_template_entry-мен алынып тасталған болуы керек."""
    normalized = _strip_invisible_chars(text.replace("\r\n", "\n").replace("\r", "\n"))
    names = {m.group(1).strip() for m in _CURATOR_ENTRY_RE.finditer(normalized)}
    return len(names)


def normalize_doc_url(raw_url: str) -> str:
    """Google Docs сілтемесін (әдеттегі 'edit' сілтемесі де) мәтіндік
    экспорт сілтемесіне айналдырады."""
    raw_url = raw_url.strip()
    if not raw_url:
        raise DocFetchError("Сілтеме бос болмауы керек.")

    if "format=txt" in raw_url:
        return raw_url

    match = DOC_ID_RE.search(raw_url)
    if not match:
        raise DocFetchError(
            "Google Docs сілтемесін тани алмадым. "
            "Сілтеме '/document/d/...' түрінде болу керек."
        )
    doc_id = match.group(1)
    return f"https://docs.google.com/document/d/{doc_id}/export?format=txt"


def fetch_doc_text(raw_url: str, max_chars: int = MAX_DOC_CHARS) -> str:
    """Сілтемеден құжаттың мәтінін жүктеп қайтарады."""
    export_url = normalize_doc_url(raw_url)
    try:
        with urlopen(export_url, timeout=20) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise DocFetchError(
                "Құжатқа қол жеткізе алмадым (рұқсат жоқ). Google Docs-та "
                "'Ортаққа бөлу → Сілтемесі бар кез келген адам → Көруші' "
                "етіп қойыңыз."
            ) from e
        if e.code == 404:
            raise DocFetchError("Құжат табылмады. Сілтемені тексеріңіз.") from e
        raise DocFetchError(f"Құжатты жүктеу сәтсіз аяқталды (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise DocFetchError(f"Құжатты жүктеу сәтсіз аяқталды: {e.reason}") from e

    text = _strip_invisible_chars(raw_bytes.decode("utf-8-sig", errors="replace")).strip()
    if not text:
        raise DocFetchError("Құжат бос болып тұр.")
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "\n\n… (мәтін қысқартылды, құжат тым ұзын)"
    return text


MONTH_MARKER_RE = re.compile(r"\t(\d+)-АЙ\r?\n")
WEEK_MARKER_RE = re.compile(r"\t(\d+)-АПТА\r?\n")


class PlanParseError(Exception):
    pass


def _clean_plan_block(raw: str) -> str:
    """Кестенің әр торкөзі (topic/objectives/stats) арасындағы бос
    жолдарды бір ғана абзац үзілісіне келтіреді, оқуға ыңғайлы қылу үшін."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]*\n[\n \t]*\t?", "\n\n", text)
    text = re.sub(r"^\t", "", text, flags=re.MULTILINE)
    return text.strip()


def parse_weekly_plan(doc_text: str) -> dict:
    """'N-АЙ' / 'N-АПТА' белгілері бойынша құрылған 5 айлық жоспар құжатын
    {(ай_нөмірі, апта_нөмірі): мәтін} сөздігіне бөледі. Құжат Google Docs-тағы
    кестені export?format=txt арқылы жүктегенде, әр торкөз '\\t' таңбасынан
    басталатын бөлек жол болып шығады — осы белгілерге сүйенеміз."""
    months = [(int(m.group(1)), m.start()) for m in MONTH_MARKER_RE.finditer(doc_text)]
    weeks = [(int(w.group(1)), w.start(), w.end()) for w in WEEK_MARKER_RE.finditer(doc_text)]

    if not weeks:
        raise PlanParseError(
            "Құжаттан 'N-АПТА' белгілерін таба алмадым. Құжат '1-АЙ', '1-АПТА' "
            "түрінде құрылымдалған кесте болуы керек."
        )

    def month_for(pos):
        current = None
        for month_num, month_pos in months:
            if month_pos <= pos:
                current = month_num
            else:
                break
        return current

    all_marker_positions = sorted([p for _, p in months] + [s for _, s, _ in weeks])

    result = {}
    for week_num, week_start, week_end in weeks:
        month_num = month_for(week_start)
        if month_num is None:
            continue
        later_positions = [p for p in all_marker_positions if p > week_start]
        block_end = min(later_positions) if later_positions else len(doc_text)
        content = _clean_plan_block(doc_text[week_end:block_end])
        if content:
            result[(month_num, week_num)] = content

    return result


_PLAN_OBJECTIVE_RE = re.compile(r"^\s*\d+\.\d+\.\d+")
_PLAN_SCOPE_KEYWORDS = ("Видеосабақ", "Авторлық", "Оқулық бет", "чанк", "Практикалық сабақ")


def classify_plan_sections(plan_text: str) -> dict:
    """Апта жоспарының мәтінін мазмұны бойынша 3 бөлікке ажыратады (бос жол
    орналасуына қарамай, себебі құжатта бұл сәйкессіз): '§' бар жолдар —
    тақырыптар, 'N.N.N...' стандарт коды бар жолдар — оқу мақсаты, ал
    видео/бет/чанк санын көрсететін жолдар — тақырып ауқымы (көлемі)."""
    topics, objectives, scope = [], [], []
    for line in _strip_invisible_chars(plan_text or "").split("\n"):
        if not line.strip():
            continue
        if _PLAN_OBJECTIVE_RE.match(line):
            objectives.append(line)
        elif any(kw in line for kw in _PLAN_SCOPE_KEYWORDS):
            scope.append(line)
        else:
            topics.append(line)

    return {
        "topics": "\n".join(topics).strip(),
        "objectives": "\n".join(objectives).strip(),
        "scope": "\n".join(scope).strip(),
    }
