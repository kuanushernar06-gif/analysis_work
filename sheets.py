import csv
import io
import re
import urllib.request
import urllib.error

SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
GID_RE = re.compile(r"[?&#]gid=(\d+)")


class SheetFetchError(Exception):
    pass


def normalize_sheet_url(raw_url: str) -> str:
    """Google Sheets-тің кез келген сілтемесін (edit, pubhtml, тікелей csv)
    CSV экспорт сілтемесіне айналдырады."""
    raw_url = raw_url.strip()
    if not raw_url:
        raise SheetFetchError("Сілтеме бос болмауы керек.")

    if "output=csv" in raw_url or "format=csv" in raw_url:
        return raw_url

    match = SHEET_ID_RE.search(raw_url)
    if not match:
        raise SheetFetchError(
            "Google Sheets сілтемесін тани алмадым. "
            "Сілтеме '/spreadsheets/d/...' түрінде болу керек."
        )
    sheet_id = match.group(1)
    gid_match = GID_RE.search(raw_url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def fetch_csv_rows(raw_url: str):
    """Сілтемеден CSV жүктеп, тізбектелген тізім (list of lists) етіп қайтарады."""
    csv_url = normalize_sheet_url(raw_url)
    req = urllib.request.Request(
        csv_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; JUZ40-analytics/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SheetFetchError(
                "Кестеге қол жеткізе алмадым (рұқсат жоқ). Google Sheets-те "
                "'Файл → Ортаққа бөлу → Интернетке жариялау' "
                "арқылы кестені жалпыға ашық етіп қойыңыз."
            ) from e
        raise SheetFetchError(f"Кестені жүктеу сәтсіз аяқталды (HTTP {e.code}).") from e
    except urllib.error.URLError as e:
        raise SheetFetchError(f"Кестені жүктеу сәтсіз аяқталды: {e.reason}") from e

    text = raw_bytes.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader]
    if not rows:
        raise SheetFetchError("Кесте бос болып тұр.")
    return rows


def rows_to_dicts(rows):
    header = [h.strip() for h in rows[0]]
    body = rows[1:]
    return header, body
