"""Материал тексеру: белгілі бір жазбаны (үй жұмысы, quiz, база, т.б.)
таңдалған кітаппен салыстырып, Claude арқылы тексеру.

Кітап book_chunks-та бет-бетімен (1 бет = 1 жазба) сақталған болу керек
(books_ingest.py арқылы толық оқылған, ingest_status='done'). Тексеру
кітапты PAGES_PER_BATCH беттен топтап, әр топ бойынша материалдағы сәйкес
тұстарды тексереді де, нәтижелерді біріктіреді — осылай бір сұраныста бүкіл
кітапты бірден салыстырудан аулақ боламыз (Vercel-дің сұраныс уақыты шегі
және Claude-тың контекст сапасы үшін). Барлық топ өткен соң, жиналған
кестені тағы бір рет қарап шығатын қорытынды тексеру қадамы жүреді."""

import base64
import json
import re
import time
import urllib.error
import urllib.request

from books_ingest import download_drive_file, BookIngestError
from gdocs import fetch_doc_text, DocFetchError, parse_weekly_plan, classify_plan_sections, PlanParseError
from netfetch import SSL_CONTEXT, USER_AGENT

CHECK_MODEL = "claude-sonnet-5"
CHECK_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 180
MAX_COMPLETION_TOKENS = 8_000
MAX_RETRIES = 4
DEFAULT_RETRY_SECONDS = 20

# Куратор нұсқауы бойынша материалды кітаппен әр 10 беттен кезек-кезек
# салыстырады (фокус шашырамас үшін), бірақ нәтиже осы функцияны шақыратын
# жақта (step_material_check) біріктіріліп, тек соңында ортақ тізім ретінде
# қайтарылады — әр 10 бет сайын бөлек нәтиже берілмейді.
PAGES_PER_BATCH = 10

# targeted режимде (апта бойынша) бір апта ондаған тақырыпты қамтуы мүмкін —
# барлық тақырыптың кітап үзінділерін бір Claude сұранысына қоссақ, сұраныс
# көлемі шектен асып кетеді (HTTP 413 "request_too_large"). Сол себепті
# ranges тізімін осы санмен топтап, партия-партия жібереміз. Кітап беттері
# сурет/сканерленген болуы мүмкін (мәтінге қарағанда әлдеқайда ауыр), сол
# себепті бір партияда тым көп кітап үзіндісі болмас үшін бұл сан кіші
# ұсталады.
RANGES_PER_BATCH = 1

# find_topic_pages кейде тақырыптың ауқымын шектен тыс кең қайтаруы мүмкін
# (мыс. бүкіл тарауды 1 тақырып деп қате белгілеу) — бір топикке кететін
# беттерді осы санмен шектейміз, сұраныс көлемі бақылаудан шықпас үшін.
MAX_TOPIC_PAGE_SPAN = 12

# find_topic_pages бұрын БҮКІЛ кітап PDF-ін бір сұранысқа жіберетін — 240
# беттік сканерленген оқулық 30-40MB-қа дейін тартып, Claude API-дің 32MB
# шегінен әрдайым асып, HTTP 413 тудыратын. Енді кітапты осы санмен
# бөліктерге бөліп, әр step-те тек БІР бөлікті іздейміз (табылмаса, келесі
# step келесі бөлікті тексереді) — сұраныс көлемі кітап көлеміне
# тәуелсіз, әрдайым шағын болып қалады.
TOPIC_SEARCH_CHUNK_PAGES = 20

# Claude API сұраныстың жалпы көлемін ~32MB-пен шектейді. Тексерілетін
# материалдың өзі (PDF) әр Claude сұранысына ТОЛЫҚ күйінде қоса жіберіледі
# (кітап үзіндісі қанша шектелсе де, бұл бөлік кішіре­юмейді) — сол себепті
# материал файлы өте үлкен (мыс. сканерленген) болса, кітап жағын қанша
# кішірейтсек те HTTP 413 қайталана береді. Мұны кітап үзіндісінен ертерек,
# нақты себебін көрсетіп хабарлау үшін шектейміз. Кітап үзіндісі де өз
# алдына MAX_BOOK_CHUNK_BYTES-пен шектелген (books_ingest.py) — екеуінің
# қосындысы base64-те де 32MB шегінен қауіпсіз төмен қалуы үшін бұл мән
# ықшамдалған.
MAX_MATERIAL_PDF_BYTES = 11_000_000

