"""Облачный слой (GigaChat, Сбер): понимание запроса, распознавание речи из аудио и эмбеддинги.

Как это устроено:
  • токен доступа GigaChat живёт 30 минут — здесь он кешируется на каждый ключ авторизации,
    обновляется заранее (за GIGACHAT_TOKEN_REFRESH_S секунд) и один раз перезапрашивается при 401;
  • аудио уходит в модель вложением (модель сама расшифровывает речь и возвращает JSON),
    поэтому речь + смысл — по-прежнему один запрос, без лишней задержки;
  • если GigaChat недоступен (нет ключа, сеть, лимит, отказ авторизации) — «предохранитель»
    запоминает это на короткое время, и ассистент сразу работает локально, не ожидая таймаутов.
"""
import base64
import binascii
import contextvars
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from command import Command, INTENTS, KINDS  # noqa: F401  (Command реэкспортируется для совместимости)
from textutil import WEEKDAYS, calendar_hint

log = logging.getLogger("llm")


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    pass


class LLMNetworkError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


class LLMAudioUnavailable(LLMError):
    """Облако не смогло обработать аудио (модель/тариф без аудио, ошибка загрузки) — нужен локальный STT."""


# ---------- служебные исключения транспорта ----------
class _Busy(LLMError):
    """429 / 5xx: перегрузка или лимит — имеет смысл повторить и попробовать другую модель."""


