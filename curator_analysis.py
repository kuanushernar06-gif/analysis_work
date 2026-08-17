"""Кураторлардың бос мәтіндегі жазбаларын Google Gemini API арқылы құрылымды
статистикалық анализге айналдыру (тегін тариф).

Құжат өте үлкен болса (мыс., жүздеген куратор), бір сұранысқа сыймай қалу
қаупіне қарсы мәтін бірнеше бөлікке (chunk) бөлініп, әрқайсысы бөлек
сұраныспен талданады да, нәтижелер бір-біріне қосылып (merge) біріктіріледі
— осылай құжаттың ешбір бөлігі талданбай қалмайды."""

import json
import os
import re
import time
import urllib.error
import urllib.request

from gdocs import count_curator_entries
from netfetch import SSL_CONTEXT, USER_AGENT

MODEL = "gemini-flash-latest"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
REQUEST_TIMEOUT = 120

# Gemini-дің "ойлау" (thinking) режимі жауап жазбас бұрын көрінбейтін
# токендерді пайдаланады (maxOutputTokens бюджетінің ішінде) — сол себепті
# бюджет тым аз болса, соңғы JSON жауап ортасынан үзіліп қалуы мүмкін
# (invalid JSON). Оны болдырмау үшін бюджетті жеткілікті үлкен қойдық.
MAX_COMPLETION_TOKENS = 12_000
# Gemini-дің контекст терезесі өте үлкен болғандықтан, көбіне бүкіл құжат
# бір ғана сұранысқа сияды. Дегенмен өте үлкен құжаттарда (мыс., 150+
# куратор) қауіпсіздік үшін chunk-қа бөлу механизмі сақталған.
MAX_CHUNK_CHARS = 60_000
MAX_STATS_SUBJECTS = 6
MAX_RETRIES = 6
DEFAULT_RETRY_SECONDS = 20

SCHEMA_HINT = """{
  "curator_count": (осы бөліктегі кураторлар саны, бүтін сан немесе null),
  "main_goal": "(осы аптаның негізгі мақсаты, қысқа сөйлем)",
  "top_error_topics": [{"name": "...", "percent": 0-100}, ...] (көбіне 5-ке дейін),
  "low_result_reasons": [{"name": "...", "percent": 0-100}, ...] (көбіне 5-ке дейін),
  "curator_mistakes": [{"name": "...", "percent": 0-100}, ...] (көбіне 5-ке дейін),
  "top_solutions": [{"name": "...", "percent": 0-100}, ...] (көбіне 10-ға дейін),
  "priority_directions": ["...", ...] (келесі аптаға басымдық берілетін 5-ке дейін бағыт),
  "overall_summary": "(2-4 сөйлемдік қысқа жалпы қорытынды)",
  "prevention_measures_taken": "(кураторлар мәтінінде СТ алдында алдын алу шаралары
    (қосымша сабақ, қайталау, мини-тест, т.б.) жасалғаны туралы айтылған ба, соған
    қысқа жауап: 'Иә' немесе 'Жоқ' немесе 'Жартылай', содан кейін бір қысқа сөйлеммен
    түсініктеме. Мәтінде дерек болмаса 'Белгісіз' деп жаз. Нақты аты-жөндерді бұл
    жерге жазба — олар төмендегі missing_analysis_curators және
    no_prevention_curators тізімдеріне барады)",
  "missing_analysis_curators": ["Аты Жөні", ...] (осы бөлікте мүлдем жазба/есеп
    қалдырмаған немесе жазбасы толық бос кураторлардың дәл сол құжаттағыдай
    аты-жөні; ондай куратор болмаса бос тізім),
  "no_prevention_curators": ["Аты Жөні", ...] (жазба жазған, бірақ алдын алу
    шаралары (қосымша сабақ, қайталау, мини-тест, т.б.) туралы мүлдем ештеңе
    айтпаған кураторлардың дәл сол құжаттағыдай аты-жөні; ондай куратор болмаса
    бос тізім)
}"""

