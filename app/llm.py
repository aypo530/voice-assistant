"""Облачный слой (GigaChat): распознавание речи + понимание запроса, эмбеддинги для поиска.

GigaChat доступен из России без VPN: речь и смысл разбираются одним запросом к модели со
структурированным ответом (JSON-схема), поэтому задержка остаётся прежней. Если облако недоступно
(нет ключа, нет сети, лимит, отказ ключа) — модуль запоминает это на короткое время («предохранитель»),
и ассистент автоматически работает локально, не ожидая таймаутов на каждом запросе.

Токен доступа GigaChat живёт 30 минут; обновлением занимается gigachat.token() — заранее и по 401.
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
import gigachat as gc
from command import Command, INTENTS  # noqa: F401  (Command реэкспортируется для совместимости)
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


class LLMQuotaError(LLMAuthError):
    """Ключ принят, но закончился пакет токенов GigaChat."""


class LLMAttachError(LLMError):
    """GigaChat не смог обработать приложенный файл (например, у модели нет аудио)."""


class LLMRegionError(LLMError):
    """Оставлено для совместимости: у GigaChat нет региональных ограничений, класс не используется."""


# ---------- состояние облака («предохранитель») ----------
_COOLDOWN = {"network": 15, "auth": 300, "quota": 300, "busy": 20, "error": 15}
_MESSAGES = {
    "no_key": "Ключ GigaChat не задан — работаю локально (без облака): понимаю запросы проще, точность ниже.",
    "network": "Нет связи с GigaChat — работаю локально: запросы обрабатываются проще, точность ниже.",
    "auth": "Ключ GigaChat отклонён — работаю локально: точность ниже.",
    "quota": "Закончился пакет токенов GigaChat — работаю локально: точность ниже.",
    "busy": "GigaChat перегружен или исчерпан лимит запросов — работаю локально: точность ниже.",
    "error": "GigaChat временно не отвечает — работаю локально: точность ниже.",
}
# Ключ может прийти от пользователя (заголовок X-LLM-Key, хранится только в его браузере) или из окружения сервера.
# Приоритет: ключ пользователя → ключ сервера (GIGACHAT_CREDENTIALS) → локальный режим.
# Состояние «предохранителя» — на каждый ключ.
_req_key: "contextvars.ContextVar[str]" = contextvars.ContextVar("gigachat_key", default="")
_states: dict = {}
_state_lock = threading.Lock()

_req_mode: "contextvars.ContextVar[str]" = contextvars.ContextVar("assistant_mode", default="auto")
MODES = ("auto", "online", "offline")      # auto — облако, при сбое локально; online — только облако; offline — только локально


def set_request_mode(mode: Optional[str]):
    return _req_mode.set(mode if mode in MODES else "auto")


def current_mode() -> str:
    return _req_mode.get()


def set_request_key(key: Optional[str]):
    return _req_key.set((key or "").strip()[:400])


def current_key() -> str:
    return _req_key.get() or config.GIGACHAT_CREDENTIALS


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
        _states.setdefault(_kid(), {}).update(reason=reason, detail=detail[:160],
                                              until=time.time() + _COOLDOWN.get(reason, 30), checked=True)
    log.warning("GigaChat недоступен (%s), ближайшие %s с работаю локально. Причина: %s",
                reason, _COOLDOWN.get(reason, 30), detail[:300])


def _mark_ok() -> None:
    with _state_lock:
        _states.setdefault(_kid(), {}).update(reason="", until=0.0, checked=True)


def status() -> dict:
    if current_mode() == "offline":
        return {"state": "offline",
                "message": "Офлайн-режим выбран вручную: всё обрабатывается на сервере без GigaChat, точность ниже."}
    if not has_key():
        return {"state": "no_key", "message": _MESSAGES["no_key"]}
    st = _state()
    if time.time() < st["until"]:
        r = st["reason"] or "error"
        return {"state": "down", "reason": r, "message": _MESSAGES.get(r, _MESSAGES["error"]),
                "detail": st.get("detail", "")}
    return {"state": "ok" if st["checked"] else "unknown", "message": ""}


def fallback_message() -> str:
    return status().get("message") or _MESSAGES["error"]


def _reason_of(err: Exception) -> str:
    if isinstance(err, LLMNetworkError):
        return "network"
    if isinstance(err, LLMQuotaError):
        return "quota"
    if isinstance(err, (LLMAuthError, LLMConfigError)):
        return "auth"
    return "busy" if str(err).startswith("busy") else "error"


# ---------- выбор модели ----------
_model: dict = {}          # ключ → модель, которая реально ответила (её и пробуем первой)
_no_schema: set = set()    # модели, которые не принимают response_format: json_schema
_dim_warned = False


def _model_queue(kid: str, model: Optional[str] = None) -> list:
    names = ([model] if model else []) + [_model.get(kid)] + [config.GIGACHAT_MODEL] + config.GIGACHAT_FALLBACK_MODELS
    out: list = []
    for name in names:
        if name and name not in out:
            out.append(name)
    return out


def current_model_name() -> str:
    return _model.get(_kid()) or config.GIGACHAT_MODEL


def _messages(contents, system: str) -> list:
    messages: list = []
    if system:
        messages.append({"role": "system", "content": system})
    if isinstance(contents, (str, dict)):
        contents = [contents]
    for part in contents:
        if isinstance(part, dict):
            messages.append(dict(part))
        else:
            messages.append({"role": "user", "content": str(part)})
    return messages


def _looks_schema_error(message: str) -> bool:
    low = message.lower()
    return "response_format" in low or "json_schema" in low or "schema" in low


def _looks_file_error(message: str) -> bool:
    low = message.lower()
    return any(w in low for w in ("attach", "file", "audio", "modality", "mime", "медиа"))


def _looks_model_error(message: str, status: Optional[int]) -> bool:
    low = message.lower()
    return status == 404 or ("model" in low and ("not found" in low or "unknown" in low or "not exist" in low))


def _generate(contents, system: str = "", json_mode: bool = True, schema: Optional[dict] = None,
              attachments: Optional[list] = None, model: Optional[str] = None,
              temperature: float = 0.1, max_tokens: Optional[int] = None) -> str:
    """Один запрос к GigaChat с перебором моделей. Возвращает текст ответа."""
    key = current_key()
    if not key:
        raise LLMConfigError("Не задан ключ GigaChat.")
    kid = _kid(key)
    messages = _messages(contents, system)
    queue = _model_queue(kid, model)
    last: Optional[Exception] = None
    while queue:
        name = queue.pop(0)
        for attempt in range(2):
            use_schema = schema if (json_mode and schema and config.GIGACHAT_JSON_SCHEMA and name not in _no_schema) else None
            try:
                text = gc.chat(
                    key, name, messages,
                    temperature=temperature,
                    json_schema=use_schema,
                    attachments=attachments,
                    max_tokens=max_tokens if max_tokens is not None else (config.MAX_RESPONSE_TOKENS if json_mode else None),
                    timeout=config.AUDIO_TIMEOUT_S if attachments else config.CLOUD_TIMEOUT_S,
                )
                _model[kid] = name
                _mark_ok()
                return text
            except gc.BadRequest as e:
                last = e
                if use_schema and _looks_schema_error(str(e)):
                    log.warning("Модель %s не приняла JSON-схему (%s) — повторяю без схемы", name, e)
                    _no_schema.add(name)
                    continue
                if attachments and _looks_file_error(str(e)):
                    if queue:                       # у этой модели нет аудио/файлов — пробуем следующую
                        log.warning("Модель %s не приняла файл (%s) — пробую следующую", name, e)
                        break
                    raise LLMAttachError(str(e)) from e
                if _looks_model_error(str(e), e.status):
                    log.warning("Модель %s недоступна: %s", name, e)
                    break
                raise LLMError(str(e)) from e
            except gc.BusyError as e:
                last = e
                if attempt == 0:
                    time.sleep(0.4)
                    continue
                break
            except gc.NetworkError as e:
                _mark_down("network", str(e))
                raise LLMNetworkError(str(e)) from e
            except gc.QuotaError as e:
                _mark_down("quota", str(e))
                raise LLMQuotaError(str(e)) from e
            except gc.AuthError as e:
                _mark_down("auth", str(e))
                raise LLMAuthError(str(e)) from e
            except gc.ConfigError as e:
                _mark_down("auth", str(e))          # ключ не задан/неверного формата — это не «сеть»
                raise LLMConfigError(str(e)) from e
            except gc.GigaChatError as e:
                last = e
                break
    if isinstance(last, gc.BusyError) or (isinstance(last, gc.GigaChatError) and last.status in (429, 500, 502, 503, 504)):
        _mark_down("busy", str(last))
        raise LLMError(f"busy: {last}")
    _mark_down("error", str(last))
    raise LLMError(str(last) or "GigaChat не ответил")


def probe() -> None:
    """Быстрая проверка доступности GigaChat (вызывается в фоне при старте, чтобы показать режим в интерфейсе)."""
    if not has_key():
        return
    try:
        _generate(["Ответь одним словом: ok"], "Отвечай одним словом.", json_mode=False)
    except LLMError as e:
        log.warning("Проверка GigaChat: %s", e)


def check_key(key: str) -> dict:
    """Проверка ключа, введённого пользователем. state: ok | auth (ключ отклонён) | down (не проверить: сеть/лимит)."""
    token = set_request_key(key)
    try:
        if not current_key():
            return {"state": "none", "message": "Ключ пустой."}
        try:
            gc.normalize(current_key())
        except gc.ConfigError as e:
            return {"state": "auth", "message": str(e)}
        try:
            _generate(["Ответь одним словом: ok"], "Отвечай одним словом.", json_mode=False)
            return {"state": "ok", "message": "Ключ GigaChat принят. Облачный режим включён."}
        except LLMAuthError:
            detail = status().get("detail", "")[:140]
            return {"state": "auth",
                    "message": "GigaChat отклонил ключ. Проверьте, что скопирован весь Authorization key"
                               + (f" ({detail})" if detail else "")}
        except LLMError:
            return {"state": "down", "message": status().get("message") or "Не удалось связаться с GigaChat."}
    finally:
        _req_key.reset(token)


# ---------- промпт ----------
def _system_prompt(now: datetime) -> str:
    return f"""Ты — модуль понимания речи голосового ассистента личных задач, напоминаний и встреч.