# Claude API-ге ЖІБЕРМЕЙ ТҰРЫП тексеретін соңғы қорғаныс — жоғарыдағы екі
# шек (материал + кітап) дұрыс жұмыс істесе, қосынды бұған ешқашан
# жетпеуі керек. Дегенмен бір бөлік бірнеше рет қосылып кетсе немесе
# болашақта шектер өзгертілсе, желіге бекер сұраныс жіберіп, түсініксіз
# HTTP 413 алудың орнына осы жерде анық диагнозбен тоқтатамыз.
MAX_REQUEST_BODY_BYTES = 30_000_000

CRITERIA_BY_TYPE = {
    "uy_zhumysy": """Үй жұмысындағы әр сұрақ пен жауапты кітаптағы сәйкес тұсымен салыстыр.
Тексеру келесі санаттар бойынша болсын:
1) жауаптың мазмұны кітапқа сай ма
2) сұрақтың берілгені және жауабы кітап бойынша дұрыс па
3) аппелияциялық/шатастыратын сұрақтар бар ма
4) орфографиялық/грамматикалық қателіктер
5) техникалық/дизайндық қателіктер
6) вариант дұрыс құралған ба""",
    "quiz_test": """Quiz тесттегі әр сұрақ пен жауапты кітаптағы сәйкес тұсымен салыстыр.
Тексеру келесі санаттар бойынша болсын:
1) жауаптың мазмұны кітапқа сай ма
2) сұрақтың берілгені және жауабы кітап бойынша дұрыс па
3) аппелияциялық/шатастыратын сұрақтар бар ма
4) орфографиялық/грамматикалық қателіктер
5) техникалық/дизайндық қателіктер
6) вариант дұрыс құралған ба""",
    "baza": """Базадағы әр сұрақ пен жауапты кітаптағы сәйкес тұсымен салыстыр.
Тексеру келесі санаттар бойынша болсын:
1) жауаптың мазмұны кітапқа сай ма
2) сұрақтың берілгені және жауабы кітап бойынша дұрыс па
3) аппелияциялық/шатастыратын сұрақтар бар ма
4) орфографиялық/грамматикалық қателіктер
5) техникалық/дизайндық қателіктер""",
    "sabaq_tapsyru_material": """Сабақ тапсырудағы әр сұрақ пен жауапты кітаптағы сәйкес тұсымен салыстыр.
Тексеру келесі санаттар бойынша болсын:
1) жауаптың мазмұны кітапқа сай ма
2) сұрақтың берілгені және жауабы кітап бойынша дұрыс па
3) аппелияциялық/шатастыратын сұрақтар бар ма
4) орфографиялық/грамматикалық қателіктер
5) техникалық/дизайндық қателіктер
6) вариант дұрыс құралған ба""",
    "taqyryptyq_test": """Тақырыптық тестті әр сұрақ пен жауапты кітаптағы сәйкес тұсымен салыстыр.
Тексеру келесі санаттар бойынша болсын:
1) жауаптың мазмұны кітапқа сай ма
2) сұрақтың берілгені және жауабы кітап бойынша дұрыс па
3) аппелияциялық/шатастыратын сұрақтар бар ма
4) орфографиялық/грамматикалық қателіктер
5) техникалық/дизайндық қателіктер
6) вариант дұрыс құралған ба""",
}

def parse_plan_weeks(plan_text):
    """Жоспар мәтінінен әр (ай, апта) үшін сол апталық тақырыптар тізімін
    алады: {(ай, апта): {"topics": [str, ...]}}. Бір аптада бірнеше тақырып
    болуы қалыпты жағдай (әртүрлі сыныптарға арналған параллель тақырыптар) —
    әрқайсысы classify_plan_sections-тың "topics" бөлігіндегі жеке жол
    ретінде келеді. Бет нөмірі жоспарда көрсетілмейді деп есептейміз (кітап
    мазмұнынан find_topic_pages арқылы табылады)."""
    try:
        weeks = parse_weekly_plan(plan_text)
    except PlanParseError:
        return {}

    result = {}
    for (month, week), text in weeks.items():
        sections = classify_plan_sections(text)
        topics = [line.strip() for line in sections["topics"].split("\n") if line.strip()]
        if not topics:
            topics = [f"{month}-ай {week}-апта"]
        result[(month, week)] = {"topics": topics}
    return result




