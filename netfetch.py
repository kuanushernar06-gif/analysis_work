"""Google-ден CSV/XLSX/DOC экспорттарын жүктеу үшін ортақ HTTPS ашушы.

python.org-нан орнатылған Python-да macOS-та түбірлік сертификаттар болмайды,
сондықтан ssl.create_default_context() қалыпты жүйелік дүкенді таппай
CERTIFICATE_VERIFY_FAILED қатесін шығарады. certifi дестесінің өз сертификат
бумасын қолдану бұл мәселені жүйе баптауына тәуелсіз шешеді.
"""

import ssl
import urllib.request

import certifi

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "Mozilla/5.0 (compatible; JUZ40-analytics/1.0)"


def urlopen(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)
