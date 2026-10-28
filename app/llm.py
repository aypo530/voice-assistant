"""Облачный слой (Gemini): распознавание речи + понимание запроса одним вызовом, эмбеддинги для поиска.

Если Gemini недоступен (нет ключа, нет сети/VPN, регион, лимит) — модуль запоминает это на короткое время
(«предохранитель»), и ассистент автоматически работает локально, не ожидая таймаутов на каждом запросе.
"""
import contextvars
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime
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


class LLMRegionError(LLMError):
    pass


class LLMAuthError(LLMError):
    pass


# ---------- состояние облака («предохранитель») ----------
_COOLDOWN = {"network": 15, "region": 300, "auth": 900, "busy": 20, "error": 10}
_MESSAGES = {
    "no_key": "Ключ Gemini не задан — работаю локально (без облака): понимаю запросы проще, точность ниже.",
    "network": "Нет связи с Gemini (возможно, нужен VPN) — работаю локально: запросы обрабатываются проще, точность ниже.",
    "region": "Gemini недоступен в вашем регионе (нужен VPN) — работаю локально: точность ниже.",
    "auth": "Ключ Gemini отклонён — работаю локально: точность ниже.",
    "busy": "Gemini перегружен или исчерпан лимит запросов — работаю локально: точность ниже.",
    "error": "Gemini временно не отвечает — работаю локально: точность ниже.",
}
# Ключ может прийти от пользователя (заголовок X-Gemini-Key, хранится только в его браузере) или из окружения сервера.
# Приоритет: ключ пользователя → ключ сервера (GEMINI_API_KEY) → локальный режим. Состояние «предохранителя» — на каждый ключ.
_req_key: "contextvars.ContextVar[str]" = contextvars.ContextVar("gemini_key", default="")
_states: dict = {}
_state_lock = threading.Lock()


_req_mode: "contextvars.ContextVar[str]" = contextvars.ContextVar("assistant_mode", default="auto")
MODES = ("auto", "online", "offline")      # auto — облако, при сбое локально; online — только облако; offline — только локально


def set_request_mode(mode: Optional[str]):
    return _req_mode.set(mode if mode in MODES else "auto")


def current_mode() -> str:
    return _req_mode.get()


def set_request_key(key: Optional[str]):
    return _req_key.set((key or "").strip()[:200])


def current_key() -> str:
    return _req_key.get() or config.GEMINI_API_KEY


def _kid(key: Optional[str] = None) -> str:
    return hashlib.sha1((key if key is not None else current_key()).encode()).hexdigest()[:12]


def _state() -> dict:
    with _state_lock:
        return _states.setdefault(_kid(), {"reason": "", "until": 0.0, "checked": False})


def has_key() -> bool:
    return bool(current_key())


def usable() -> bool:
    """Можно ли сейчас пробовать облако."""
    return has_key() and current_mode() != "offline" and time.time() >= _state()["until"]


def _mark_down(reason: str, detail: str = "") -> None:
    with _state_lock:
        _states.setdefault(_kid(), {}).update(reason=reason, detail=detail[:160], until=time.time() + _COOLDOWN.get(reason, 30), checked=True)
    log.warning("Gemini недоступен (%s), ближайшие %s с работаю локально. Причина: %s", reason, _COOLDOWN.get(reason, 30), detail[:300])


def _mark_ok() -> None:
    with _state_lock:
        _states.setdefault(_kid(), {}).update(reason="", until=0.0, checked=True)


def status() -> dict:
    if current_mode() == "offline":
        return {"state": "offline", "message": "Офлайн-режим выбран вручную: всё обрабатывается на сервере без Gemini, точность ниже."}
    if not has_key():
        return {"state": "no_key", "message": _MESSAGES["no_key"]}
    st = _state()
    if time.time() < st["until"]:
        r = st["reason"] or "error"
        return {"state": "down", "reason": r, "message": _MESSAGES.get(r, _MESSAGES["error"]), "detail": st.get("detail", "")}
    return {"state": "ok" if st["checked"] else "unknown", "message": ""}


def fallback_message() -> str:
    return status().get("message") or _MESSAGES["error"]


def _is_network_error(exc) -> bool:
    seen = 0
    names = ("ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException",
             "ProxyError", "RemoteProtocolError", "NetworkError", "ConnectionError")
    while exc is not None and seen < 6:
        if isinstance(exc, (OSError, TimeoutError)) or type(exc).__name__ in names:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _classify(e: Exception) -> LLMError:
    code = getattr(e, "code", None)
    low = str(e).lower()
    if _is_network_error(e):
        return LLMNetworkError(str(e))
    if code == 400 and ("location is not supported" in low or "user location" in low):
        return LLMRegionError(str(e))
    if code in (401, 403) or (code == 400 and "api key" in low):
        return LLMAuthError(str(e))
    if code == 429 or (isinstance(code, int) and code >= 500):
        return LLMError(f"busy: {e}")
    return LLMError(str(e))


def _reason_of(err: LLMError) -> str:
    if isinstance(err, LLMNetworkError):
        return "network"
    if isinstance(err, LLMRegionError):
        return "region"
    if isinstance(err, LLMAuthError):
        return "auth"
    return "busy" if str(err).startswith("busy") else "error"