PROMPT_TEMPLATE = """Сен білім беру ұйымының деректер аналитигісің. Төменде әр түрлі
кураторлардың апталық СТ (сынақ тапсырма) нәтижелері бойынша жазған бос мәтіндегі
есептерінің бір бөлігі берілген (әр куратордың аты, пәні және жазбасы кезектесіп
келеді; бұл толық құжаттың бір бөлігі болуы мүмкін, қалғаны бөлек талданады).
{stats_section}
Осы мәтінді мұқият оқып, осы бөліктегі кураторлардың жазбаларын біріктіріп,
төмендегі JSON схемасына сәйкес нақты, сандық анализ жаса. Пайыздарды осы
бөліктегі кураторлар арасында қайталанатын тұстардың нақты санағына негіздеп
есепте (мысалы, осы бөліктегі N кураторлың ішінде қаншасы белгілі бір
тақырыпты немесе себепті атады). Мәтінде сан ретінде шығаруға жеткілікті
дерек болмаса, тиісті өрісті null немесе бос тізім қалдыр — ойдан сан
құрастырма.

Әр куратордың жазбасын жеке-жеке тексер: егер куратордың атынан кейін жазба
мүлдем бос болса (немесе "жоқ", "-" секілді мазмұнсыз белгі ғана болса), оның
дәл сол құжаттағы аты-жөнін missing_analysis_curators тізіміне қос. Егер
куратор жазба жазған болса, бірақ онда СТ алдындағы алдын алу шарасы (қосымша
сабақ, қайталау, мини-тест, СЖ, чек-лист, т.б.) туралы бірде-бір сөз айтылмаса,
оның аты-жөнін no_prevention_curators тізіміне қос. Бір куратор екі тізімде
бірдей болмайды — толығымен бос жазба болса, тек missing_analysis_curators-қа
ғана жаз. Аты-жөндерді ойдан шығарма — тек құжатта нақты кездескендерін ғана
жаз.

JSON схемасы:
{schema}

Жауапты ТЕК таза JSON түрінде қайтар — түсіндірме, markdown белгісі (```),
қосымша мәтін қоспа.

Кураторлардың мәтіні:
---
{doc_text}
---
"""


class CuratorAnalysisError(Exception):
    pass


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _format_report_stats(report):
    if not report or not report.get("has_data"):
        return None

    lines = [
        f"- Ортақ орташа балл: {report.get('overall_avg_percent')}% (шикі балл: {report.get('overall_avg_score')})",
        f"- Барлық оқушы саны: {report.get('unique_students')}",
        f"- Проходной балл жинамаған оқушы саны: {report.get('fail_unique_students')}",
        f"- Максималды балл алған нәтиже саны: {report.get('max_achiever_count')}",
    ]

    subjects = (report.get("subjects") or [])[:MAX_STATS_SUBJECTS]
    if subjects:
        lines.append("Пәндер бойынша орташа % (жазба саны, өтпегендер):")
        for s in subjects:
            lines.append(f"  - {s['name']}: {s['avg_percent']}% ({s['count']} жазба, {s['fail']} өтпеген)")

    worst = (report.get("worst_topics") or [])[:5]
    if worst:
        lines.append("Ең төмен нәтижелі тақырыптар:")
        for t in worst:
            lines.append(f"  - {t['name']}: {t['avg_percent']}%")

    return "\n".join(lines)


def _lowercase_first(text):
    return text[0].lower() + text[1:] if text else text


def _join_names(items, limit=5):
    names = [i.get("name") for i in (items or [])[:limit] if i.get("name")]
    if not names:
        return "—"
    # Тізім бір сөйлем ішінде ';' арқылы қосылатындықтан, тек бірінші сөз
    # бас әріппен қалады — қалғандары жаңа сөйлем емес, кіші әріппен жазылады.
    joined = [names[0]] + [_lowercase_first(n) for n in names[1:]]
    return "; ".join(joined)


def _join_plain_names(names):
    names = [n for n in (names or []) if n]
    return "; ".join(names) if names else "Жоқ"


