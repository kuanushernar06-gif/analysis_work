"""Google Drive-та жатқан кітап PDF-ін жүктеп алып, Claude арқылы бет-бетімен
(10 беттен) толық оқып, әр бөліктің мазмұнын мәтін түрінде сақтау.

Неге бөлікпен: бір сұраныста бүкіл кітапты (жүздеген бет) жіберу Claude
API-дің құжат шегінен де (32МБ/600 бет), Vercel-дің бір сұраныс уақыты
шегінен де асып кетуі мүмкін. Сол себепті кітап "оқылымы" бірнеше қысқа
сұраныс арқылы бірте-бірте (әр сұраныста бір ғана 10 беттік бөлік)
жүргізіледі — веб-беттегі JS осы модульдің бір "қадамын" қайта-қайта
шақырып, барлық бөлік дайын болғанша сұрай береді.
"""

import base64
import json
import re
import time
import urllib.error
import urllib.request

from pypdf import PdfReader, PdfWriter

from netfetch import SSL_CONTEXT, USER_AGENT

CLAUDE_MODEL = "claude-sonnet-5"
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
REQUEST_TIMEOUT = 180
MAX_COMPLETION_TOKENS = 8_000
MAX_RETRIES = 4
DEFAULT_RETRY_SECONDS = 20

# 10 беттік бөлікті толық оқу шамамен 2.5-3 минут алды (тексерілді), 2 беттік
# бөлік ~49 секунд алды — бұл Vercel-дің бір сұраныс уақыты шегіне (maxDuration,
# vercel.json-да 60с) тым жақын, желі/сервер баяулауында оңай асып кетуі
# мүмкін. Сол себепті қауіпсіз қор үшін бөлікті одан әрі кішірейттік.
PAGES_PER_CHUNK = 1