# ---------- клиент ----------
_clients: dict = {}
_model: dict = {}          # рабочая модель: отдельно для ключей AI Studio («AIza…») и Vertex Express («AQ.…»)


_mode_ok: dict = {}        # ключ → режим, в котором он сработал: "studio" (AI Studio) или "vertex" (Vertex Express)


def _is_vertex_key(key: str) -> bool:
    return key.startswith("AQ.")


def _modes() -> list:
    """Ключ «AQ.…» бывает двух видов: новый ключ AI Studio и ключ Vertex Express. Пробуем оба адреса и запоминаем рабочий."""
    key = current_key()
    if not _is_vertex_key(key):
        return ["studio"]
    ok = _mode_ok.get(key)
    return [ok] + [m for m in ("studio", "vertex") if m != ok] if ok else ["studio", "vertex"]


def _models(vertex: bool = False) -> list:
    """Основная модель и запасные (у каждой модели свой лимит). Имена моделей в Vertex другие."""
    if vertex:
        names = [config.GEMINI_VERTEX_MODEL] + config.GEMINI_VERTEX_FALLBACK_MODELS
    else:
        names = [config.GEMINI_MODEL] + config.GEMINI_FALLBACK_MODELS
    ok = _model.get(vertex)
    return ([ok] if ok else []) + [n for n in names if n != ok]


def _get_client(vertex: Optional[bool] = None):
    key = current_key()
    if not key:
        raise LLMConfigError("Не задан ключ Gemini.")
    if vertex is None:
        vertex = _mode_ok.get(key) == "vertex"
    client = _clients.get((key, vertex))
    if client is None:
        from google import genai  # ленивый импорт: тесты и локальный режим работают без SDK
        from google.genai import types
        if len(_clients) > 50:
            _clients.clear()
        opts = types.HttpOptions(timeout=int(config.CLOUD_TIMEOUT_S * 1000))
        if vertex:      # Vertex AI Express: другой адрес API
            client = genai.Client(vertexai=True, api_key=key, http_options=opts)
        else:
            client = genai.Client(api_key=key, http_options=opts)
        _clients[(key, vertex)] = client
    return client


def _discover_model() -> Optional[str]:
    """Если настроенная модель снята с поддержки — берём самую новую доступную flash-модель."""
    bad = ("image", "tts", "live", "audio", "embedding", "robotics", "computer")
    names = []
    for m in _get_client(False).models.list():
        n = (m.name or "").removeprefix("models/")
        actions = getattr(m, "supported_actions", None) or []
        if "flash" in n and "lite" not in n and not any(b in n for b in bad) and (not actions or "generateContent" in actions):
            names.append(n)
    names.sort(reverse=True)
    return names[0] if names else None


def _generate(contents: list, system: str, json_mode: bool = True) -> str:
    """Пробует доступные адреса API (для ключей «AQ.…» их два). Переходит к следующему только при отказе по ключу."""
    last: Optional[LLMError] = None
    for mode in _modes():
        try:
            text = _generate_mode(contents, system, json_mode, mode == "vertex")
            _mode_ok[current_key()] = mode
            _mark_ok()
            return text
        except LLMAuthError as e:
            last = e
            log.info("Ключ не принят в режиме %s: %s", mode, str(e)[:200])
            continue
        except LLMError as e:
            last = e
            break
    assert last is not None
    _mark_down(_reason_of(last), str(last))
    raise last


def _generate_mode(contents: list, system: str, json_mode: bool, vertex: bool) -> str:
    from google.genai import types
    cfg = types.GenerateContentConfig(system_instruction=system, temperature=0.1,
                                      response_mime_type="application/json" if json_mode else None)
    queue = _models(vertex)
    last: Optional[LLMError] = None
    tried_discover = False
    while queue:
        model = queue.pop(0)
        for attempt in range(3):
            try:
                resp = _get_client(vertex).models.generate_content(model=model, contents=contents, config=cfg)
                _model[vertex] = model
                return resp.text or ""
            except LLMConfigError:
                raise
            except Exception as e:  # google.genai.errors.APIError, httpx-ошибки
                last = _classify(e)
                code = getattr(e, "code", None)
                if isinstance(last, (LLMRegionError, LLMAuthError)):
                    queue.clear()
                    break                               # другая модель не поможет
                if isinstance(last, LLMNetworkError):
                    queue.clear()                       # таймаут/нет сети: не ждём повторно, сразу локальный режим
                    break
                if code == 404:                         # модель снята — ищем доступную
                    if not tried_discover and not vertex:
                        tried_discover = True
                        try:
                            found = _discover_model()
                        except Exception:
                            found = None
                        if found and found != model:
                            log.warning("Модель %s недоступна, пробую %s", model, found)
                            queue.insert(0, found)
                    break
                if code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.0)                     # короткий повтор, потом — другая модель
                    continue
                break
        else:
            continue
    assert last is not None
    raise last