FINDING_SCHEMA_HINT = """[
  {
    "question_number": "(тапсырмадағы сұрақ/тапсырма нөмірі немесе қысқа атауы)",
    "test_variant": "(тапсырмада берілген нұсқа/жауап қысқаша)",
    "book_correct": "(кітап бойынша дұрысы, қысқаша)",
    "error_type": "(қате түрі: мазмұн/дұрыстық/аппеляциялық/орфографиялық/техникалық/вариант құрылымы — немесе 'Қате жоқ' деп жазба, тек қате табылғанда ғана осы жазбаны қос)",
    "confidence": "(жоғары/орташа/төмен)"
  }
]"""

BATCH_PROMPT = """Сен білім беру материалдарын тексеретін сарапшысың.

{criteria}

Төменде эталон кітаптың {page_start}-{page_end} беттерінің мазмұны берілген.
Тексерілетін материалда осы беттерге қатысты сұрақ/тапсырма кездессе, соны
осы кітап мәтінімен салыстырып тексер. Егер бұл беттерге қатысты ешбір
сұрақ/тапсырма болмаса, бос тізім қайтар — ойдан қате құрастырма.

Кітаптың {page_start}-{page_end} беттері:
---
{book_segment}
---

Жауапты ТЕК осы JSON схемасына сәйкес таза JSON тізім түрінде қайтар —
түсіндірме, markdown белгісі (```), қосымша мәтін қоспа. Қате табылмаса, бос
тізім [] қайтар.

JSON схемасы:
{schema}
"""

FINAL_REVIEW_PROMPT = """Сен білім беру материалдарын тексеретін сарапшысың. Төменде материалды
кітаппен бөлік-бөлеп салыстыру арқылы жиналған қателер тізімі берілген
(әр бөлік кітаптың басқа беттерін қамтыған).

{criteria}

Осы тізімді тағы бір рет мұқият қарап шық:
- Қайталанатын жазбалар болса, біріктір.
- Бір-біріне қайшы келетін жазбалар болса, дұрысын таңда.
- Тізім құрылымы дұрыс, түсінікті болсын.

Жиналған қателер тізімі:
{findings}

Жауапты ТЕК осы JSON схемасына сәйкес таза JSON тізім түрінде қайтар —
түсіндірме, markdown белгісі (```), қосымша мәтін қоспа.

JSON схемасы:
{schema}
"""


class MaterialCheckError(Exception):
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


def fetch_material_content(link: str):
    """Тексерілетін материалдың мазмұнын жүктейді. Google Docs сілтемесі
    болса мәтін ретінде, Google Drive файлы (PDF) болса байт ретінде
    қайтарады: (kind, content) — kind 'text' немесe 'pdf'."""
    link = (link or "").strip()
    if not link:
        raise MaterialCheckError("Материалдың сілтемесі жоқ.")

    if "docs.google.com/document" in link:
        try:
            return "text", fetch_doc_text(link, max_chars=150_000)
        except DocFetchError as e:
            raise MaterialCheckError(str(e)) from e

    if "drive.google.com" in link or "docs.google.com" in link:
        try:
            data = download_drive_file(link)
        except BookIngestError as e:
            raise MaterialCheckError(str(e)) from e
        if data[:5] == b"%PDF-":
            if len(data) > MAX_MATERIAL_PDF_BYTES:
                raise MaterialCheckError(
                    "Тексерілетін материалдың файлы тым үлкен "
                    f"({len(data) / 1_000_000:.1f}MB) — Claude API мұндай "
                    "көлемдегі сұранысты қабылдамайды. Бұл әдетте файл "
                    "сканерленген суреттерден тұрғанда болады. Файлды "
                    "сығып (PDF compress) немесе Google Docs форматында "
                    "қайта жүктеп көріңіз."
                )
            return "pdf", data
        raise MaterialCheckError(
            "Бұл Drive файлы PDF емес сияқты — қазірше тек Google Docs "
            "немесе PDF (Google Drive) сілтемелерін тексере аламын."
        )

    raise MaterialCheckError(
        "Сілтемені тани алмадым — тек Google Docs немесе Google Drive "
        "сілтемесін қолдай аламын."
    )


