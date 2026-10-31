"""Транспорт GigaChat API: токен доступа, chat/completions, эмбеддинги, файлы (аудио).

Главное про авторизацию: ключ авторизации (Base64 из личного кабинета) меняется на access_token
через POST /api/v2/oauth. Токен живёт 30 минут (`expires_at`), поэтому он кэшируется на ключ и
обновляется:
  • заранее — за TOKEN_SKEW_S секунд до истечения (плюс фоновое продление в llm/server);
  • немедленно — если API ответил 401, после чего запрос повторяется один раз;
  • «single-flight» — параллельные запросы ждут одно обновление, а не плодят десяток OAuth-запросов
    (Сбер ограничивает выдачу токенов 10 запросами в секунду).
"""
import base64
import hashlib
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger("gigachat")


# ---------- ошибки ----------
class GigaChatError(Exception):
    """Ошибка слоя GigaChat. status — HTTP-код, если он был."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class ConfigError(GigaChatError):
    """Ключ не задан или имеет неверный формат (наша ошибка конфигурации, не сети)."""


class AuthError(GigaChatError):
    """Ключ авторизации отклонён или токен не удалось получить."""


class NetworkError(GigaChatError):
    """Нет связи с GigaChat (сеть, TLS, таймаут)."""


class QuotaError(AuthError):
    """Ключ верный, но закончился пакет токенов / оплаченный лимит (HTTP 402)."""


class BusyError(GigaChatError):
    """Перегрузка, лимит запросов (429) или временная ошибка сервера (5xx)."""


class BadRequest(GigaChatError):
    """Некорректный запрос: неизвестная модель, слишком длинный контекст, неподдерживаемый файл."""


# ---------- ключ авторизации ----------
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_PREFIXES = ("GIGACHAT_CREDENTIALS=", "GIGACHAT_AUTH_KEY=", "GIGACHAT_API_KEY=",
             "Authorization: Basic ", "Authorization:Basic ", "Basic ")


def normalize(raw: str) -> str:
    """Приводит ключ к Base64(client_id:client_secret). Принимает готовый Base64 или пару «id:secret»."""
    v = (raw or "").strip().strip("\"'").strip()
    low = v.lower()
    for prefix in _PREFIXES:
        if low.startswith(prefix.lower()):
            v = v[len(prefix):].strip()
            break
    v = "".join(v.split())
    if not v:
        raise ConfigError("Ключ GigaChat не задан.")
    if len(v) > 400:
        raise ConfigError("Ключ GigaChat слишком длинный — скопируйте только значение Authorization key.")
    if ":" in v and not _B64_RE.match(v):
        v = base64.b64encode(v.encode()).decode()
    if not _B64_RE.match(v):
        raise ConfigError("Ключ GigaChat должен быть Base64-строкой Authorization key или парой client_id:client_secret.")
    v += "=" * (-len(v) % 4)
    try:
        decoded = base64.b64decode(v, validate=True)
    except Exception as e:  # noqa: BLE001
        raise ConfigError("Ключ GigaChat не читается как Base64 — проверьте, что скопирован целиком.") from e
    if b":" not in decoded:
        raise ConfigError("В ключе GigaChat нет client_id:client_secret — это не Authorization key.")
    return v


# ---------- HTTP ----------
_client = None
_client_lock = threading.Lock()
_bundle_path: Optional[str] = None


def _combined_bundle(extra: str) -> str:
    """certifi + сертификаты Минцифры в один файл (для запуска на компьютере без системных сертификатов)."""
    global _bundle_path
    if _bundle_path and os.path.exists(_bundle_path):
        return _bundle_path
    try:
        import certifi
        base = open(certifi.where(), "rb").read()
    except Exception:  # noqa: BLE001
        base = b""
    try:
        addition = open(extra, "rb").read()
    except OSError:
        return extra
    data = base + (b"" if base.endswith(b"\n") else b"\n") + addition
    digest = hashlib.sha1(data).hexdigest()[:10]
    target = os.path.join(str(config.MODELS_DIR), f"ca_bundle_{digest}.pem")
    try:
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as f:
            f.write(data)
        _bundle_path = target
        return target
    except OSError:
        return extra


def _verify():
    if not config.GIGACHAT_TLS_VERIFY:
        return False
    extra = config.GIGACHAT_CA_BUNDLE
    if extra and os.path.exists(extra):
        return _combined_bundle(extra)
    return True


def _http_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            import httpx
            _client = httpx.Client(
                verify=_verify(),
                timeout=httpx.Timeout(config.CLOUD_TIMEOUT_S, connect=min(5.0, config.CLOUD_TIMEOUT_S)),
                follow_redirects=True,
                headers={"User-Agent": "voice-assistant/2.0 (GigaChat)", "Accept": "application/json"},
            )
        return _client


def reset_client() -> None:
    """Сбрасывает HTTP-клиент (нужно тестам и смене настроек TLS)."""
    global _client, _bundle_path
    with _client_lock:
        _client = None
        _bundle_path = None


def _request(method: str, url: str, *, headers=None, params=None, json=None, data=None, files=None,
             timeout: Optional[float] = None):
    """Единственная точка выхода в сеть. Тесты подменяют её, чтобы не ходить в интернет."""
    import httpx
    client = _http_client()
    try:
        return client.request(method, url, headers=headers, params=params, json=json, data=data, files=files,
                              timeout=timeout if timeout is not None else config.CLOUD_TIMEOUT_S)
    except httpx.TimeoutException as e:
        raise NetworkError("GigaChat не ответил вовремя") from e
    except httpx.HTTPError as e:
        raise NetworkError(f"Нет связи с GigaChat: {e}") from e


def _json(resp) -> dict:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _message(resp) -> str:
    body = _json(resp)
    text = body.get("message") or body.get("error") or getattr(resp, "text", "") or ""
    return str(text)[:300]


def _raise_for(resp, url: str = "") -> None:
    code = getattr(resp, "status_code", 0)
    if code == 200:
        return
    msg = _message(resp) or f"HTTP {code}"
    if code == 402:
        raise QuotaError(msg, code)
    if code == 401:
        raise AuthError(msg, code)
    if code == 403 and ("token" in msg.lower() or "unauthor" in msg.lower()):
        raise AuthError(msg, code)
    if code == 429:
        raise BusyError(msg, code)
    if code in (500, 502, 503, 504):
        raise BusyError(msg, code)
    if code == 404 and "/models" not in url and "not found" in msg.lower() and "model" in msg.lower():
        raise BadRequest(msg, code)
    if code in (400, 404, 409, 413, 415, 422):
        raise BadRequest(msg, code)
    if code == 403:
        raise BadRequest(msg, code)
    raise GigaChatError(msg, code)


# ---------- токен доступа ----------
@dataclass
class _Token:
    value: str
    scope: str
    expires_at: float          # epoch, секунды
    obtained_at: float


_tokens: dict = {}
_scope_ok: dict = {}
_locks: dict = {}
_meta = threading.Lock()


def _ckey(cred: str) -> str:
    return hashlib.sha1(cred.encode()).hexdigest()


def _lock_for(key: str) -> threading.Lock:
    with _meta:
        return _locks.setdefault(key, threading.Lock())


def invalidate(cred: str) -> None:
    """Забывает токен (при 401 или смене ключа) — следующий запрос получит новый."""
    with _meta:
        _tokens.pop(_ckey(cred), None)


def _expiry(value, now: float) -> float:
    """GigaChat отдаёт expires_at в секундах; часть SDK/прокси — в миллисекундах. Понимаем оба варианта."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return now + 1800.0
    if ts <= 0:
        return now + 1800.0
    if ts > 1e12:                      # миллисекунды
        ts /= 1000.0
    if ts < now - 60:                  # часы разошлись — доверяем известному TTL 30 минут
        ts = now + 1800.0
    return max(ts, now + 60.0)         # не пытаемся «жить» токеном, который истекает вот-вот