Ты ничего не выполняешь: только определяешь намерение и извлекаешь параметры.
Отвечай СТРОГО одним JSON-объектом, без markdown и пояснений.

Сейчас: {now:%Y-%m-%d %H:%M}, {WEEKDAYS[now.weekday()]}.
{calendar_hint(now)}

Формат ответа (неиспользуемые поля = пустая строка):
{{"intent": "create|list|search|complete|reopen|delete|update|clarify|other", "transcript": "...", "kind": "task|meeting|reminder|",
  "title": "...", "date": "YYYY-MM-DD", "time": "HH:MM", "range_start": "YYYY-MM-DD", "range_end": "YYYY-MM-DD",
  "query": "...", "ref": "last|it|all|all_done|ord:N|", "repeat": "daily|weekdays|weekly|monthly|", "reply": "..."}}

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
   - ref (для delete/update/complete/reopen, когда запись названа не по смыслу): "last" — «последнюю запись/заметку», "it" — «её / эту / его» (запись, о которой только что шла речь), "all" — «все» («удали все», «отметь все задачи выполненными»; если названы слова записи — они в query, а kind ограничивает тип), "all_done" — «все выполненные» (для delete), "ord:N" — N-я запись из только что показанного списка («удали первую» → "ord:1", «отметь третью» → "ord:3"); query тогда пустая;
   - repeat (только для create): «каждый день»/«ежедневно» — daily, «по будням» — weekdays, «каждый понедельник»/«каждую неделю» — weekly (date = ближайший такой день), «каждый месяц» — monthly; иначе пустая строка. Слова повтора в title не включай;
   - clarify — для создания не хватает обязательных данных; в reply задай ОДИН короткий вопрос;
   - other — приветствие, благодарность, не относится к задачам; в reply — короткий вежливый ответ и подсказка, что ты умеешь.
