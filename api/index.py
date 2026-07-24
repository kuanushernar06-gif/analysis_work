import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import app  # noqa: E402

# Vercel-дің @vercel/python тұтқасы осы модульдегі "app" WSGI объектісін іздейді.