def build_summary_text(report, analysis: dict, label: str = "СТ") -> str:
    """Апта қорытындысын қолданушы белгілеген нақты үлгі бойынша құрастырады:
    сандық бөлігі есептелген report-тан (сенімді), сапалы бөлігі AI талдауынан алынады.
    label — 'СТ' (сабақ тапсыру) немесе 'АТ' (айлық тест); АТ санатында мақсат балл
    жоқ болғандықтан, ол жол мүлде көрсетілмейді (has_targets жоқ болса)."""
    report = report or {}
    analysis = analysis or {}

    lines = [f"{label} ортақ балл: {report.get('overall_avg_score')}"]
    if report.get("has_targets") and report.get("target_achievement_percent") is not None:
        lines.append(f"{label} мақсат орындалу пайызы: {report['target_achievement_percent']}%")
    lines += [
        f"Макс балл алған оқушы саны: {report.get('max_achiever_students')}",
        f"0 алған оқушы саны: {report.get('zero_students')}",
        "",
        f"Оқушылар көп қателескен сұрақтар: {_join_names(analysis.get('top_error_topics'))}",
        f"Алдын алу шаралары жасалды ма: {analysis.get('prevention_measures_taken') or 'Белгісіз'}",
        f"Анализ толтырмаған кураторлар: {_join_plain_names(analysis.get('missing_analysis_curators'))}",
        f"Алдын алу шаралары жасалмаған кураторлар: {_join_plain_names(analysis.get('no_prevention_curators'))}",
        f"Оқушылар неге төмен/жоғары нәтиже көрсетті: {_join_names(analysis.get('low_result_reasons'))}",
        f"Кураторлар тарапынан жіберген қателіктер: {_join_names(analysis.get('curator_mistakes'))}",
        f"Алдын алу шаралары: {_join_names(analysis.get('top_solutions'), limit=10)}",
    ]
    return "\n".join(lines)


def _split_into_chunks(doc_text, max_chars):
    """Мәтінді бос жолдар (абзац) бойынша бөліктерге бөліп, әр chunk-ты
    max_chars-тан аспайтындай етіп жинақтайды — осылай бір куратордың
    жазбасы екі chunk-қа бөлініп кетуі азаяды."""
    blocks = re.split(r"\n\s*\n", doc_text)
    chunks = []
    current = []
    current_len = 0
    for block in blocks:
        block_len = len(block) + 2
        if current and current_len + block_len > max_chars:
            chunks.append("\n\n".join(current))
            current = [block]
            current_len = len(block)
        else:
            current.append(block)
            current_len += block_len
    if current:
        chunks.append("\n\n".join(current))

    safe_chunks = []
    for c in chunks:
        while len(c) > max_chars:
            safe_chunks.append(c[:max_chars])
            c = c[max_chars:]
        if c:
            safe_chunks.append(c)
    return safe_chunks or [doc_text[:max_chars]]


def _extract_retry_delay(error_body_text):
    """Gemini 429 қатесінің денесінен RetryInfo.retryDelay мәнін алады
    (мыс., '"retryDelay": "20s"'), болмаса None қайтарады."""
    match = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', error_body_text)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None
    return None