def _extract_retry_delay(headers):
    value = headers.get("retry-after") if headers else None
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _call_claude_with_key(content_blocks, api_key, thinking_disabled=True, expect=list, context="?"):
    payload = {
        "model": CHECK_MODEL,
        "max_tokens": MAX_COMPLETION_TOKENS,
        "messages": [{"role": "user", "content": content_blocks}],
    }
    if thinking_disabled:
        payload["thinking"] = {"type": "disabled"}

    body_bytes = json.dumps(payload).encode("utf-8")
    # 413 болғанда нақты қай бөлік (материал ма, кітап үзіндісі ме) үлкен
    # екенін бірден көру үшін — диагностика.
    block_sizes = ", ".join(
        f"{b.get('type', '?')}:{len(b.get('source', {}).get('data', '')) or len(b.get('text', ''))}b"
        for b in content_blocks
    )

    # MAX_MATERIAL_PDF_BYTES/MAX_BOOK_CHUNK_BYTES жоғарыда бет/файл деңгейінде
    # шектейді, бірақ бірнеше құжат бір сұранысқа қосылса (немесе болашақта
    # осы шектер өзгерсе), қосынды дегенмен Claude-тың ~32MB шегінен асып
    # кетуі мүмкін. Сол жағдайда желіге сұраныс жібермей-ақ, дәл осы жерде,
    # анық себебін көрсетіп тоқтатамыз — расталмаған HTTP 413-тен гөрі
    # бірден нақты диагноз беру үшін.
    if len(body_bytes) > MAX_REQUEST_BODY_BYTES:
        raise MaterialCheckError(
            f"Сұраныс көлемі тым үлкен ({len(body_bytes) / 1_000_000:.1f}MB, "
            f"шегі {MAX_REQUEST_BODY_BYTES / 1_000_000:.0f}MB) — Claude API-ге "
            f"жібермей тұрып тоқтатылды. [{context}] Бөліктер: {block_sizes}"
        )

    req = urllib.request.Request(
        CHECK_API_URL,
        data=body_bytes,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "user-agent": USER_AGENT,
        },
    )

    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=SSL_CONTEXT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in (429, 500, 529) and attempt < MAX_RETRIES - 1:
                wait_seconds = _extract_retry_delay(e.headers) or DEFAULT_RETRY_SECONDS
                time.sleep(min(wait_seconds, 65) + 1)
                continue
            try:
                detail_msg = json.loads(detail).get("error", {}).get("message", "") or detail[:200]
            except (json.JSONDecodeError, AttributeError):
                detail_msg = detail[:200]
            if e.code == 413:
                detail_msg += (
                    f" [сұраныс көлемі: {len(body_bytes) / 1_000_000:.1f}MB, "
                    f"бөліктер: {block_sizes}]"
                )
            raise MaterialCheckError(f"Claude API қатесі (HTTP {e.code}): {detail_msg}") from e
        except urllib.error.URLError as e:
            raise MaterialCheckError(f"Claude API-ге қосыла алмадым: {e.reason}") from e
    else:
        raise MaterialCheckError("Claude API-мен көп қайталаудан кейін де байланыса алмадым.")

    if body.get("stop_reason") == "refusal":
        raise MaterialCheckError("Claude бұл сұранысты қауіпсіздік саясаты бойынша орындаудан бас тартты.")
    if body.get("stop_reason") == "max_tokens":
        raise MaterialCheckError(
            "Claude жауабы жауап көлемінің шегінен асып, ортасынан үзіліп қалды."
        )

    try:
        text = next(b["text"] for b in body["content"] if b.get("type") == "text")
    except (KeyError, StopIteration, TypeError) as e:
        raise MaterialCheckError("Claude API жауабының форматы күтпеген болды.") from e

    text = _strip_code_fence(text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise MaterialCheckError(f"Claude API жауабын JSON ретінде оқи алмадым: {e}") from e
    if not isinstance(parsed, expect):
        kind = "тізім" if expect is list else "объект"
        raise MaterialCheckError(f"Claude API жауабы күтілген құрылымда (JSON {kind}) болмады.")
    return parsed


def _material_content_block(material_kind, material_content):
    if material_kind == "pdf":
        b64 = base64.standard_b64encode(material_content).decode("ascii")
        return {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        }
    return {"type": "text", "text": f"Тексерілетін материал мәтіні:\n---\n{material_content}\n---"}


FIND_TOPIC_PROMPT = """Сен оқулықпен жұмыс істейтін көмекшісің. Төменде толық
оқулықтың PDF файлы тіркелген. Осы оқулықтан мына тақырыпты тап:

«{topic}»

Тақырыпты § нөмірі, атауы немесе мазмұны бойынша тап (жоспар құжатында бет
нөмірі көрсетілмеген, сол себепті кітаптың өз мазмұны/мәтіні бойынша іздеу
керек). Тақырып қай беттен басталып, қай бетте аяқталатынын анықта (кіріспе
бет пен тапсырмалар/сұрақтар бөлімін қоса алғанда, тиесілі толық ауқымды).

Жауапты ТЕК осы JSON схемасына сәйкес таза JSON объект түрінде қайтар —
түсіндірме, markdown белгісі (```), қосымша мәтін қоспа:
{{"page_start": (сан) немесе null, "page_end": (сан) немесе null}}

Тақырыпты кітаптан таба алмасаң, {{"page_start": null, "page_end": null}} қайтар."""


def find_topic_pages(book_pdf_bytes, topic_text, api_key, context_note=""):
    """Жоспарда бет нөмірі көрсетілмеген жағдайда, берілген тақырыпты кітаптың
    өз мазмұны бойынша іздеп, қай беттерде орналасқанын табады. Кітаптың
    толық PDF-ін жібереді (алдын ала бет-бетімен оқудың қажеті жоқ)."""
    prompt = FIND_TOPIC_PROMPT.format(topic=topic_text or "белгісіз")
    book_block = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(book_pdf_bytes).decode("ascii"),
        },
    }
    content_blocks = [book_block, {"type": "text", "text": prompt}]
    context = f"find_topic_pages {context_note}".strip()
    result = _call_claude_with_key(content_blocks, api_key, thinking_disabled=True, expect=dict, context=context)
    return result.get("page_start"), result.get("page_end")


