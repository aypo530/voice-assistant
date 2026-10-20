"""Настройки приложения (переменные окружения / файл .env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent
RESOURCE_DIR = APP_DIR

load_dotenv(ROOT_DIR / ".env")      # .env в корне проекта приоритетнее
load_dotenv(APP_DIR / ".env")


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# --- облако (Gemini) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
GEMINI_EMBED_MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip()
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
CLOUD_TIMEOUT_S = float(os.getenv("CLOUD_TIMEOUT_S", "15"))   # дольше ждать Gemini не имеет смысла: дальше — локальный режим

# --- локальный режим (без интернета) ---
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")           # tiny / base / small / medium
LOCAL_EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
MODELS_DIR = Path(os.getenv("MODELS_DIR", ROOT_DIR / "models"))   # сюда один раз скачиваются локальные модели
PRELOAD_LOCAL = _flag("PRELOAD_LOCAL", "1")                  # грузить локальные модели в фоне при старте

# --- данные и сервер ---
DATA_FILE = Path(os.getenv("DATA_FILE", ROOT_DIR / "assistant_data.csv"))
DEFAULT_TZ = os.getenv("TZ_NAME", "Europe/Moscow")
MULTI_USER = _flag("MULTI_USER", "0")     # 1 — у каждого браузера свои записи (для публичного сайта)
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

MAX_AUDIO_BYTES = 8 * 1024 * 1024
HISTORY_TURNS = 6
MAX_SESSIONS = 500