3. kind: meeting — встреча, созвон, приём, визит (к врачу, стоматологу и т.п.); reminder — «напомни…»; task — остальные дела («нужно», «поставь задачу», «купить», «отправить»).
   Для list/search/complete заполняй kind ТОЛЬКО если пользователь явно ограничил тип («мои встречи», «какие задачи»), иначе пустая строка.
4. title — краткая суть без даты, времени и слов-команд, с заглавной буквы: «Позвонить Ивану», «Встреча с дизайнером», «Приём у стоматолога Олега». Ничего не выдумывай.
5. Даты и время:
   - date в формате YYYY-MM-DD, time — 24-часовой HH:MM. Используй календарь выше.
   - «утра» = AM, «вечера»/«дня» = PM («в 7 вечера» = 19:00). Без уточнения: 8–11 → утро, 12 → полдень, 1–7 → 13:00–19:00.
   - «через два часа / 30 минут» — прибавь к текущему времени (обнови и date, если перешли на другой день).
   - «в пятницу» — ближайшая такая дата начиная с сегодня; если сегодня как раз этот день недели и не сказано «сегодня» — на следующей неделе.
   - «до конца недели» — date = ближайшее воскресенье, time = пустая. «На следующей неделе» для создания без дня — понедельник следующей недели.
6. Для create: у задачи обязателен только title (дата/время по желанию). У встречи и напоминания обязательны title, date и time — если чего-то нет, intent="clarify" и спроси недостающее («На какое время поставить встречу?»).
7. Диалог: если предыдущий вопрос ассистента был уточняющим, а новая реплика отвечает на него («в три», «завтра», «в 15:00») — объедини с ранее названным и верни intent="create" с полными данными.
8. list: заполни range_start и range_end (включительно). «Сегодня» — оба = сегодня; «на этой неделе» — от сегодня до воскресенья; «на следующей неделе» — понедельник–воскресенье следующей недели; «в пятницу» — оба = эта дата. Если период не назван («покажи все задачи», «какие у меня встречи?», «что у меня есть?») — range_start = "all", range_end = пустая. «В прошлом месяце», «в ноябре» — первый и последний день этого месяца.
9. search: query — ключевые слова по смыслу в начальной форме (имена, темы), без служебных слов и без слов «задача/встреча».
10. reply заполняй только для clarify и other. Пиши по-русски, коротко."""


# Все поля обязательны и строковые: GigaChat со strict=true вернёт их все, а Command.from_dict
# отбросит пустые. Так ответ стабильно разбирается без «почти-JSON».
COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["create", "list", "search", "complete", "reopen", "delete",
                                              "update", "clarify", "other"]},
        "transcript": {"type": "string", "description": "дословная расшифровка речи (для аудио) или исходный текст"},
        "kind": {"type": "string", "enum": ["task", "meeting", "reminder", ""], "description": "тип записи"},
        "title": {"type": "string", "description": "краткая суть записи без даты и времени"},
        "date": {"type": "string", "description": "дата YYYY-MM-DD или пустая строка"},
        "time": {"type": "string", "description": "время HH:MM или пустая строка"},
        "range_start": {"type": "string", "description": "начало периода для list, YYYY-MM-DD или all"},
        "range_end": {"type": "string", "description": "конец периода для list, YYYY-MM-DD или пустая строка"},
        "query": {"type": "string", "description": "ключевые слова записи для поиска/правки/удаления"},
        "ref": {"type": "string", "description": "last, it, all, all_done, ord:N или пустая строка"},
        "repeat": {"type": "string", "enum": ["daily", "weekdays", "weekly", "monthly", ""]},
        "reply": {"type": "string", "description": "вопрос при clarify или короткий ответ при other"},
    },
    "required": ["intent", "transcript", "kind", "title", "date", "time", "range_start", "range_end",
                 "query", "ref", "repeat", "reply"],
    "additionalProperties": True,
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


# ---------- речь ----------
_AUDIO_EXT = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/x-pn-wav": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "mp4", "audio/x-m4a": "m4a",
    "audio/webm": "weba", "audio/ogg": "ogg", "audio/opus": "opus",
}
_AUDIO_PROMPT = ("Новый запрос пользователя — аудиозапись (русская речь). "
                 "Расшифруй её в поле transcript и обработай по правилам.")


def _upload_audio(audio: bytes, mime: str) -> Optional[str]:
    """Загружает запись в хранилище GigaChat. None — если загрузка недоступна (тогда сработает локальный Whisper)."""
    mime = (mime or "audio/wav").split(";")[0].strip().lower() or "audio/wav"
    name = "voice." + _AUDIO_EXT.get(mime, "wav")
    try:
        return gc.upload(current_key(), audio, name, mime)
    except gc.BadRequest as e:
        # формат не принят (например, браузер прислал webm без поддержки) — пусть решает локальный STT
        log.warning("GigaChat не принял аудио (%s) — попробую локальное распознавание", e)
        return None
    except gc.BusyError as e:
        _mark_down("busy", str(e))
        raise LLMError(f"busy: {e}") from e
    except gc.NetworkError as e:
        _mark_down("network", str(e))
        raise LLMNetworkError(str(e)) from e
    except gc.QuotaError as e:
        _mark_down("quota", str(e))
        raise LLMQuotaError(str(e)) from e
    except gc.AuthError as e:
        _mark_down("auth", str(e))
        raise LLMAuthError(str(e)) from e
    except gc.ConfigError as e:
        _mark_down("auth", str(e))
        raise LLMConfigError(str(e)) from e
    except gc.GigaChatError as e:
        _mark_down("error", str(e))
        raise LLMError(str(e)) from e


def _delete_quietly(file_id: str) -> None:
    try:
        gc.delete_file(current_key(), file_id)
    except Exception as e:  # noqa: BLE001 — уборка не должна ломать ответ
        log.info("Файл %s остался в хранилище GigaChat: %s", file_id, e)


def transcribe_file(file_id: str) -> str:
    """Отдельная расшифровка аудио (страховка, если модель не смогла ответить JSON по записи)."""
    text = _generate(
        [{"role": "user", "content": "Дословно расшифруй русскую речь из аудиозаписи. "
                                     "Верни только текст расшифровки, без пояснений, кавычек и markdown."}],
        "", json_mode=False, attachments=[file_id],
        model=config.GIGACHAT_AUDIO_MODEL or config.GIGACHAT_MODEL, temperature=0.0, max_tokens=1024,
    )
    text = text.strip().strip('"').strip("«»").strip()
    if len(text) > 2000:
        text = text[:2000]
    return text


def _parse_messages(messages: list, system: str, attachments: Optional[list], json_mode: bool = True) -> Command:
    try:
        raw = _generate(messages, system, json_mode, COMMAND_SCHEMA, attachments)
    except LLMAttachError:
        raise
    try:
        return Command.from_dict(_extract_json(raw))
    except LLMError as e:
        log.warning("GigaChat вернул не JSON (%s) — повторяю с явным требованием формата", e)
        strict = system + "\n\nВАЖНО: ответ — ровно один JSON-объект по указанному формату, без markdown и текста вокруг."
        raw = _generate(messages, strict, json_mode, None, attachments)
        return Command.from_dict(_extract_json(raw))


def _prompt(text: str) -> str:
    return f"Новый запрос пользователя: {text}"


def parse(now: datetime, history: list, text: Optional[str] = None,
          audio: Optional[bytes] = None, mime: str = "audio/wav") -> Command:
    """history — список (role, message), role = 'user' | 'assistant'."""
    system = _system_prompt(now)
    base = [{"role": "user" if r == "user" else "assistant", "content": m} for r, m in history]

    if not audio:
        return _parse_messages(base + [{"role": "user", "content": _prompt(text or "")}], system, None)

    file_id = _upload_audio(audio, mime)
    try:
        if file_id:
            try:
                cmd = _parse_messages(base + [{"role": "user", "content": _AUDIO_PROMPT}], system, [file_id])
                if cmd.transcript or cmd.intent not in ("other", "clarify") or cmd.reply:
                    return cmd
                log.info("GigaChat не вернул расшифровку — расшифровываю отдельным запросом")
            except (LLMNetworkError, LLMAuthError, LLMConfigError):
                raise                      # сеть/ключ — сразу локальный режим, без второго ожидания
            except (LLMAttachError, LLMError) as e:
                log.warning("GigaChat: аудио+JSON не сработали (%s) — расшифровываю отдельно", e)
            heard = transcribe_file(file_id)
            if not heard:
                raise LLMError("Не удалось распознать речь в записи")
            cmd = _parse_messages(base + [{"role": "user", "content": _prompt(heard)}], system, None)
            if not cmd.transcript:
                cmd.transcript = heard
            return cmd
        raise LLMAttachError("Аудио не загрузилось в GigaChat")
    finally:
        if file_id:
            _delete_quietly(file_id)


def embed(texts: list, query: bool = False) -> list:
    """Эмбеддинги GigaChat для семантического поиска. Ошибки сети переводят ассистента в локальный режим."""
    if not texts:
        return []
    try:
        vectors = gc.embed(current_key(), config.GIGACHAT_EMBED_MODEL, list(texts))
    except gc.ConfigError as e:
        raise LLMConfigError(str(e)) from e
    except gc.QuotaError as e:
        _mark_down("quota", str(e))
        raise LLMQuotaError(str(e)) from e
    except gc.AuthError as e:
        _mark_down("auth", str(e))
        raise LLMAuthError(str(e)) from e
    except gc.NetworkError as e:
        _mark_down("network", str(e))
        raise LLMNetworkError(str(e)) from e
    except gc.BusyError as e:
        raise LLMError(f"busy: {e}") from e
    except gc.GigaChatError as e:
        raise LLMError(str(e)) from e
    global _dim_warned
    if vectors and config.EMBED_DIM and len(vectors[0]) != config.EMBED_DIM and not _dim_warned:
        _dim_warned = True
        log.info("Размерность эмбеддингов GigaChat %s (ожидалась %s) — поиск работает, порог подобран по факту",
                 len(vectors[0]), config.EMBED_DIM)
    return vectors
