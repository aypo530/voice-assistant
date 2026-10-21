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
def _clean_key(v: str) -> str:
    """Убирает типичные ошибки вставки: кавычки, пробелы, перевод строки, префикс «GEMINI_API_KEY=»."""
    v = v.strip().strip("\"'").strip()
    if "=" in v:
        v = v.split("=", 1)[1].strip().strip("\"'")
    return "".join(v.split())


GEMINI_API_KEY = _clean_key(os.getenv("GEMINI_API_KEY", ""))
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest").strip()      # «-latest» — псевдоним, не устаревает
# Если основная модель перегружена или исчерпан её лимит (429/503), пробуем эти (у каждой модели свой лимит)
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-flash-lite-latest,gemini-2.5-flash").split(",") if m.strip()]
GEMINI_VERTEX_MODEL = os.getenv("GEMINI_VERTEX_MODEL", "gemini-2.5-flash").strip()     # для ключей «AQ.…»
GEMINI_VERTEX_FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_VERTEX_FALLBACK_MODELS", "gemini-2.5-flash-lite").split(",") if m.strip()]
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