def _call_gemini_raw(prompt, api_key, json_mode=True):
    generation_config = {"maxOutputTokens": MAX_COMPLETION_TOKENS}
    if json_mode:
        generation_config["responseMimeType"] = "application/json"
    payload = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{API_URL}?key={api_key}",
        data=payload,
        method="POST",
        headers={"content-type": "application/json", "user-agent": USER_AGENT},
    )

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait_seconds = _extract_retry_delay(detail) or DEFAULT_RETRY_SECONDS
                time.sleep(min(wait_seconds, 65) + 1)
                continue
            raise CuratorAnalysisError(f"Gemini API қатесі (HTTP {e.code}): {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise CuratorAnalysisError(f"Gemini API-ге қосыла алмадым: {e.reason}") from e
    else:
        raise CuratorAnalysisError("Gemini API-дің минуттық сұраныс шегінен байланыса алмадым (тегін тариф).")

    candidate = (body.get("candidates") or [{}])[0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise CuratorAnalysisError(
            "Gemini жауабы жауап көлемінің шегінен (maxOutputTokens) асып, ортасынан үзіліп қалды. "
            "Құжаттың осы бөлігі тым күрделі болуы мүмкін — қайта жүктеп көріңіз."
        )

    try:
        text = candidate["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise CuratorAnalysisError("Gemini API жауабының форматы күтпеген болды.") from e

    text = _strip_code_fence(text)
    if not json_mode:
        return text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise CuratorAnalysisError(f"Gemini API жауабын JSON ретінде оқи алмадым: {e}") from e

    if not isinstance(parsed, dict):
        # Кейде модель күтілген объект орнына тізім немесе басқа пішінде
        # жауап қайтарады — мұны төменгі деңгейде (мыс. build_summary_text
        # ішінде) түсініксіз AttributeError ретінде емес, осы жерде анық
        # хабарламамен ұстау керек.
        raise CuratorAnalysisError(
            "Gemini API жауабы күтілген құрылымда (JSON объект) болмады — қайта жүктеп көріңіз."
        )
    return parsed


def _call_gemini(prompt, api_key):
    return _call_gemini_raw(prompt, api_key, json_mode=True)


def _call_gemini_text(prompt, api_key):
    return _call_gemini_raw(prompt, api_key, json_mode=False)


def _analyze_chunk(chunk_text, stats_section, api_key):
    prompt = PROMPT_TEMPLATE.format(schema=SCHEMA_HINT, doc_text=chunk_text, stats_section=stats_section)
    return _call_gemini(prompt, api_key)


def _weighted_merge_list(chunk_items_with_weights, key_field, limit):
    totals = {}
    for weight, items in chunk_items_with_weights:
        if not items or not weight:
            continue
        for it in items:
            name = it.get(key_field)
            pct = it.get("percent")
            if not name or pct is None:
                continue
            totals[name] = totals.get(name, 0.0) + (pct / 100.0) * weight
    total_weight = sum(w for w, _ in chunk_items_with_weights if w) or 1
    merged = [{key_field: name, "percent": round(count / total_weight * 100)} for name, count in totals.items()]
    merged.sort(key=lambda x: x["percent"], reverse=True)
    return merged[:limit]


def merge_analyses(chunk_analyses):
    if len(chunk_analyses) == 1:
        return chunk_analyses[0]

    weights = [max(a.get("curator_count") or 1, 1) for a in chunk_analyses]
    total_curators = sum(weights)

    def merge_field(field, key_field, limit):
        return _weighted_merge_list(
            [(w, a.get(field)) for w, a in zip(weights, chunk_analyses)], key_field, limit
        )

    def merge_unique(field):
        seen = []
        for a in chunk_analyses:
            for name in a.get(field) or []:
                if name not in seen:
                    seen.append(name)
        return seen

    priorities = merge_unique("priority_directions")

    return {
        "curator_count": total_curators,
        "main_goal": next((a.get("main_goal") for a in chunk_analyses if a.get("main_goal")), None),
        "top_error_topics": merge_field("top_error_topics", "name", 5),
        "low_result_reasons": merge_field("low_result_reasons", "name", 5),
        "curator_mistakes": merge_field("curator_mistakes", "name", 5),
        "top_solutions": merge_field("top_solutions", "name", 10),
        "priority_directions": priorities[:5],
        "overall_summary": next((a.get("overall_summary") for a in chunk_analyses if a.get("overall_summary")), None),
        "prevention_measures_taken": next(
            (a.get("prevention_measures_taken") for a in chunk_analyses if a.get("prevention_measures_taken")),
            "Белгісіз",
        ),
        "missing_analysis_curators": merge_unique("missing_analysis_curators"),
        "no_prevention_curators": merge_unique("no_prevention_curators"),
    }


RESULTS_PROMPT_TEMPLATE = """Сен білім беру ұйымының деректер аналитигісің. Төменде оқушылардың
{label} нәтижелері бойынша есептелген НАҚТЫ сандық статистика берілген (кураторлардың жеке
жазбасы жоқ — тек балл кестесінен есептелген деректер).

{stats_section}

Осы деректерді мұқият талдап, оқушылардың нәтижесі бойынша қысқа, нақты қорытынды жаз: жалпы
деңгей қандай, қай пән/тақырыптар ең әлсіз, бұл неге байланысты болуы мүмкін (жалпы болжам),
келесі кезеңге қандай басымдық беру керек. 4-6 сөйлемнен аспасын. Жоғарыда берілмеген сандарды
ойдан шығарма.

Жауапты ТЕК қорытынды мәтін түрінде қайтар — JSON, markdown белгісі немесе қосымша тақырып қоспа.
"""


def generate_results_analysis(report, label: str = "ДТ/БТ") -> str:
    """Кураторлардың жазбасынсыз, тек импортталған балл кестесінен есептелген
    report статистикасы негізінде AI-ден оқушылардың нәтижесі бойынша қысқа
    қорытынды сұрайды — ДЕҢГЕЙЛІК/БАЙҚАУ ТЕСТ санатында куратор құжаты
    тұжырымдамасы жоқ болғандықтан керек."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        api_key = "".join(ch for ch in api_key.strip() if ch.isascii() and ch.isprintable())
    if not api_key:
        raise CuratorAnalysisError(
            "GEMINI_API_KEY орнатылмаған. .env файлына тегін Gemini API кілтін қосыңыз "
            "(aistudio.google.com сайтынан алуға болады)."
        )

    stats_text = _format_report_stats(report)
    if not stats_text:
        raise CuratorAnalysisError("Алдымен кестені импорттаңыз — сандық деректер жоқ.")

    prompt = RESULTS_PROMPT_TEMPLATE.format(label=label, stats_section=stats_text)
    return _call_gemini_text(prompt, api_key)


def generate_curator_analysis(doc_text: str, report=None) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        # Ортам айнымалысын веб-интерфейс арқылы қою кезінде кейде көзге
        # көрінбейтін ASCII емес таңбалар (мыс. бос орынның басқа түрі)
        # ілесіп қалады — олар кілтті шын мәнінде бұзбаса да, HTTP сұрау
        # жолын кодтау кезінде түсініксіз UnicodeEncodeError тудырады.
        api_key = "".join(ch for ch in api_key.strip() if ch.isascii() and ch.isprintable())
    if not api_key:
        raise CuratorAnalysisError(
            "GEMINI_API_KEY орнатылмаған. .env файлына тегін Gemini API кілтін қосыңыз "
            "(aistudio.google.com сайтынан алуға болады)."
        )

    stats_text = _format_report_stats(report)
    stats_section = ""
    if stats_text:
        stats_section = (
            "\nБұл аптаның импортталған кестеден есептелген НАҚТЫ сандық нәтижелері:\n"
            f"{stats_text}\n"
        )

    chunks = _split_into_chunks(doc_text, MAX_CHUNK_CHARS)
    chunk_analyses = []
    for i, chunk in enumerate(chunks):
        if i > 0:
            # Минуттық сұраныс шегіне сыю үшін бөліктер арасында аздап
            # күтеміз (retry логикасы 429 кезінде қосымша күтеді).
            time.sleep(2)
        chunk_analyses.append(_analyze_chunk(chunk, stats_section, api_key))

    merged = merge_analyses(chunk_analyses)
    # AI-дің curator_count-ты еркін мәтіннен болжамдап санауы (әсіресе үлкен
    # құжатта) дәл болмайды — соның орнына құжат құрылымынан (аты + '\t-'
    # жазба басы) тікелей, дәл санап, үстінен жазамыз.
    merged["curator_count"] = count_curator_entries(doc_text)
    return merged
