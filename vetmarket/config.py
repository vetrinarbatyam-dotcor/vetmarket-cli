"""Centralized config + paths."""
from __future__ import annotations
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **k): pass

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "vetmarket.db"
SESSION_PATH = DATA_DIR / "session.json"
HTML_CACHE = DATA_DIR / "html"
HTML_CACHE.mkdir(exist_ok=True)

SECRETS_FILE = Path.home() / ".clinic-secrets" / "vetmarket.env"
load_dotenv(SECRETS_FILE)

USERNAME = os.getenv("VETMARKET_USERNAME", "")
PASSWORD = os.getenv("VETMARKET_PASSWORD", "")
BASE_URL = os.getenv("VETMARKET_BASE_URL", "https://www.vetmarket.co.il").rstrip("/")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