DRIVE_ID_RE_LIST = [
    re.compile(r"/file/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"[?&]id=([a-zA-Z0-9_-]+)"),
    re.compile(r"/document/d/([a-zA-Z0-9_-]+)"),
]

READ_CHUNK_PROMPT = """Сен кітаптың PDF файлының бір бөлігін (беттер: {page_start}-{page_end})
мұқият, әр сөзін өткізбей оқып, толық мазмұнын мәтін түрінде қайта жаз.

Бұл мәтін кейін осы кітапты негізге алып басқа тапсырмаларды тексеру үшін
пайдаланылады, сондықтан:
- Әр беттің мазмұнын мүмкіндігінше толық және дәл жаз (тақырып атаулары,
  анықтамалар, деректер, сандар, күндер, есімдер).
- Егер бетте сұрақ-жауап, тест тапсырмасы немесе жаттығу болса, сұрақтың
  толық мәтінін және дұрыс жауабын (немесе жауап кілтін) АҚПАРАТ
  ЖОҒАЛТПАЙ дәл сол күйінде келтір.
- Сурет, кесте, диаграмма болса, олардың мазмұнын қысқаша сөзбен сипатта.
- Ойдан ештеңе қоспа, тек PDF-те бар нәрсені жаз.

Жауапты тек оқылған мазмұнның өзі етіп қайтар — кіріспе сөз, түсіндірме
немесе "мына бетте..." деген сияқты мета-сөйлемдер жазба."""


class BookIngestError(Exception):
    pass


def extract_drive_file_id(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise BookIngestError("Сілтеме бос болмауы керек.")
    for pattern in DRIVE_ID_RE_LIST:
        m = pattern.search(url)
        if m:
            return m.group(1)
    raise BookIngestError(
        "Google Drive сілтемесінен файл ID-ін тани алмадым. "
        "Сілтеме '.../file/d/ID/view' немесе '...?id=ID' түрінде болу керек."
    )


def _http_get(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def download_drive_file(link: str) -> bytes:
    """Drive-тың ашық (public) сілтемесінен файл байттарын жүктейді. Үлкен
    файлдарда Drive вирус тексерусіз жүктеу туралы ескерту беті (HTML)
    қайтарады — сол жағдайда бетте жасырылған 'confirm' токенін тауып,
    сұранысты қайта жібереді."""
    file_id = extract_drive_file_id(link)
    url = f"https://drive.google.com/uc?export=download&id={file_id}"

    try:
        with _http_get(url) as resp:
            data = resp.read()
            content_type = resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise BookIngestError(
                "Файлға қол жеткізе алмадым (рұқсат жоқ). Google Drive-та "
                "'Ортаққа бөлу → Сілтемесі бар кез келген адам → Көруші' "
                "етіп қойыңыз."
            ) from e
        if e.code == 404:
            raise BookIngestError("Файл табылмады. Сілтемені тексеріңіз.") from e
        raise BookIngestError(f"Файлды жүктеу сәтсіз аяқталды (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise BookIngestError(f"Файлды жүктеу сәтсіз аяқталды: {e.reason}") from e

    if content_type.startswith("text/html") or data[:15].lstrip().startswith(b"<!DOCTYPE"):
        html = data.decode("utf-8", errors="replace")
        token_match = re.search(r'name="confirm"\s+value="([^"]+)"', html) or re.search(
            r"confirm=([0-9A-Za-z_-]+)", html
        )
        if not token_match:
            raise BookIngestError(
                "Drive файлды тікелей бере алмады (тым үлкен немесе рұқсат "
                "жабық болуы мүмкін). Сілтеменің ашықтығын тексеріңіз."
            )
        confirm_token = token_match.group(1)
        confirm_url = (
            f"https://drive.google.com/uc?export=download&confirm={confirm_token}&id={file_id}"
        )
        try:
            with _http_get(confirm_url, timeout=120) as resp:
                data = resp.read()
        except urllib.error.URLError as e:
            raise BookIngestError(f"Файлды жүктеу сәтсіз аяқталды: {e}") from e

    if not data:
        raise BookIngestError("Жүктелген файл бос болып шықты.")
    return data


def get_pdf_page_count(pdf_bytes: bytes) -> int:
    try:
        reader = PdfReader(__import__("io").BytesIO(pdf_bytes))
        return len(reader.pages)
    except Exception as e:
        raise BookIngestError(f"PDF файлын оқи алмадым: {e}") from e


def extract_chunk_pdf_bytes(pdf_bytes: bytes, page_start: int, page_end: int) -> bytes:
    """1-негізді (page_start/page_end инклюзивті) бет аралығын жаңа шағын
    PDF файлы ретінде қайтарады."""
    import io

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for i in range(page_start - 1, min(page_end, len(reader.pages))):
        writer.add_page(reader.pages[i])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# Бет саны (MAX_TOPIC_PAGE_SPAN) шектелсе де, жоғары ажыратымдылықпен
# сканерленген оқулықтарда 12 бет өзі ондаған МБ тартуы мүмкін — Claude
# API-дің жалпы сұраныс шегінен (32МБ) оңай асып кетеді. Сол себепті бет
# санымен қатар нақты байт көлемін де шектейміз.
MAX_BOOK_CHUNK_BYTES = 9_000_000


def rasterize_pdf_compressed(pdf_bytes: bytes, dpi: int = 150, quality: int = 55) -> bytes:
    """Әр бетті сурет ретінде қайта РЕНДЕРЛЕП (PDF-тің ішкі құрылымына —
    XObject суреттер ме, inline суреттер ме, векторлық графика ма, ауыр
    қаріп пе — мүлдем қарамай), содан кейін сол суреттерден жаңа, ықшам
    PDF құрастырады. compress_pdf_images-тің орнын басады: ол тек
    XObject суреттерді ғана көретін (беттің салмағы inline суретте немесе
    басқа объектіде жатса көмектеспей қалатын), ал бұл — беттің нақты
    КӨРІНІСІН түсіріп алатындықтан, себебіне қарамай әрқашан жұмыс
    істейді. PyMuPDF (fitz) арқылы рендерленеді."""
    import io
    import fitz
    from reportlab.pdfgen import canvas as rl_canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = io.BytesIO()
    c = None
    try:
        for page in doc:
            rect = page.rect
            pix = page.get_pixmap(dpi=dpi)
            img = Image.frombytes(
                "RGB" if pix.n < 4 else "RGBA", (pix.width, pix.height), pix.samples
            )
            if img.mode != "RGB":
                img = img.convert("RGB")
            img_buf = io.BytesIO()
            img.save(img_buf, format="JPEG", quality=quality)
            img_buf.seek(0)
            if c is None:
                c = rl_canvas.Canvas(out, pagesize=(rect.width, rect.height))
            else:
                c.setPageSize((rect.width, rect.height))
            c.drawImage(ImageReader(img_buf), 0, 0, width=rect.width, height=rect.height)
            c.showPage()
    finally:
        doc.close()
    if c is not None:
        c.save()
    return out.getvalue()


def extract_chunk_pdf_bytes_capped(pdf_bytes: bytes, page_start: int, page_end: int, max_bytes: int = MAX_BOOK_CHUNK_BYTES):
    """extract_chunk_pdf_bytes сияқты, бірақ нәтиже max_bytes-тан үлкен болса,
    сыйғанша соңғы беттерден бастап аралықты қысқартады (сканерленген
    беттер ауыр болғанда). Тіпті 1 бетке дейін қысқартып та әлі сыймаса,
    соңғы шара ретінде беттерді сатылап (алдымен жұмсақ, сыймаса —
    қаттырақ) РЕНДЕРЛЕП қайта сығады (rasterize_pdf_compressed) — бет
    санын одан әрі кемітудің орнына. (chunk_bytes, нақты_page_start,
    нақты_page_end, compress_note) төрттігін қайтарады — compress_note
    бос жол ('') болса, сығу қолданылмаған, әйтпесе не болғанын
    сипаттайды (диагностика үшін, сығу көмектеспеген жағдайда себебін
    бірден көру үшін)."""
    chunk = extract_chunk_pdf_bytes(pdf_bytes, page_start, page_end)
    end = page_end
    while len(chunk) > max_bytes and end > page_start:
        end -= 1
        chunk = extract_chunk_pdf_bytes(pdf_bytes, page_start, end)

    compress_note = ""
    if len(chunk) > max_bytes:
        before = len(chunk)
        for dpi, quality in ((150, 55), (110, 40), (85, 28)):
            try:
                rasterized = rasterize_pdf_compressed(chunk, dpi=dpi, quality=quality)
            except Exception as e:
                compress_note = f"rasterize {dpi}dpi/{quality}q failed: {type(e).__name__}: {e}"
                break
            if not rasterized:
                compress_note = f"rasterize {dpi}dpi/{quality}q produced empty output"
                break
            chunk = rasterized
            compress_note = f"rasterized @ {dpi}dpi/{quality}q: {before}b -> {len(chunk)}b"
            if len(chunk) <= max_bytes:
                break

    return chunk, page_start, end, compress_note


def _extract_retry_delay(headers):
    value = headers.get("retry-after") if headers else None
    if value:
        try:
            return float(value)
        except ValueError:
            return None
    return None


def read_chunk_with_claude(chunk_pdf_bytes: bytes, page_start: int, page_end: int, api_key: str) -> str:
    b64 = base64.standard_b64encode(chunk_pdf_bytes).decode("ascii")
    payload = json.dumps(
        {
            "model": CLAUDE_MODEL,
            "max_tokens": MAX_COMPLETION_TOKENS,
            # Мұнда терең пайымдау емес, дәл транскрипция керек — "ойлау"
            # режимі әдепкі бойынша қосулы болғандықтан, оны өшірмесек әр
            # бөлік бірнеше есе баяу оқылады (тексерілді: ~3 минут/бөлік).
            "thinking": {"type": "disabled"},
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": READ_CHUNK_PROMPT.format(page_start=page_start, page_end=page_end),
                        },
                    ],
                }
            ],
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        CLAUDE_API_URL,
        data=payload,
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
            raise BookIngestError(f"Claude API қатесі (HTTP {e.code}): {detail[:300]}") from e
        except urllib.error.URLError as e:
            raise BookIngestError(f"Claude API-ге қосыла алмадым: {e.reason}") from e
    else:
        raise BookIngestError("Claude API-мен көп қайталаудан кейін де байланыса алмадым.")

    if body.get("stop_reason") == "refusal":
        raise BookIngestError("Claude бұл бетті оқудан қауіпсіздік саясаты бойынша бас тартты.")

    try:
        text = next(b["text"] for b in body["content"] if b.get("type") == "text")
    except (KeyError, StopIteration, TypeError) as e:
        raise BookIngestError("Claude API жауабының форматы күтпеген болды.") from e

    return text.strip()