def check_batch(material_kind, material_content, book_segment_text, page_start, page_end, criteria, api_key):
    prompt = BATCH_PROMPT.format(
        criteria=criteria, page_start=page_start, page_end=page_end,
        book_segment=book_segment_text, schema=FINDING_SCHEMA_HINT,
    )
    content_blocks = [_material_content_block(material_kind, material_content), {"type": "text", "text": prompt}]
    return _call_claude_with_key(content_blocks, api_key, thinking_disabled=True, context="check_batch")


TARGETED_PROMPT = """Сен білім беру материалдарын тексеретін сарапшысың.

{criteria}

Тексерілетін материал мен жоспардың осы аптасына («{topic}») қатысты барлық
тақырыптардың эталон кітап(тар)дан табылған тиісті беттері төменде құжат
түрінде тіркелген. Әр тіркелген құжат қай кітаптың, қай тақырыптың, қай
беттері екені көрсетілген:
{books_note}
Материалдағы әр сұрақ/тапсырманы қатысты тақырыпқа сай құжатпен салыстырып
тексер. Бірнеше тақырып/кітап тіркелген болса, әрқайсысын жеке ескеріп, сол
тақырыпқа/кітапқа сай нұсқа/жауапты сол құжатпен салыстыр.

Жауапты ТЕК осы JSON схемасына сәйкес таза JSON тізім түрінде қайтар —
түсіндірме, markdown белгісі (```), қосымша мәтін қоспа. Қате табылмаса, бос
тізім [] қайтар.

JSON схемасы:
{schema}
"""


def check_targeted(material_kind, material_content, book_pdf_items, topic, criteria, api_key):
    """Толық кітапты алдын ала оқымай-ақ, тақырыпқа сай табылған бет ауқымын
    кітап(тар)дың Drive сілтемесінен сол сәтте жүктеп алып, материалмен бір-ақ
    сұраныста салыстырады. book_pdf_items: [(кітап атауы, page_start, page_end,
    PDF байттары), ...] — бір немесе бірнеше эталон кітап (әрқайсысының өз бет
    ауқымы болуы мүмкін, өйткені баспалар әртүрлі беттерге орналастырады),
    барлығы бір біріктірілген нәтижеге салыстырылады."""
    books_note = "\n".join(
        f"- {title}: {page_start}-{page_end} беттер" for title, page_start, page_end, _pdf in book_pdf_items
    )
    prompt = TARGETED_PROMPT.format(
        criteria=criteria, topic=topic or "белгісіз", schema=FINDING_SCHEMA_HINT, books_note=books_note,
    )
    book_blocks = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
            },
        }
        for _title, _page_start, _page_end, pdf_bytes in book_pdf_items
    ]
    content_blocks = [_material_content_block(material_kind, material_content), *book_blocks, {"type": "text", "text": prompt}]
    return _call_claude_with_key(content_blocks, api_key, thinking_disabled=True, context="check_targeted")