def probe() -> None:
    """Быстрая проверка доступности Gemini (вызывается в фоне при старте, чтобы показать режим в интерфейсе)."""
    if not has_key():
        return
    try:
        _generate(["Ответь одним словом: ok"], "Отвечай одним словом.", json_mode=False)
    except LLMError as e:
        log.warning("Проверка Gemini: %s", e)


def check_key(key: str) -> dict:
    """Проверка ключа, введённого пользователем. state: ok | auth (ключ отклонён) | down (ключ не проверить: нет сети/VPN/лимит)."""
    token = set_request_key(key)
    try:
        if not current_key():
            return {"state": "none", "message": "Ключ пустой."}
        try:
            _generate(["Ответь одним словом: ok"], "Отвечай одним словом.", json_mode=False)
            return {"state": "ok", "message": "Ключ принят. Облачный режим включён."}
        except LLMAuthError:
            return {"state": "auth", "message": "Ключ отклонён Gemini. Проверьте, что он скопирован полностью. (" + status().get("detail", "")[:140] + ")"}
        except LLMError:
            return {"state": "down", "message": status().get("message") or "Не удалось связаться с Gemini."}
    finally:
        _req_key.reset(token)


# ---------- промпт ----------
def _system_prompt(now: datetime) -> str:
    return f"""Ты — модуль понимания речи голосового ассистента личных задач, напоминаний и встреч.
Ты ничего не выполняешь: только определяешь намерение и извлекаешь параметры.
Отвечай СТРОГО одним JSON-объектом, без markdown и пояснений.

Сейчас: {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}.
{calendar_hint(now)}

Формат ответа (неиспользуемые поля = null):
{{"intent": "create|list|search|complete|delete|update|clarify|other", "transcript": "...", "kind": "task|meeting|reminder|null",
  "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "range_start": "YYYY-MM-DD", "range_end": "YYYY-MM-DD",
  "query": "...", "ref": "last|it|null", "repeat": "daily|weekdays|weekly|monthly|null", "reply": "..."}}

Правила:
1. transcript — дословная расшифровка речи, если вход — аудио; если вход — текст, повтори его. Если в аудио нет внятной речи — пустая строка и intent="other".
2. intent:
   - create — создать запись (задача, напоминание, встреча);
   - list — показать записи за период («что у меня сегодня», «какие планы на завтра», «покажи встречи на неделю»). Разные формулировки одного смысла — один и тот же intent;
   - search — найти ранее созданную запись по смыслу («найди задачу про презентацию», «когда была встреча с Иваном», «я ставил задачу по договору»);
   - complete — пользователь просит отметить запись выполненной или говорит, что сделал дело («отметь задачу про договор выполненной», «я купил билеты»); query — ключевые слова записи;
   - delete — удалить запись («удали задачу про договор», «удали последнюю запись»); query — ключевые слова записи;
   - update — перенести запись на другое время/дату («перенеси встречу с Иваном на пятницу в 15:00», «а перенеси её на завтра»): date/time = НОВЫЕ значения (только названные), query — ключевые слова записи;
   - ref (для delete/update/complete, когда запись названа не по смыслу): "last" — «последнюю запись/заметку», "it" — «её / эту / его» (запись, о которой только что шла речь); query тогда null;
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


def _extract_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < start:
        raise LLMError(f"Модель вернула не JSON: {text[:200]!r}")
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Некорректный JSON от модели: {e}") from e


def parse(now: datetime, history: list, text: Optional[str] = None,
          audio: Optional[bytes] = None, mime: str = "audio/wav") -> Command:
    """history — список (role, message), role = 'user' | 'assistant'."""
    contents: list = []
    if history:
        lines = [f"{'Пользователь' if r == 'user' else 'Ассистент'}: {m}" for r, m in history]
        contents.append("Предыдущий диалог:\n" + "\n".join(lines))
    if audio:
        from google.genai import types
        contents.append("Новый запрос пользователя — аудиозапись (русская речь). Расшифруй её в transcript и обработай по правилам.")
        contents.append(types.Part.from_bytes(data=audio, mime_type=mime))
    else:
        contents.append(f"Новый запрос пользователя: {text}")
    raw = _generate(contents, _system_prompt(now))
    log.info("LLM: %s", raw.replace("\n", " ")[:400])
    return Command.from_dict(_extract_json(raw))


def embed(texts: list, query: bool = False) -> list:
    """Эмбеддинги Gemini для семантического поиска. Ошибки сети переводят ассистента в локальный режим."""
    from google.genai import types
    cfg = types.EmbedContentConfig(task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT",
                                   output_dimensionality=config.EMBED_DIM)
    out: list = []
    try:
        client = _get_client()
        for i in range(0, len(texts), 100):
            resp = client.models.embed_content(model=config.GEMINI_EMBED_MODEL, contents=texts[i:i + 100], config=cfg)
            out += [list(e.values) for e in resp.embeddings]
    except LLMConfigError:
        raise
    except Exception as e:
        err = _classify(e)
        if isinstance(err, (LLMNetworkError, LLMRegionError, LLMAuthError)):
            _mark_down(_reason_of(err), str(err))
        raise err from e
    return out
