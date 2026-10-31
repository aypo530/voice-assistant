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


# --- облако (GigaChat, Сбер) ---
def _clean_key(v: str) -> str:
    """Убирает типичные ошибки вставки: кавычки, пробелы, переводы строк, «Basic », «Authorization key:».

    Важно: «=» в конце ключа — это padding Base64, его нельзя отрезать.
    """
    v = (v or "").strip().strip("\"'").strip()
    low = v.lower()
    for prefix in ("authorization key:", "authorization:", "basic ", "ключ авторизации:"):
        if low.startswith(prefix):
            v = v[len(prefix):].strip().strip("\"'").strip()
            low = v.lower()
    if low.startswith("gigachat_auth_key") and "=" in v:      # скопировали строку «GIGACHAT_AUTH_KEY=…»
        v = v.split("=", 1)[1]
    return "".join(v.split())


# Ключ авторизации из личного кабинета GigaChat (он же Base64 от «Client ID:Client Secret»).
GIGACHAT_AUTH_KEY = _clean_key(os.getenv("GIGACHAT_AUTH_KEY", "") or os.getenv("GIGACHAT_CREDENTIALS", ""))
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()          # PERS / B2B / CORP
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1").rstrip("/")
GIGACHAT_OAUTH_URL = os.getenv("GIGACHAT_OAUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth").strip()
# Модели: основная + запасные (если основная недоступна по тарифу или перегружена).
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2").strip()
GIGACHAT_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GIGACHAT_FALLBACK_MODELS", "GigaChat-2-Pro,GigaChat-2-Lite,GigaChat-2-Max,GigaChat").split(",") if m.strip()]
# Модель для распознавания речи из аудио (умеет анализировать аудио: GigaChat 2 Max).
GIGACHAT_AUDIO_MODEL = os.getenv("GIGACHAT_AUDIO_MODEL", "GigaChat-2-Max").strip()
GIGACHAT_EMBED_MODEL = os.getenv("GIGACHAT_EMBED_MODEL", "Embeddings").strip()
GIGACHAT_EMBED_FLOOR = float(os.getenv("GIGACHAT_EMBED_FLOOR", "0.62"))   # порог похожести для поиска по смыслу
# Строгий JSON по схеме (response_format). На части тарифов GigaChat отвечает на него 200 с ПУСТЫМ content,
# поэтому по умолчанию выключено: модель и так возвращает JSON, а разбор его проверяет.
GIGACHAT_STRUCTURED = _flag("GIGACHAT_STRUCTURED", "0")
# Токен доступа живёт 30 минут: обновляем заранее, чтобы запросы не попадали на истёкший токен.
GIGACHAT_TOKEN_REFRESH_S = int(os.getenv("GIGACHAT_TOKEN_REFRESH_S", "300"))   # за сколько секунд до истечения обновлять
GIGACHAT_CA_BUNDLE = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()               # свой CA-бандл (по умолчанию — встроенный)
GIGACHAT_VERIFY_SSL = _flag("GIGACHAT_VERIFY_SSL", "1")                        # 0 — отключить проверку TLS (только для отладки)
CLOUD_TIMEOUT_S = float(os.getenv("CLOUD_TIMEOUT_S", "20"))
# Аудио (загрузка файла + распознавание речи моделью) идёт дольше текстового запроса — отдельный, больший таймаут.
AUDIO_TIMEOUT_S = float(os.getenv("AUDIO_TIMEOUT_S", "90"))   # дольше ждать GigaChat не имеет смысла: дальше — локальный режим

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
