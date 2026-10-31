"""Настройки приложения (переменные окружения / файл .env).

Облако — GigaChat (Сбер). Ключ авторизации можно задать строкой Base64 из личного кабинета
(`GIGACHAT_CREDENTIALS`) или парой Client ID / Client Secret (`GIGACHAT_CLIENT_ID` + `GIGACHAT_CLIENT_SECRET`).
"""
import base64
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


def _clean(v: str, names=()) -> str:
    """Убирает кавычки, пробелы и случайный префикс «GIGACHAT_CREDENTIALS=» из значения.

    В отличие от старой версии, «=» внутри значения (padding Base64) сохраняется: ключ GigaChat
    часто заканчивается на «=», и обрезать его нельзя.
    """
    v = (v or "").strip().strip("\"'").strip()
    for name in names:
        if v.upper().startswith(name.upper() + "="):
            v = v.split("=", 1)[1].strip().strip("\"'")
            break
    return "".join(v.split())


def _credentials() -> str:
    raw = _clean(os.getenv("GIGACHAT_CREDENTIALS") or os.getenv("GIGACHAT_AUTH_KEY") or os.getenv("GIGACHAT_API_KEY") or "",
                 names=("GIGACHAT_CREDENTIALS", "GIGACHAT_AUTH_KEY", "GIGACHAT_API_KEY"))
    if raw:
        return raw
    client_id = _clean(os.getenv("GIGACHAT_CLIENT_ID", ""), names=("GIGACHAT_CLIENT_ID",))
    client_secret = _clean(os.getenv("GIGACHAT_CLIENT_SECRET", ""), names=("GIGACHAT_CLIENT_SECRET",))
    if client_id and client_secret:
        return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return ""


# --- облако (GigaChat) ---
GIGACHAT_CREDENTIALS = _credentials()          # ключ авторизации: Base64 (client_id:client_secret)
GIGACHAT_BASE_URL = os.getenv("GIGACHAT_BASE_URL", "https://api.giga.chat/v1").strip().rstrip("/")
GIGACHAT_AUTH_URL = os.getenv("GIGACHAT_AUTH_URL", "https://ngw.devices.sberbank.ru:9443/api/v2/oauth").strip()

GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS").strip()      # GIGACHAT_API_PERS | _B2B | _CORP
GIGACHAT_SCOPES = [s.strip() for s in os.getenv(
    "GIGACHAT_SCOPES", "GIGACHAT_API_PERS,GIGACHAT_API_CORP,GIGACHAT_API_B2B").split(",") if s.strip()]

GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max").strip()
# Если основная модель недоступна/перегружена (429/503/404), пробуем следующие: у каждой свой лимит
GIGACHAT_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GIGACHAT_FALLBACK_MODELS", "GigaChat-2-Pro,GigaChat-2").split(",") if m.strip()]
GIGACHAT_AUDIO_MODEL = os.getenv("GIGACHAT_AUDIO_MODEL", "").strip()           # пусто — та же модель (2 Max умеет аудио)
GIGACHAT_EMBED_MODEL = os.getenv("GIGACHAT_EMBED_MODEL", "Embeddings").strip()  # Embeddings | Embeddings-2 | EmbeddingsGigaR
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))
EMBED_BATCH = int(os.getenv("EMBED_BATCH", "64"))

# Токен доступа GigaChat действует 30 минут. Обновляем заранее, за TOKEN_SKEW_S секунд до истечения,
# и сразу при ответе 401. Фоновое продление не даёт первому запросу пользователя платить за OAuth.
TOKEN_SKEW_S = float(os.getenv("GIGACHAT_TOKEN_SKEW_S", "60"))
TOKEN_KEEPALIVE_S = float(os.getenv("GIGACHAT_TOKEN_KEEPALIVE_S", "120"))
TOKEN_KEEPALIVE = _flag("GIGACHAT_TOKEN_KEEPALIVE", "1")
AUTH_TIMEOUT_S = float(os.getenv("GIGACHAT_AUTH_TIMEOUT_S", "10"))

CLOUD_TIMEOUT_S = float(os.getenv("CLOUD_TIMEOUT_S", "15"))    # дольше ждать ответ модели не имеет смысла
AUDIO_TIMEOUT_S = float(os.getenv("GIGACHAT_AUDIO_TIMEOUT_S", "30"))
MAX_RESPONSE_TOKENS = int(os.getenv("GIGACHAT_MAX_TOKENS", "800"))   # JSON-ответ короткий; ограничение ускоряет генерацию
GIGACHAT_JSON_SCHEMA = _flag("GIGACHAT_JSON_SCHEMA", "1")      # строгая JSON-схема ответа (при отказе — обычный режим)

# TLS: GigaChat использует цепочку НУЦ Минцифры. В Docker сертификаты ставятся в системное хранилище.
GIGACHAT_TLS_VERIFY = _flag("GIGACHAT_TLS_VERIFY", "1")
GIGACHAT_CA_BUNDLE = os.getenv("GIGACHAT_CA_BUNDLE", "").strip()   # путь к .pem с сертификатами Минцифры (если нет системных)

# Порог похожести для семантического поиска на эмбеддингах GigaChat (можно подстроить без правки кода)
SEARCH_FLOOR_CLOUD = float(os.getenv("SEARCH_FLOOR_GIGACHAT", "0.65"))

# --- локальный режим (без облака) ---
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")           # tiny / base / small / medium
LOCAL_EMBED_MODEL = os.getenv("LOCAL_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
MODELS_DIR = Path(os.getenv("MODELS_DIR", ROOT_DIR / "models"))   # сюда один раз скачиваются локальные модели
PRELOAD_LOCAL = _flag("PRELOAD_LOCAL", "1")                  # грузить локальные модели в фоне при старте

# --- данные и сервер ---
# Amvera монтирует постоянное хранилище в /data (см. amvera.yml) — файл записей переживает перезапуск.
_DATA_DEFAULT = "/data/assistant_data.csv" if Path("/data").is_dir() else str(ROOT_DIR / "assistant_data.csv")
DATA_FILE = Path(os.getenv("DATA_FILE", _DATA_DEFAULT))
DEFAULT_TZ = os.getenv("TZ_NAME", "Europe/Moscow")
MULTI_USER = _flag("MULTI_USER", "0")     # 1 — у каждого браузера свои записи (для публичного сайта)
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

MAX_AUDIO_BYTES = 8 * 1024 * 1024
HISTORY_TURNS = 6
MAX_SESSIONS = 500

# Старые настройки Gemini: если остались в окружении, подскажем в логе, что они больше не используются.
LEGACY_GEMINI_KEY = bool(os.getenv("GEMINI_API_KEY", "").strip())