def _scopes(cred: str) -> list:
    known = _scope_ok.get(_ckey(cred))
    order = [config.GIGACHAT_SCOPE] + [s for s in config.GIGACHAT_SCOPES if s != config.GIGACHAT_SCOPE]
    return ([known] + [s for s in order if s != known]) if known else order


def _fetch_token(cred: str, scope: str) -> _Token:
    headers = {
        "Authorization": f"Basic {cred}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    try:
        resp = _request("POST", config.GIGACHAT_AUTH_URL, headers=headers, data={"scope": scope},
                        timeout=config.AUTH_TIMEOUT_S)
    except GigaChatError:
        raise
    code = getattr(resp, "status_code", 0)
    if code != 200:
        msg = _message(resp)
        if code == 402:
            raise QuotaError(msg or "Закончился пакет токенов GigaChat", code)
        if code in (400, 401, 403):
            raise AuthError(msg or "Ключ авторизации отклонён", code)
        if code == 429:
            raise BusyError(msg or "Слишком много запросов токена", code)
        raise GigaChatError(msg or f"OAuth: HTTP {code}", code)
    body = _json(resp)
    token = (body.get("access_token") or "").strip()
    if not token:
        raise AuthError("GigaChat не вернул access_token", code)
    now = time.time()
    return _Token(value=token, scope=scope, expires_at=_expiry(body.get("expires_at"), now), obtained_at=now)


def token(cred: Optional[str] = None, force: bool = False) -> str:
    """Действующий access_token для ключа. Обновляет заранее и только один раз на всех."""
    cred = normalize(cred or config.GIGACHAT_CREDENTIALS)
    key = _ckey(cred)
    with _lock_for(key):
        cached = _tokens.get(key)
        now = time.time()
        if cached and not force and now < cached.expires_at - config.TOKEN_SKEW_S:
            return cached.value
        last: Optional[GigaChatError] = None
        for scope in _scopes(cred):
            try:
                fresh = _fetch_token(cred, scope)
            except AuthError as e:
                last = e
                log.info("Ключ GigaChat не принят со scope %s: %s", scope, e)
                continue
            _tokens[key] = fresh
            _scope_ok[key] = scope
            if len(_tokens) > 200:                 # публичный сайт: старые ключи не держим вечно
                with _meta:
                    _tokens.clear()
                    _locks.clear()
                    _scope_ok.clear()
                _locks[key] = threading.Lock()
                _tokens[key] = fresh
            log.info("Токен GigaChat получен (scope %s, действует %d мин)",
                     scope, round((fresh.expires_at - now) / 60))
            return fresh.value
        if last:
            raise last
        raise AuthError("Не удалось получить токен GigaChat")


def token_ttl(cred: Optional[str] = None) -> Optional[int]:
    """Сколько секунд осталось токену (для /api/health). None — токена ещё нет."""
    try:
        tok = _tokens.get(_ckey(normalize(cred or config.GIGACHAT_CREDENTIALS)))
    except ConfigError:
        return None
    return max(0, int(tok.expires_at - time.time())) if tok else None


_keepalive_started = False


def start_keepalive() -> None:
    """Фоновое продление токена: к моменту запроса пользователя он всегда свежий."""
    global _keepalive_started
    with _meta:
        if _keepalive_started or not config.GIGACHAT_CREDENTIALS or not config.TOKEN_KEEPALIVE:
            return
        _keepalive_started = True

    def loop():
        while True:
            try:
                token(config.GIGACHAT_CREDENTIALS)
            except GigaChatError as e:
                log.info("Продление токена GigaChat отложено: %s", e)
            time.sleep(max(15.0, config.TOKEN_KEEPALIVE_S))

    threading.Thread(target=loop, daemon=True, name="gigachat-token").start()


# ---------- запросы к модели ----------
def _post(cred: str, path: str, body: dict, timeout: Optional[float] = None):
    url = f"{config.GIGACHAT_BASE_URL}{path}"
    for attempt in (0, 1):
        tok = token(cred)
        headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"}
        resp = _request("POST", url, headers=headers, json=body, timeout=timeout)
        if getattr(resp, "status_code", 0) == 401 and attempt == 0:
            invalidate(cred)                       # токен истёк или отозван — берём новый и повторяем один раз
            log.info("GigaChat ответил 401 — обновляю токен и повторяю запрос")
            continue
        return resp
    return resp


def chat(cred: str, model: str, messages: list, *, temperature: float = 0.1,
         json_schema: Optional[dict] = None, attachments: Optional[list] = None,
         max_tokens: Optional[int] = None, timeout: Optional[float] = None) -> str:
    """Ответ модели текстом. messages — список {role, content}; attachments — id файлов в хранилище GigaChat."""
    msgs = [dict(m) for m in messages]
    body: dict = {"model": model, "messages": msgs, "temperature": temperature}
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    if json_schema:
        body["response_format"] = {"type": "json_schema", "schema": json_schema, "strict": True}
    if attachments:
        if not msgs:
            raise BadRequest("Нет сообщения для прикреплённого файла")
        msgs[-1]["attachments"] = list(attachments)
        body["function_call"] = "auto"            # по документации нужен для работы с файлами
    resp = _post(cred, "/chat/completions", body, timeout=timeout)
    _raise_for(resp, "chat")
    data = _json(resp)
    choices = data.get("choices") or []
    if not choices:
        raise GigaChatError("GigaChat вернул пустой ответ")
    choice = choices[0] or {}
    reason = choice.get("finish_reason")
    if reason == "blacklist":
        raise BadRequest("GigaChat отклонил запрос по тематическим ограничениям")
    content = ((choice.get("message") or {}).get("content") or "").strip()
    if not content:
        raise GigaChatError("GigaChat вернул пустой текст")
    return content


def embed(cred: str, model: str, texts: list) -> list:
    """Векторы для семантического поиска. Порядок совпадает с порядком texts."""
    out: list = []
    batch = max(1, config.EMBED_BATCH)
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        resp = _post(cred, "/embeddings", {"model": model, "input": chunk})
        _raise_for(resp, "embeddings")
        data = (_json(resp).get("data") or [])
        if len(data) != len(chunk):
            raise GigaChatError(f"GigaChat вернул {len(data)} векторов на {len(chunk)} текстов")
        data = sorted(data, key=lambda d: int(d.get("index", 0)))
        out += [list(map(float, d.get("embedding") or [])) for d in data]
    return out


def upload(cred: str, data: bytes, filename: str, mime: str) -> str:
    """Кладёт аудио в хранилище GigaChat (purpose=general) и возвращает id файла для attachments."""
    url = f"{config.GIGACHAT_BASE_URL}/files"
    for attempt in (0, 1):
        tok = token(cred)
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
        resp = _request("POST", url, headers=headers, data={"purpose": "general"},
                        files={"file": (filename, data, mime)}, timeout=max(config.AUDIO_TIMEOUT_S, 20))
        if getattr(resp, "status_code", 0) == 401 and attempt == 0:
            invalidate(cred)
            continue
        break
    _raise_for(resp, "files")
    file_id = (_json(resp).get("id") or "").strip()
    if not file_id:
        raise GigaChatError("GigaChat не вернул id загруженного файла")
    return file_id


def delete_file(cred: str, file_id: str) -> None:
    """Убирает аудио из хранилища, чтобы оно не занимало место."""
    url = f"{config.GIGACHAT_BASE_URL}/files/{file_id}/delete"
    tok = token(cred)
    resp = _request("POST", url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                    timeout=config.AUTH_TIMEOUT_S)
    if getattr(resp, "status_code", 0) not in (200, 204, 404):
        log.info("Не удалось удалить файл %s из GigaChat: %s", file_id, _message(resp))


def models(cred: str) -> list:
    """Список доступных моделей (используется для диагностики)."""
    url = f"{config.GIGACHAT_BASE_URL}/models"
    tok = token(cred)
    resp = _request("GET", url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"},
                    timeout=config.AUTH_TIMEOUT_S)
    _raise_for(resp, "models")
    return [m.get("id") or m.get("name") for m in (_json(resp).get("data") or [])]
