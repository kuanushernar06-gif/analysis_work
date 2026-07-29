"""Google-ден CSV/XLSX/DOC экспорттарын жүктеу үшін ортақ HTTPS ашушы.

python.org-нан орнатылған Python-да macOS-та түбірлік сертификаттар болмайды,
сондықтан ssl.create_default_context() қалыпты жүйелік дүкенді таппай
CERTIFICATE_VERIFY_FAILED қатесін шығарады. certifi дестесінің өз сертификат
бумасын қолдану бұл мәселені жүйе баптауына тәуелсіз шешеді.
"""

import ssl
import urllib.parse
import urllib.request

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "Mozilla/5.0 (compatible; JUZ40-analytics/1.0)"


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Google-дің экспорт қайта бағыттауларында кейде құжат атауы (кириллица)
    сілтемеге дұрыс проценттік кодтаусыз қосылып келеді — Python-дың
    http.client мұндай сілтемені ASCII ретінде оқуға тырысып
    UnicodeEncodeError шығарады. Қайта бағыттау сілтемесін қайта қолданбас
    бұрын қайта кодтап, осы қатенің алдын аламыз."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url = urllib.parse.quote(newurl, safe=":/?&=%.,+~#@!$'()*;")
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=SSL_CONTEXT), _SafeRedirectHandler
)


def urlopen(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return _OPENER.open(req, timeout=timeout)