class _BadRequest(LLMError):
    """400 / 404 / 422: не та модель, параметр или формат — другую модель попробовать можно."""

    def __init__(self, code: int, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _TokenRejected(LLMError):
    """401: токен истёк или отозван — перезапрашиваем и повторяем один раз."""


# ---------- состояние облака («предохранитель») ----------
_COOLDOWN = {"network": 15, "auth": 900, "busy": 20, "error": 10}
_MESSAGES = {
    "no_key": "Ключ GigaChat не задан — работаю локально (без облака): понимаю запросы проще, точность ниже.",
    "network": "Нет связи с GigaChat — работаю локально: запросы обрабатываются проще, точность ниже.",
    "auth": "Ключ GigaChat отклонён или истёк срок действия проекта — работаю локально: точность ниже.",
    "busy": "GigaChat перегружен или исчерпан лимит запросов — работаю локально: точность ниже.",
    "error": "GigaChat временно не отвечает — работаю локально: точность ниже.",
}
# Ключ может прийти от пользователя (заголовок X-GigaChat-Key, хранится только в его браузере) или из окружения сервера.
# Приоритет: ключ пользователя → ключ сервера (GIGACHAT_AUTH_KEY) → локальный режим. Состояние «предохранителя» — на каждый ключ.
_req_key: "contextvars.ContextVar[str]" = contextvars.ContextVar("gigachat_key", default="")
_states: dict = {}
_state_lock = threading.Lock()

_req_mode: "contextvars.ContextVar[str]" = contextvars.ContextVar("assistant_mode", default="auto")
# Таймаут текущего вызова модели: аудио ждём заметно дольше, чем текст.
_call_timeout: "contextvars.ContextVar[Optional[tuple]]" = contextvars.ContextVar("call_timeout", default=None)
MODES = ("auto", "online", "offline")      # auto — облако, при сбое локально; online — только облако; offline — только локально


def set_request_mode(mode: Optional[str]):
    return _req_mode.set(mode if mode in MODES else "auto")


def current_mode() -> str:
    return _req_mode.get()


def set_request_key(key: Optional[str]):
    return _req_key.set(config._clean_key((key or "")[:300]))


def current_key() -> str:
    return _req_key.get() or config.GIGACHAT_AUTH_KEY


def request_key() -> str:
    """Ключ, присланный браузером (или пустая строка, если его нет)."""
    return _req_key.get()


def _kid(key: Optional[str] = None) -> str:
    return hashlib.sha1((key if key is not None else current_key()).encode()).hexdigest()[:12]


def _state() -> dict:
    with _state_lock:
        return _states.setdefault(_kid(), {"reason": "", "until": 0.0, "checked": False, "detail": ""})


def has_key() -> bool:
    return bool(current_key())


def usable() -> bool:
    """Можно ли сейчас пробовать облако."""
    return has_key() and current_mode() != "offline" and time.time() >= _state()["until"]


def _mark_down(reason: str, detail: str = "") -> None:
    with _state_lock:
        _states.setdefault(_kid(), {}).update(reason=reason, detail=detail[:160], until=time.time() + _COOLDOWN.get(reason, 30), checked=True)
    log.warning("GigaChat недоступен (%s), ближайшие %s с работаю локально. Причина: %s",
                reason, _COOLDOWN.get(reason, 30), detail[:300])


def _mark_ok() -> None:
    with _state_lock:
        _states.setdefault(_kid(), {}).update(reason="", until=0.0, checked=True, detail="")


def status() -> dict:
    if current_mode() == "offline":
        return {"state": "offline", "message": "Офлайн-режим выбран вручную: всё обрабатывается на сервере без GigaChat, точность ниже."}
    if not has_key():
        return {"state": "no_key", "message": _MESSAGES["no_key"]}
    st = _state()
    if time.time() < st["until"]:
        r = st["reason"] or "error"
        return {"state": "down", "reason": r, "message": _MESSAGES.get(r, _MESSAGES["error"]), "detail": st.get("detail", "")}
    return {"state": "ok" if st["checked"] else "unknown", "message": ""}


def fallback_message() -> str:
    return status().get("message") or _MESSAGES["error"]


# ---------- TLS ----------
# GigaChat использует сертификаты Минцифры: без них requests не доверяет api.giga.chat.
_UA = "voice-assistant/2.0 (gigachat-api)"
_local = threading.local()
_bundle = {"path": None}
_bundle_lock = threading.Lock()


def ca_bundle():
    """Путь к файлу с корневыми сертификатами: обычные (certifi) + сертификат Минцифры из app/certs."""
    if config.GIGACHAT_CA_BUNDLE:
        return config.GIGACHAT_CA_BUNDLE
    if _bundle["path"]:
        return _bundle["path"]
    with _bundle_lock:
        if _bundle["path"]:
            return _bundle["path"]
        parts = []
        try:
            import certifi
            parts.append(Path(certifi.where()).read_bytes())
        except Exception:                                    # certifi нет — останется только сертификат Минцифры
            pass
        local_ca = config.RESOURCE_DIR / "certs" / "russian_trusted_root_ca_pem.crt"
        if local_ca.exists():
            parts.append(local_ca.read_bytes())
        elif not parts:
            _bundle["path"] = True                           # доверяем системному хранилищу
            return _bundle["path"]
        try:
            target = Path(config.MODELS_DIR) / "gigachat_ca_bundle.pem"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\n".join(parts))
            _bundle["path"] = str(target)
        except OSError:
            _bundle["path"] = True
        return _bundle["path"]


def _verify():
    return ca_bundle() if config.GIGACHAT_VERIFY_SSL else False


def _session():
    s = getattr(_local, "session", None)
    if s is None:
        import requests
        s = _local.session = requests.Session()
        s.headers.update({"User-Agent": _UA, "Accept": "application/json"})
    return s


def _http(method: str, url: str, **kw):
    import requests
    kw.setdefault("timeout", (5, config.CLOUD_TIMEOUT_S))
    kw.setdefault("verify", _verify())
    try:
        return _session().request(method, url, **kw)
    except requests.exceptions.SSLError as e:
        raise LLMNetworkError(
            "не удалось проверить сертификат GigaChat (TLS). Нужны сертификаты Минцифры — "
            "файл app/certs/russian_trusted_root_ca_pem.crt, см. README") from e
    except requests.exceptions.Timeout as e:
        raise LLMNetworkError("GigaChat не ответил за отведённое время") from e
    except requests.exceptions.RequestException as e:
        raise LLMNetworkError(str(e) or e.__class__.__name__) from e


# ---------- токен доступа (живёт 30 минут) ----------
_tokens: dict = {}                       # ключ авторизации → {"access_token", "exp"}
_tokens_lock = threading.Lock()
_refresh_lock = threading.Lock()
_refresher: Optional[threading.Thread] = None


def _as_unix(value) -> float:
    """GigaChat отдаёт expires_at в секундах, некоторые версии — в миллисекундах."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return time.time() + 30 * 60
    if v > 1e11:                          # миллисекунды
        v /= 1000.0
    return v


def _basic(auth_key: str) -> str:
    """Ключ авторизации — уже Base64 от «Client ID:Client Secret». Поддерживаем и пару «id:secret»."""
    k = config._clean_key(auth_key)
    if ":" in k and not _looks_b64(k):
        k = base64.b64encode(k.encode()).decode()
    return "Basic " + k


def _looks_b64(s: str) -> bool:
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return b":" in raw


def key_client_id(auth_key: str) -> str:
    """Client ID из ключа авторизации — чтобы показать пользователю, какой проект он ввёл (секрет не раскрываем)."""
    try:
        raw = base64.b64decode(config._clean_key(auth_key) + "=" * (-len(auth_key) % 4), validate=True).decode()
        return raw.split(":", 1)[0][:36]
    except Exception:
        return ""


def _fetch_token(auth_key: str) -> dict:
    headers = {"RqUID": str(uuid.uuid4()), "Authorization": _basic(auth_key),
               "Content-Type": "application/x-www-form-urlencoded"}
    resp = _http("POST", config.GIGACHAT_OAUTH_URL, headers=headers, data={"scope": config.GIGACHAT_SCOPE}, timeout=(5, 15))
    if resp.status_code in (400, 401, 403):
        detail = _detail(resp)
        hint = ""
        if "decode" in detail.lower() or "authorization" in detail.lower():
            hint = (" — проверьте, что вставлен «Ключ авторизации» из личного кабинета (строка Base64), "
                    "а не Client ID и не Client Secret")
        raise LLMAuthError(f"ключ авторизации отклонён ({resp.status_code}): {detail}{hint}")
    if resp.status_code >= 500:
        raise LLMNetworkError(f"сервер авторизации GigaChat недоступен ({resp.status_code})")
    if resp.status_code != 200:
        raise LLMAuthError(f"не удалось получить токен ({resp.status_code}): {_detail(resp)}")
    try:
        data = resp.json()
    except ValueError as e:
        raise LLMError("сервер авторизации вернул не JSON") from e
    token = (data.get("access_token") or "").strip()
    if not token:
        raise LLMAuthError("сервер авторизации не вернул токен")
    exp = _as_unix(data.get("expires_at"))
    return {"access_token": token, "exp": exp}


def token(auth_key: Optional[str] = None, force: bool = False) -> str:
    """Действующий токен доступа. Пока он жив (с запасом GIGACHAT_TOKEN_REFRESH_S) — берём из кеша."""
    key = config._clean_key(auth_key if auth_key is not None else current_key())
    if not key:
        raise LLMConfigError("Не задан ключ авторизации GigaChat.")
    with _tokens_lock:
        rec = _tokens.get(key)
        fresh = bool(rec) and not force and rec["exp"] - config.GIGACHAT_TOKEN_REFRESH_S > time.time()
    if fresh:
        return rec["access_token"]
    with _refresh_lock:                   # один запрос токена на всех: не долбим сервер авторизации
        with _tokens_lock:
            rec = _tokens.get(key)
            if rec and not force and rec["exp"] - config.GIGACHAT_TOKEN_REFRESH_S > time.time():
                return rec["access_token"]
        rec = _fetch_token(key)
        with _tokens_lock:
            _tokens[key] = rec
        log.info("GigaChat: получен новый токен, действует %d мин", max(1, int((rec["exp"] - time.time()) / 60)))
        return rec["access_token"]


def token_left() -> int:
    """Сколько секунд осталось у текущего токена (0 — токена нет). Для диагностики."""
    with _tokens_lock:
        rec = _tokens.get(config._clean_key(current_key()))
    return max(0, int(rec["exp"] - time.time())) if rec else 0


def _drop_token(auth_key: str = "") -> None:
    with _tokens_lock:
        _tokens.pop(config._clean_key(auth_key or current_key()), None)


def _refresh_loop() -> None:
    """Фоновое обновление токенов: запросы пользователей не должны ждать сервер авторизации."""
    while True:
        try:
            now = time.time()
            with _tokens_lock:
                keys = [k for k, v in _tokens.items() if v["exp"] - now < config.GIGACHAT_TOKEN_REFRESH_S + 120]
            for key in keys:
                try:
                    token(key, force=True)
                except LLMError as e:
                    log.info("GigaChat: не удалось обновить токен заранее (%s)", str(e)[:160])
        except Exception:                                  # поток не должен умирать
            log.debug("GigaChat: сбой фонового обновления токена", exc_info=True)
        time.sleep(60)


def start_token_refresher() -> None:
    global _refresher
    if not current_key():
        return
    with _tokens_lock:
        if _refresher and _refresher.is_alive():
            return
        _refresher = threading.Thread(target=_refresh_loop, daemon=True, name="gigachat-token")
        _refresher.start()


# ---------- запросы к API ----------
_best: dict = {}            # назначение ("text"/"audio") → модель, которая сработала
_schema_ok: dict = {}       # модель → принимает ли response_format=json_schema
_audio_block = {"until": 0.0}


def _detail(resp) -> str:
    try:
        data = resp.json()
    except Exception:
        return (resp.text or "").strip()[:200]
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or data
        if isinstance(msg, dict):
            msg = msg.get("message") or msg
        return str(msg)[:200]
    return str(data)[:200]


def _check(resp):
    code = resp.status_code
    if 200 <= code < 300:
        return resp
    if code == 401:
        raise _TokenRejected(_detail(resp))
    if code == 403:
        raise LLMAuthError(f"доступ запрещён ({_detail(resp)}) — проверьте scope проекта и оплату токенов")
    if code in (400, 404, 409, 413, 422):
        raise _BadRequest(code, _detail(resp))
    if code == 429:
        raise _Busy(f"лимит запросов: {_detail(resp)}")
    if code >= 500:
        raise _Busy(f"GigaChat {code}: {_detail(resp)}")
    raise LLMError(f"GigaChat вернул {code}: {_detail(resp)}")


def _post(path: str, key: str, **kw):
    url = path if path.startswith("http") else config.GIGACHAT_BASE_URL + path
    for attempt in (0, 1):
        headers = {"Authorization": "Bearer " + token(key)}
        headers.update(kw.pop("headers", None) or {})
        resp = _http("POST", url, headers=headers, **kw)
        try:
            return _check(resp)
        except _TokenRejected:
            if attempt:
                raise LLMAuthError("токен отклонён даже после повторного получения — проверьте ключ и scope")
            _drop_token(key)                              # токен истёк/отозван — берём новый и повторяем один раз
            log.info("GigaChat: токен отклонён, обновляю и повторяю запрос")


def _candidates(purpose: str = "text") -> list:
    names = [config.GIGACHAT_MODEL] + config.GIGACHAT_FALLBACK_MODELS
    if purpose == "audio":
        names = [config.GIGACHAT_AUDIO_MODEL] + names
    best = _best.get(purpose)
    order = ([best] if best else []) + names
    seen, out = set(), []
    for n in order:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _discover() -> list:
    """Если настроенные модели недоступны (переименованы/сняты) — берём список из GET /models."""
    key = config._clean_key(current_key())
    url = config.GIGACHAT_BASE_URL + "/models"
    try:
        resp = _http("GET", url, headers={"Authorization": "Bearer " + token(key)})
        _check(resp)
        data = resp.json().get("data") or []
    except LLMError as e:
        log.info("GigaChat: не удалось получить список моделей (%s)", str(e)[:160])
        return []
    bad = ("embedding", "image", "3d", "tts", "stt", "embed")
    out = []
    for m in data:
        name = m.get("id") or m.get("name") or ""
        if name and not any(b in name.lower() for b in bad):
            out.append(name)
    out.sort(reverse=True)                                # новые версии обычно «свежее» по имени
    return out[:4]


def _content(resp) -> str:
    try:
        data = resp.json()
    except ValueError as e:
        raise LLMError("GigaChat вернул не JSON") from e
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"в ответе GigaChat нет choices: {str(data)[:160]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):                         # v2-формат: список частей
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return (content or "").strip()


def _chat_once(model: str, messages: list, attachments: Optional[list], schema: Optional[dict]) -> str:
    timeout = _call_timeout.get()
    key = config._clean_key(current_key())
    used_schema = schema if (schema and config.GIGACHAT_STRUCTURED and _schema_ok.get(model, True)) else None
    payload = {"model": model, "messages": messages, "temperature": 0.1, "max_tokens": 900, "stream": False}
    if attachments:
        # GigaChat принимает вложения только внутри сообщения (messages[].attachments);
        # на верхнем уровне JSON они молча игнорируются и модель отвечает пустотой.
        idx = next((i for i in range(len(payload["messages"]) - 1, -1, -1)
                    if payload["messages"][i].get("role") == "user"), None)
        if idx is not None:
            payload["messages"][idx] = {**payload["messages"][idx], "attachments": list(attachments)}
    while True:
        body = dict(payload)
        if used_schema:
            body["response_format"] = {"type": "json_schema", "schema": used_schema, "strict": True}
        try:
            text = _content(_post("/chat/completions", key, json=body,
                                  **({"timeout": timeout} if timeout else {})))
        except _BadRequest as e:
            if used_schema:                               # модель/версия не принимает схему — просим JSON промптом
                _schema_ok[model] = False
                used_schema = None
                log.info("GigaChat: модель %s не приняла JSON-схему (%s) — прошу JSON в промпте", model, e.detail[:120])
                continue
            raise
        if text.strip():
            return text
        if used_schema:
            # Бывает 200 с пустым content, когда запрошена строгая схема: повторяем без response_format
            # и запоминаем, что этой модели схема не подходит (иначе теряли бы запрос на каждом вызове).
            _schema_ok[model] = False
            used_schema = None
            log.info("GigaChat: модель %s вернула пустой ответ со схемой — повторяю без response_format", model)
            continue
        log.warning("GigaChat: модель %s вернула пустой ответ", model)
        return text


def _call(messages: list, attachments: Optional[list] = None, purpose: str = "text",
          schema: Optional[dict] = None) -> str:
    """Запрос к модели с перебором моделей: недоступная/перегруженная заменяется следующей."""
    last: Optional[Exception] = None
    _call_timeout.set((5, config.AUDIO_TIMEOUT_S) if purpose == "audio" else None)
    queue = _candidates(purpose)
    tried: list = []
    discovered = False
    while queue:
        model = queue.pop(0)
        tried.append(model)
        for attempt in range(2):
            try:
                text = _chat_once(model, messages, attachments, schema)
                _best[purpose] = model
                _mark_ok()
                return text
            except LLMAuthError as e:
                _mark_down("auth", str(e))
                raise
            except LLMNetworkError as e:
                _mark_down("network", str(e))
                raise
            except _Busy as e:
                last = e
                if attempt == 0:
                    time.sleep(1.0)                       # короткая пауза и ещё одна попытка той же модели
                    continue
                break
            except _BadRequest as e:
                last = e
                break                                     # модель не подошла — пробуем следующую
            except LLMError as e:
                last = e
                break
        if purpose == "audio" and isinstance(last, _BadRequest):
            raise LLMAudioUnavailable(str(last))          # аудио этой моделью не обработать: дальше локальный STT
        if isinstance(last, _BadRequest) and not discovered and not queue:
            discovered = True
            extra = [m for m in _discover() if m not in tried]
            if extra:
                log.warning("GigaChat: настроенные модели недоступны, пробую %s", ", ".join(extra))
                queue.extend(extra)
    err = last or LLMError("GigaChat не ответил")
    reason = "busy" if isinstance(err, _Busy) else "error"
    _mark_down(reason, str(err))
    if isinstance(err, _BadRequest):
        raise LLMError(f"модель недоступна ({err.detail})")
    raise LLMError(str(err))


def _call_audio(messages: list, attachments: list, schema: Optional[dict]) -> str:
    if time.time() < _audio_block["until"]:
        raise LLMAudioUnavailable("распознавание речи в GigaChat недоступно для этого ключа")
    try:
        return _call(messages, attachments, purpose="audio", schema=schema)
    except LLMAudioUnavailable:
        _audio_block["until"] = time.time() + 3600         # час не тратим время на неудачные попытки
        log.info("GigaChat: аудио не поддерживается — распознаю речь локально")
        raise


# GigaChat: /files требует явное назначение файла, иначе 400 «No purpose provided».
FILE_PURPOSE = "general"


def _upload(audio: bytes, mime: str) -> str:
    key = config._clean_key(current_key())
    ext = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/webm": "weba",
           "audio/ogg": "ogg", "audio/opus": "opus", "audio/mp4": "mp4", "audio/mp3": "mp3",
           "audio/mpeg": "mp3", "audio/x-m4a": "m4a", "audio/m4a": "m4a"}.get((mime or "").lower(), "wav")
    try:
        resp = _post("/files", key, files={"file": (f"voice.{ext}", audio, mime or "audio/wav")},
                     data={"purpose": FILE_PURPOSE}, timeout=(5, config.AUDIO_TIMEOUT_S))
        file_id = (resp.json() or {}).get("id")
    except _BadRequest as e:
        raise LLMAudioUnavailable(f"аудио не принято хранилищем GigaChat ({e.detail})") from e
    if not file_id:
        raise LLMAudioUnavailable("GigaChat не вернул идентификатор загруженного аудио")
    return file_id


def _delete_file(file_id: str) -> None:
    try:
        _post(f"/files/{file_id}/delete", config._clean_key(current_key()))
    except LLMError as e:                                  # не критично: файл просто останется в хранилище
        log.debug("GigaChat: не удалось удалить аудиофайл %s (%s)", file_id, str(e)[:120])


def probe() -> None:
    """Быстрая проверка доступности GigaChat (в фоне при старте, чтобы показать режим в интерфейсе)."""
    if not has_key():
        return
    start_token_refresher()
    try:
        _call([{"role": "system", "content": "Отвечай одним словом."}, {"role": "user", "content": "Ответь: ok"}])
    except LLMError as e:
        log.warning("Проверка GigaChat: %s", e)


def check_key(key: str) -> dict:
    """Проверка ключа, введённого пользователем. state: ok | auth (ключ отклонён) | down (не проверить: сеть/лимит)."""
    token_ctx = set_request_key(key)
    try:
        k = current_key()
        if not k:
            return {"state": "none", "message": "Ключ пустой."}
        if not _looks_b64(k):
            return {"state": "auth", "message": "Это не похоже на ключ авторизации GigaChat: нужна строка Base64 "
                                                "из личного кабинета (кнопка «Получить ключ»), а не Client ID."}
        try:
            token(k, force=True)
            _call([{"role": "system", "content": "Отвечай одним словом."}, {"role": "user", "content": "Ответь: ok"}])
        except LLMAuthError as e:
            return {"state": "auth", "message": "Ключ отклонён GigaChat. Проверьте, что он скопирован полностью "
                                                "и проект активен. (" + str(e)[:140] + ")"}
        except LLMError:
            return {"state": "down", "message": status().get("message") or "Не удалось связаться с GigaChat."}
        cid = key_client_id(k)
        return {"state": "ok", "message": "Ключ принят. Облачный режим включён." + (f" Проект: {cid[:8]}…" if cid else "")}
    finally:
        _req_key.reset(token_ctx)


# ---------- промпт ----------
def _system_prompt(now: datetime) -> str:
    return f"""Ты — модуль понимания речи голосового ассистента личных задач, напоминаний и встреч.
Ты ничего не выполняешь: только определяешь намерение и извлекаешь параметры.
Отвечай СТРОГО одним JSON-объектом, без markdown и пояснений.

Сейчас: {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}.
{calendar_hint(now)}

Формат ответа (неиспользуемые поля = null):
{{"intent": "create|list|search|complete|reopen|delete|update|clarify|other", "transcript": "...", "kind": "task|meeting|reminder|null",
  "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "range_start": "YYYY-MM-DD", "range_end": "YYYY-MM-DD",
  "query": "...", "ref": "last|it|all|all_done|ord:N|null", "repeat": "daily|weekdays|weekly|monthly|null", "reply": "..."}}

Правила:
1. transcript — дословная расшифровка речи, если вход — аудио; если вход — текст, повтори его. Если в аудио нет внятной речи — пустая строка и intent="other".
2. intent:
   - create — создать запись (задача, напоминание, встреча);
   - list — показать записи за период («что у меня сегодня», «какие планы на завтра», «покажи встречи на неделю»). Разные формулировки одного смысла — один и тот же intent;
   - search — найти ранее созданную запись по смыслу («найди задачу про презентацию», «когда была встреча с Иваном», «я ставил задачу по договору»);
   - complete — пользователь просит отметить запись выполненной или говорит, что сделал дело («отметь задачу про договор выполненной», «я купил билеты»); query — ключевые слова записи;
   - reopen — снять отметку выполнения («отмени выполнение», «верни задачу про договор в работу», «задача ещё не сделана»); query/ref как у complete;
   - delete — удалить запись («удали задачу про договор», «удали последнюю запись»); query — ключевые слова записи;
   - update — перенести запись на другое время/дату («перенеси встречу с Иваном на пятницу в 15:00», «а перенеси её на завтра»): date/time = НОВЫЕ значения (только названные), query — ключевые слова записи;
   - ref (для delete/update/complete/reopen, когда запись названа не по смыслу): "last" — «последнюю запись/заметку», "it" — «её / эту / его» (запись, о которой только что шла речь), "all" — «все» («удали все», «отметь все задачи выполненными»; если названы слова записи — они в query, а kind ограничивает тип), "all_done" — «все выполненные» (для delete), "ord:N" — N-я запись из только что показанного списка («удали первую» → "ord:1", «отметь третью» → "ord:3"); query тогда null;
   - repeat (только для create): «каждый день»/«ежедневно» — daily, «по будням» — weekdays, «каждый понедельник»/«каждую неделю» — weekly (date = ближайший такой день), «каждый месяц» — monthly; иначе null. Слова повтора в title не включай;
   - clarify — для создания не хватает обязательных данных; в reply задай ОДИН короткий вопрос;
   - other — приветствие, благодарность, не относится к задачам; в reply — короткий вежливый ответ и подсказка, что ты умеешь.
3. kind: meeting — встреча, созвон, приём, визит (к врачу, стоматологу и т.п.); reminder — «напомни…»; task — остальные дела («нужно», «поставь задачу», «купить», «отправить»).
   Для list/search/complete заполняй kind ТОЛЬКО если пользователь явно ограничил тип («мои встречи», «какие задачи»), иначе null.
4. title — краткая суть без даты, времени и слов-команд, с заглавной буквы: «Позвонить Ивану», «Встреча с дизайнером», «Приём у стоматолога Олега». Ничего не выдумывай.
5. Даты и время:
   - date в формате YYYY-MM-DD, time — 24-часовой HH:MM. Используй календарь выше.
   - «утра» = AM, «вечера»/«дня» = PM («в 7 вечера» = 19:00). Без уточнения: 8–11 → утро, 12 → полдень, 1–7 → 13:00–19:00.
   - «через два часа / 30 минут» — прибавь к текущему времени (обнови и date, если перешли на другой день).
   - «в пятницу» — ближайшая такая дата начиная с сегодня; если сегодня как раз этот день недели и не сказано «сегодня» — на следующей неделе.
   - «до конца недели» — date = ближайшее воскресенье, time = null. «На следующей неделе» для создания без дня — понедельник следующей недели.
6. Для create: у задачи обязателен только title (дата/время по желанию). У встречи и напоминания обязательны title, date и time — если чего-то нет, intent="clarify" и спроси недостающее («На какое время поставить встречу?»).
7. Диалог: если предыдущий вопрос ассистента был уточняющим, а новая реплика отвечает на него («в три», «завтра», «в 15:00») — объедини с ранее названным и верни intent="create" с полными данными.
8. list: заполни range_start и range_end (включительно). «Сегодня» — оба = сегодня; «на этой неделе» — от сегодня до воскресенья; «на следующей неделе» — понедельник–воскресенье следующей недели; «в пятницу» — оба = эта дата. Если период не назван («покажи все задачи», «какие у меня встречи?», «что у меня есть?») — range_start = "all", range_end = null. «В прошлом месяце», «в ноябре» — первый и последний день этого месяца.
9. search: query — ключевые слова по смыслу в начальной форме (имена, темы), без служебных слов и без слов «задача/встреча».
10. reply заполняй только для clarify и other. Пиши по-русски, коротко."""


# Схема ответа: модель возвращает строго эти поля (все необязательные допускают null).
_NULLABLE_STR = {"type": ["string", "null"]}
_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": sorted(INTENTS)},
        "transcript": dict(_NULLABLE_STR),
        "kind": {"type": ["string", "null"], "enum": sorted(KINDS) + [None]},
        "title": dict(_NULLABLE_STR),
        "date": dict(_NULLABLE_STR),
        "time": dict(_NULLABLE_STR),
        "range_start": dict(_NULLABLE_STR),
        "range_end": dict(_NULLABLE_STR),
        "query": dict(_NULLABLE_STR),
        "ref": dict(_NULLABLE_STR),
        "repeat": {"type": ["string", "null"], "enum": ["daily", "weekdays", "weekly", "monthly", None]},
        "reply": dict(_NULLABLE_STR),
    },
    "required": ["intent", "transcript", "kind", "title", "date", "time", "range_start", "range_end",
                 "query", "ref", "repeat", "reply"],
    "additionalProperties": False,
}


def _extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise LLMError(f"Модель вернула не JSON: {text[:200]!r}")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Некорректный JSON от модели: {e}") from e


def _messages(now: datetime, history: list, text: Optional[str], audio: bool) -> list:
    """История диалога передаётся обычными репликами; длинные ответы ассистента подрезаем (списки не нужны модели)."""
    msgs = [{"role": "system", "content": _system_prompt(now)}]
    for role, msg in (history or [])[-config.HISTORY_TURNS * 2:]:
        body = (msg or "").strip()
        if not body:
            continue
        if role != "user" and len(body) > 300:
            body = body[:300] + " …"
        msgs.append({"role": "user" if role == "user" else "assistant", "content": body})
    if audio:
        msgs.append({"role": "user", "content":
                     "Новый запрос пользователя — аудиозапись (русская речь). Расшифруй её в transcript и обработай по правилам."})
    else:
        msgs.append({"role": "user", "content": f"Новый запрос пользователя: {text}"})
    return msgs


def _drop_rejected_request_key(err: Exception) -> bool:
    """Ключ из браузера отклонён GigaChat, а на сервере есть рабочий? Тогда работаем ключом сервера.

    Типичный случай: пользователь перевыпустил ключ в кабинете, а в localStorage остался старый —
    GigaChat отвечает 401 «credentials doesn't match db data», и раньше ассистент из-за этого
    навсегда уходил в локальный режим, хотя ключ сервера рабочий.
    """
    if _req_key.get() and config.GIGACHAT_AUTH_KEY:
        log.warning("Ключ из браузера отклонён (%s) — работаю ключом сервера", str(err)[:160])
        _req_key.set("")
        return True
    return False


def parse(now: datetime, history: list, text: Optional[str] = None,
          audio: Optional[bytes] = None, mime: str = "audio/wav") -> Command:
    """history — список (role, message), role = 'user' | 'assistant'."""
    try:
        return _parse(now, history, text, audio, mime)
    except LLMAuthError as e:
        if _drop_rejected_request_key(e):
            return _parse(now, history, text, audio, mime)
        raise


def _parse(now: datetime, history: list, text: Optional[str] = None,
           audio: Optional[bytes] = None, mime: str = "audio/wav") -> Command:
    messages = _messages(now, history, text, audio=bool(audio))
    if audio:
        file_id = _upload(audio, mime)
        try:
            raw = _call_audio(messages, [file_id], schema=_SCHEMA)
        finally:
            if file_id:
                _delete_file(file_id)
    else:
        raw = _call(messages, schema=_SCHEMA)
    log.info("GigaChat: %s", raw.replace("\n", " ")[:400])
    return Command.from_dict(_extract_json(raw))


def embed(texts: list, query: bool = False) -> list:
    """Эмбеддинги GigaChat для семантического поиска. Ошибки сети переводят ассистента в локальный режим."""
    try:
        return _embed(texts, query)
    except LLMAuthError as e:
        if _drop_rejected_request_key(e):
            return _embed(texts, query)
        raise


def _embed(texts: list, query: bool = False) -> list:
    key = config._clean_key(current_key())
    if not key:
        raise LLMConfigError("Не задан ключ GigaChat.")
    models = [config.GIGACHAT_EMBED_MODEL, "Embeddings", "Embeddings-2"]
    seen, order = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m)
            order.append(m)
    out: list = []
    last: Optional[Exception] = None
    for model in order:
        out = []
        try:
            for i in range(0, len(texts), 50):
                resp = _post("/embeddings", key, json={"model": model, "input": texts[i:i + 50]})
                data = (resp.json() or {}).get("data") or []
                data.sort(key=lambda d: d.get("index", 0))
                out += [list(map(float, d.get("embedding") or [])) for d in data]
            if len(out) == len(texts):
                _best["embed"] = model
                return out
            last = LLMError(f"модель {model} вернула {len(out)} векторов вместо {len(texts)}")
        except LLMAuthError as e:
            _mark_down("auth", str(e))
            raise
        except LLMNetworkError as e:
            _mark_down("network", str(e))
            raise
        except _BadRequest as e:
            last = e
            continue
        except _Busy as e:
            last = e
            continue
    err = last or LLMError("эмбеддинги недоступны")
    raise LLMError(str(err))
