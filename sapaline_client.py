"""Sapaline (Live сабақ ұнау пайызы) API клиенті — Supabase REST (PostgREST)
арқылы жұмыс істейді. Juz40 клиентімен бірдей паттерн: логин токенін
кэштейді, желі қатесінде бірнеше рет қайталап көреді."""
import json
import os
import threading
import time
import urllib.error
import urllib.request

from netfetch import SSL_CONTEXT, USER_AGENT

SAPALINE_URL = "https://vmmmtujfuxrfzsbgoxow.supabase.co"
# Бұл — жобаның ашық (anon/public) кілті, құпия емес: сайттың өз JS
# бумасында да ашық тұрады, тек кіру (auth) арқылы алынған Bearer токен
# ғана нақты рұқсатты анықтайды.
SAPALINE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZtbW10"
    "dWpmdXhyZnpzYmdveG93Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzQ4NTkzOTAsImV4cCI6MjA5"
    "MDQzNTM5MH0.5ZpzPCyppXTKWc1vY5yrselmdYtmdHQqPNfnfBiaKDo"
)

_MAX_RETRIES = 3
_RETRY_BACKOFF_SECONDS = 2.0
_TOKEN_CACHE_SECONDS = 3000  # ~50 минут (Supabase токені әдетте 1 сағатқа жарамды)

_token_cache = {"token": None, "expires_at": 0.0}
_token_lock = threading.Lock()


class SapalineError(Exception):
    pass


def _get_credentials():
    email = os.environ.get("SAPALINE_EMAIL")
    password = os.environ.get("SAPALINE_PASSWORD")
    if not email or not password:
        raise SapalineError(
            "SAPALINE_EMAIL / SAPALINE_PASSWORD ортам айнымалылары орнатылмаған "
            "(.env файлына қосыңыз)."
        )
    return email, password


def _request(path, method="GET", body=None, token=None):
    url = f"{SAPALINE_URL}{path}"
    headers = {
        "apikey": SAPALINE_ANON_KEY, "Content-Type": "application/json", "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    last_error = None
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < _MAX_RETRIES - 1:
                last_error = e
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            body_text = e.read().decode("utf-8", errors="replace")
            raise SapalineError(f"Sapaline API қатесі (HTTP {e.code}): {body_text}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < _MAX_RETRIES - 1:
                last_error = e
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise SapalineError(f"Sapaline API-ге қосылу сәтсіз аяқталды: {e}") from e
    raise SapalineError(f"Sapaline API қатесі: {last_error}")


def _login_fresh():
    email, password = _get_credentials()
    body = _request(
        "/auth/v1/token?grant_type=password", method="POST",
        body={"email": email, "password": password},
    )
    token = body.get("access_token")
    if not token:
        raise SapalineError("Sapaline логин жауабында access_token табылмады.")
    return token


def login(force=False):
    """Sapaline-ге кіріп, Bearer токен қайтарады — кэштелген токен әлі
    жарамды болса, қайта логин болмайды (Juz40 клиентіндегі token-кэштеу
    паттерні секілді)."""
    with _token_lock:
        if not force and _token_cache["token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["token"]
        token = _login_fresh()
        _token_cache["token"] = token
        _token_cache["expires_at"] = time.time() + _TOKEN_CACHE_SECONDS
        return token


def get_periods(token, division="smart"):
    """live_sabaq_periods кестесінен период тізімін қайтарады:
    [{"id":.., "division":.., "month":.., "week":..}, ...]."""
    return _request(
        f"/rest/v1/live_sabaq_periods?select=id,division,month,week&division=eq.{division}",
        token=token,
    )


def get_live_sabaq_rows(token, division, subject, period_id):
    """Берілген период үшін барлық Live сабақ жолын қайтарады:
    [{"teacher":.., "stream":.., "lesson_date":.., "time_from":..,
    "like_pct":.., "search_status":.., "survey":..}, ...]. 'survey' —
    {"jalpy": жалпы жауап берген сан, "qatysty": қатысқандар,
    "sebepti"/"sebepsiz": себепті/себепсіз қатыспағандар} — jalpy әрдайым
    qatysty+sebepti+sebepsiz-ге тең, сондықтан қатысым % осыдан есептеледі."""
    path = (
        "/rest/v1/live_sabaq_rows?"
        "select=teacher,stream,lesson_date,time_from,like_pct,search_status,survey"
        f"&division=eq.{division}&subject=eq.{subject}&period_id=eq.{period_id}"
        "&order=lesson_date.asc&limit=1000"
    )
    return _request(path, token=token)