def final_review(findings, criteria, api_key):
    if not findings:
        return []
    prompt = FINAL_REVIEW_PROMPT.format(
        criteria=criteria, findings=json.dumps(findings, ensure_ascii=False, indent=2), schema=FINDING_SCHEMA_HINT,
    )
    content_blocks = [{"type": "text", "text": prompt}]
    return _call_claude_with_key(content_blocks, api_key, thinking_disabled=False, context="final_review")


REPORT_SCHEMA_HINT = """{
  "checked_document_summary": "(тексерілген құжаттың қысқаша сипаттамасы: түрі, жалпы сұрақ/тапсырма саны)",
  "sources": [
    {"title": "(эталон кітаптың атауы)", "detail": "(неше сұрақ осы кітапқа сілтейді, қай беттер қамтылды)"}
  ],
  "methodology": "(тексеру әдіснамасы, 1-2 сөйлем)",
  "overall_conclusion": "(жалпы қорытынды абзац — неше сұрақ дұрыс/қатесіз, пайызбен, негізгі тұжырым)",
  "category_summary": [
    {"category": "(санат атауы)", "count": 0, "severity": "(Жоғары/Орташа/Төмен-орташа/Төмен/—)"}
  ],
  "issue_groups": [
    {
      "group_title": "(санат тақырыбы, мыс. 'Ақпараттың сәйкес келмеуі — дереккөз/бет сілтемесі қате')",
      "items": [
        {
          "label": "(мыс. 'Сұрақ 32')",
          "text_ref": "(сұрақтың/жауаптың қысқаша дәйексөзі немесе сипаттамасы)",
          "problem": "(мәселенің егжей-тегжейлі сипаттамасы, кітаптағы дәл орнын көрсетіп)",
          "suggestion": "(нақты түзету ұсынысы)"
        }
      ]
    }
  ],
  "positive_notes": ["(тексеруде расталған оң тұстар, әрқайсысы жеке жол)"],
  "final_recommendations": ["(қорытынды, іс-әрекетке негізделген ұсыныстар тізімі)"]
}"""

COMPILE_REPORT_PROMPT = """Сен білім беру материалдарын тексеретін сарапшысың. Материалды кітап(тар)пен
салыстыру толық аяқталды. Төменде: тексеру критерийлері, тексерілген материалдың
өзі, және тексеру барысында жиналған қателер тізімі берілген.

{criteria}

Осы деректер негізінде толық, кәсіби есеп құрастыр — куратор/әдіскер оқитын,
нақты дәйексөздер мен бет нөмірлерін келтіретін, әр қатені санатқа бөліп
топтастыратын есеп. Материалдың өзін оқып, жалпы сұрақ/тапсырма санын өзің
есепте (findings тізіміндегі сан емес — тек ҚАТЕЛЕР саны, ал есепте жалпы
санды да көрсету керек). category_summary-де ТЕК осы тексеруге қатысты
санаттарды ғана көрсет (мыс. егер аппеляциялық қате табылмаса, "0" деп жаз,
бірақ санатты алып тастама — критерийде көрсетілген санаттардың барлығын
қамту керек). issue_groups-те әр қатені өз санатына топтастыр, әр топтың
ішінде items тізімінде әр жеке қатені жеке жазба ретінде бер (findings
тізіміндегідей "Қате жоқ" жазбаларды қоспа).

Жиналған қателер тізімі:
{findings}

Жауапты ТЕК осы JSON схемасына сәйкес таза JSON объект түрінде қайтар —
түсіндірме, markdown белгісі (```), қосымша мәтін қоспа.

JSON схемасы:
{schema}
"""


def compile_report(material_kind, material_content, findings, criteria, api_key):
    """Тексеру аяқталған соң, материалдың өзін қайта оқып, жиналған
    қателер тізімін толық, құрылымды есепке (JSON объект) айналдырады —
    PDF ретінде экспорттауға дайын."""
    prompt = COMPILE_REPORT_PROMPT.format(
        criteria=criteria, findings=json.dumps(findings, ensure_ascii=False, indent=2), schema=REPORT_SCHEMA_HINT,
    )
    content_blocks = [_material_content_block(material_kind, material_content), {"type": "text", "text": prompt}]
    return _call_claude_with_key(content_blocks, api_key, thinking_disabled=False, expect=dict, context="compile_report")
