"""Бизнес-логика ассистента: понимание запроса (облако или локально) → проверки → хранилище → ответ.

Все обязательные проверки (хватает ли данных, не дубль ли, не в прошлом ли) — здесь, в коде, а не в LLM,
поэтому поведение предсказуемо и тестируется. Режимы:
  • облачный: Gemini (речь + смысл одним вызовом);
  • локальный: faster-whisper + правила (localnlu) — включается автоматически, если Gemini недоступен.
"""
import logging
import re
import threading
from collections import OrderedDict, deque
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config
import llm
import localnlu
import search
import storage
import stt_local
from command import Command, KINDS
from textutil import parse_date, parse_time, period_phrase, human_date, short_date, when_phrase, WEEKDAYS_SHORT

log = logging.getLogger("assistant")

KIND_LABEL = {"task": "задача", "meeting": "встреча", "reminder": "напоминание"}
KIND_FILTER = {"task": ("task", "reminder"), "meeting": ("meeting",), "reminder": ("reminder",)}
HELP = ("Я умею добавлять задачи, напоминания и встречи, показывать планы на любой период, искать старые записи, "
        "переносить и удалять их, отмечать задачи выполненными.")


class OnlineUnavailable(Exception):
    """Выбран строго онлайн-режим, а Gemini недоступен: молча уходить в офлайн нельзя."""


def _tz(name: Optional[str]):
    for candidate in (name, config.DEFAULT_TZ):
        try:
            if candidate:
                return ZoneInfo(candidate)
        except Exception:
            continue
    return timezone.utc


class Session:
    def __init__(self):
        self.history: deque = deque(maxlen=config.HISTORY_TURNS * 2)
        self.pending: Optional[dict] = None       # недостроенная запись после уточняющего вопроса
        self.last_id: Optional[int] = None        # последняя запись, о которой шла речь («перенеси её на завтра»)


class Assistant:
    def __init__(self):
        self._sessions: "OrderedDict[str, Session]" = OrderedDict()
        self._lock = threading.Lock()

    def _session(self, sid: str) -> Session:
        with self._lock:
            s = self._sessions.get(sid)
            if s is None:
                s = self._sessions[sid] = Session()
                while len(self._sessions) > config.MAX_SESSIONS:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(sid)
            return s

    # ---------- понимание запроса ----------
    def _understand(self, sess: Session, now: datetime, text, audio, mime):
        """Возвращает (команда, расшифровка, режим 'cloud'|'local'). Бросает stt_local.STTUnavailable."""
        mode = llm.current_mode()
        if mode == "online" and not llm.has_key():
            raise OnlineUnavailable("Выбран онлайн-режим, но ключ Gemini не задан. Введите ключ (кнопка «🔑 Ключ») или выберите режим «Авто» или «Офлайн».")
        if mode == "online" and not llm.usable():
            raise OnlineUnavailable("Онлайн-режим: Gemini сейчас недоступен. " + llm.fallback_message() + " Подождите немного или выберите «Авто» или «Офлайн».")
        if llm.usable():
            try:
                cmd = llm.parse(now, list(sess.history), text=text, audio=audio, mime=mime)
                return cmd, (cmd.transcript or text or "").strip(), "cloud"
            except llm.LLMError as e:
                if mode == "online":
                    raise OnlineUnavailable("Онлайн-режим: Gemini не ответил. " + llm.fallback_message() + " Выберите «Авто» или «Офлайн».") from e
                log.warning("Облако не сработало (%s) — перехожу на локальный режим", e)
        if audio:
            heard = stt_local.transcribe(audio, mime)
        else:
            heard = text or ""
        cmd = localnlu.parse(heard, now, sess.pending)
        cmd.transcript = heard
        return cmd, heard, "local"

    # ---------- точка входа ----------
    def handle(self, session_id: str, tz_name: Optional[str] = None, text: Optional[str] = None,
               audio: Optional[bytes] = None, mime: str = "audio/wav") -> dict:
        now = datetime.now(_tz(tz_name))
        sess = self._session(session_id)
        try:
            cmd, heard, engine = self._understand(sess, now, text, audio, mime)
        except OnlineUnavailable as e:
            return {"ok": False, "transcript": "", "intent": "error", "engine": "cloud", "reply": str(e)}
        except stt_local.STTUnavailable as e:
            if llm.current_mode() == "offline":
                return {"ok": False, "transcript": "", "intent": "error", "engine": "local",
                        "reply": f"Офлайн-распознавание речи не готово ({e}). Напишите запрос текстом или выберите режим «Авто»."}
            return {"ok": False, "transcript": "", "intent": "error", "engine": "local",
                    "reply": ("Не удалось распознать речь: облако (Gemini) сейчас недоступно. " + llm.fallback_message()
                              + " Напишите запрос текстом.") if config.MULTI_USER else
                             f"Облачное распознавание сейчас недоступно, а локальное не готово ({e}). Напишите запрос текстом."}
        except Exception:
            log.exception("Ошибка понимания запроса")
            return {"ok": False, "transcript": "", "intent": "error", "engine": "local",
                    "reply": "Не удалось обработать запрос. Попробуйте ещё раз или напишите текстом."}

        out_extra = {"engine": engine}
        if engine == "local":
            out_extra["notice"] = llm.fallback_message()
        if audio and not heard:
            return {"ok": True, "transcript": "", "intent": "other", "reply": "Я ничего не расслышал. Повторите, пожалуйста.",
                    **out_extra}
        if cmd.intent != "clarify":      # при переспросе незаконченная запись остаётся в памяти диалога
            sess.pending = None
        try:
            reply, intent = self._dispatch(cmd, heard, now, session_id, sess)
        except (storage.StorageError, ValueError) as e:
            reply, intent = str(e), "error"
        except Exception:
            log.exception("Ошибка выполнения действия")
            reply, intent = "Что-то пошло не так при выполнении. Попробуйте переформулировать запрос.", "error"

        sess.history.append(("user", heard))
        sess.history.append(("assistant", reply))
        return {"ok": intent != "error", "transcript": heard, "reply": reply, "intent": intent, **out_extra}

    def _dispatch(self, cmd: Command, heard: str, now: datetime, user: str, sess: Session):
        if cmd.intent == "create":
            return self._create(cmd, now, user, sess)
        if cmd.intent == "list":
            return self._list(cmd, now, user), "list"
        if cmd.intent == "search":
            return self._search(cmd, heard, now, user, sess), "search"
        if cmd.intent == "complete":
            return self._complete(cmd, heard, now, user, sess), "complete"
        if cmd.intent == "delete":
            return self._delete(cmd, now, user, sess), "delete"
        if cmd.intent == "update":
            return self._update(cmd, now, user, sess)
        if cmd.intent == "clarify":
            return cmd.reply or "Уточните, пожалуйста, детали.", "clarify"
        return cmd.reply or HELP, "other"

    # ---------- создание ----------
    def _create(self, cmd: Command, now: datetime, user: str, sess: Session):
        today = now.date()
        kind = cmd.kind or "task"
        title = (cmd.title or "").strip(" .,!?")
        if not title:
            return "Что именно нужно записать?", "clarify"
        d, t = parse_date(cmd.date), parse_time(cmd.time)
        label = KIND_LABEL[kind]

        if kind in ("meeting", "reminder"):
            noun = "встречу" if kind == "meeting" else "напоминание"
            partial = {"kind": kind, "title": title, "date": d.isoformat() if d else None, "time": t}
            if not d and not t:
                sess.pending = partial
                return f"На какой день и время поставить {noun}?", "clarify"
            if not d:
                sess.pending = partial
                return "На какой день?", "clarify"
            if not t:
                sess.pending = partial
                return f"На какое время поставить {noun}?", "clarify"
            if datetime.combine(d, datetime.strptime(t, "%H:%M").time(), tzinfo=now.tzinfo) < now.replace(second=0, microsecond=0):
                return "Это время уже прошло. На какую дату и время поставить?", "clarify"

        dd = d.isoformat() if d else None
        if storage.find_duplicate(user, kind, title, dd, t):
            return f"Такая {label} уже есть: «{title}»" + (f" — {when_phrase(d, t, today)}." if d or t else "."), "create"

        conflicts = storage.find_meetings_at(user, dd, t) if kind == "meeting" and dd and t else []
        added = storage.add_item(user, kind, title, dd, t, now.isoformat(timespec="seconds"))
        sess.last_id = added.get("id")
        when = when_phrase(d, t, today)
        reply = f"Записано: {label} «{title}»" + (f" — {when}." if when else ".")
        if conflicts:
            reply += f"\nВнимание: в это время уже есть встреча «{conflicts[0]['title']}»."
        return reply, "create"

    # ---------- список ----------
    def _list(self, cmd: Command, now: datetime, user: str) -> str:
        today = now.date()
        start = parse_date(cmd.range_start) or parse_date(cmd.date) or today
        end = parse_date(cmd.range_end) or start
        if end < start:
            start, end = end, start
        kinds = KIND_FILTER.get(cmd.kind or "")
        rows = storage.list_range(user, start.isoformat(), end.isoformat(), kinds)
        phrase = period_phrase(start, end, today)
        what = {"meeting": "Встреч", "task": "Задач"}.get(cmd.kind or "", "Записей")
        if not rows:
            return f"{what} {phrase} нет."
        multi = start != end
        lines = [self._line(r, today, with_date=multi) for r in rows]
        return f"{what} {phrase}: {len(rows)}.\n" + "\n".join(lines)

    @staticmethod
    def _line(r: dict, today, with_date: bool = False, full_date: bool = False) -> str:
        d = parse_date(r["due_date"])
        t = r["due_time"] or ("без времени" if d else "без срока")
        if full_date and d:
            prefix = f"{human_date(d, today)}, {t}"
        elif with_date and d:
            prefix = f"{WEEKDAYS_SHORT[d.weekday()]} {short_date(d, today)}, {t}"
        else:
            prefix = t
        done = " — выполнено" if r.get("status") == "done" else ""
        return f"• {prefix} — {r['title']} ({KIND_LABEL[r['kind']]}){done}"

    # ---------- поиск ----------
    def search_items(self, user: str, query: str, kind: Optional[str] = None) -> tuple:
        pool = storage.all_items(user, KIND_FILTER.get(kind or ""))
        found, mode = search.find(query, pool)
        if found and mode == "exact":
            found = sorted(found, key=lambda r: (r["due_date"] or "9999", r["due_time"] or "")) if query.strip() else found
        return found, mode

    def _search(self, cmd: Command, heard: str, now: datetime, user: str, sess: Optional[Session] = None) -> str:
        today = now.date()
        if not storage.all_items(user):
            return "Пока нет ни одной записи."
        found, mode = self.search_items(user, cmd.query or heard, cmd.kind)
        if not found:
            return "Ничего похожего не нашёл. Попробуйте назвать другое слово из записи."
        lines = [self._line(r, today, full_date=True) for r in found]
        if sess is not None and len(found) == 1:
            sess.last_id = found[0]["id"]
        if mode == "semantic":
            head = "Точных совпадений нет. Возможно, вы имели в виду:"
        else:
            head = "Нашёл одну запись:" if len(found) == 1 else f"Нашёл записей: {len(found)}."
        return head + "\n" + "\n".join(lines)

    # ---------- отметка выполненной ----------
    def _complete(self, cmd: Command, heard: str, now: datetime, user: str, sess: Optional[Session] = None) -> str:
        pool = [r for r in storage.all_items(user, KIND_FILTER.get(cmd.kind or "")) if r["status"] != "done"]
        if not pool:
            return "Невыполненных записей нет."
        if not cmd.query and sess is not None and sess.last_id:      # «отметь её выполненной»
            found = [r for r in pool if r["id"] == sess.last_id]
            if found:
                storage.update(user, found[0]["id"], status="done")
                return f"Отметил выполненной: {KIND_LABEL[found[0]['kind']]} «{found[0]['title']}»."
        found, _mode = search.find(cmd.query or "", pool, limit=5)
        if not cmd.query or not found:
            return "Не нашёл такую невыполненную запись. Назовите её по-другому."
        if len(found) > 1:
            lines = [self._line(r, now.date(), full_date=True) for r in found]
            return "Нашёл несколько подходящих записей, какую отметить? Уточните название.\n" + "\n".join(lines)
        item = found[0]
        if sess is not None:
            sess.last_id = item["id"]
        storage.update(user, item["id"], status="done")
        return f"Отметил выполненной: {KIND_LABEL[item['kind']]} «{item['title']}»."


    # ---------- удаление и перенос голосом ----------
    def _resolve(self, cmd: Command, now: datetime, user: str, sess: Session):
        """Находит запись, о которой речь. Возвращает (запись, None) или (None, ответ-уточнение).
        Для необратимых действий принимаем только совпадение по словам, а не «по смыслу»."""
        pool = storage.all_items(user, KIND_FILTER.get(cmd.kind or ""))
        if not pool:
            return None, "Записей пока нет."
        if cmd.query:
            found, mode = search.find(cmd.query, pool, limit=5)
            if not found:
                return None, "Не нашёл такую запись. Назовите её по-другому."
            if mode != "exact":
                lines = [self._line(r, now.date(), full_date=True) for r in found]
                return None, "Точного совпадения нет. Возможно, вы имели в виду:\n" + "\n".join(lines) + "\nУточните название."
            if len(found) > 1:
                lines = [self._line(r, now.date(), full_date=True) for r in found]
                return None, "Нашёл несколько подходящих записей, какую выбрать? Уточните название.\n" + "\n".join(lines)
            return found[0], None
        if cmd.ref == "last":
            return max(pool, key=lambda r: r["id"]), None
        item = storage.get(user, sess.last_id) if sess.last_id else None
        if item is None:
            return None, "О какой записи речь? Назовите её, например: «задачу про договор»."
        return item, None

    def _delete(self, cmd: Command, now: datetime, user: str, sess: Session) -> str:
        item, ask = self._resolve(cmd, now, user, sess)
        if ask:
            return ask
        storage.delete(user, item["id"])
        if sess.last_id == item["id"]:
            sess.last_id = None
        return f"Удалил: {KIND_LABEL[item['kind']]} «{item['title']}»."

    def _update(self, cmd: Command, now: datetime, user: str, sess: Session):
        item, ask = self._resolve(cmd, now, user, sess)
        if ask:
            return ask, "clarify"
        sess.last_id = item["id"]
        d, t = parse_date(cmd.date), parse_time(cmd.time)
        if not d and not t:
            sess.pending = {"op": "move", "id": item["id"]}
            return f"На какую дату и время перенести {KIND_LABEL[item['kind']]} «{item['title']}»?", "clarify"
        data = {}
        if d:
            data["date"] = d.isoformat()
        if t:
            data["time"] = t
        if t and not d and not item["due_date"]:
            data["date"] = now.date().isoformat()      # «перенеси на 10 утра» без даты у записи → сегодня
        new_d = parse_date(data.get("date")) or parse_date(item["due_date"])
        new_t = data.get("time") or item["due_time"]
        if new_d and new_t and datetime.combine(new_d, datetime.strptime(new_t, "%H:%M").time(), tzinfo=now.tzinfo) < now.replace(second=0, microsecond=0):
            return "Это время уже прошло. На какую дату и время перенести?", "clarify"
        updated = self.update_item(user, item["id"], data)
        reply = f"Перенёс: {KIND_LABEL[updated['kind']]} «{updated['title']}» — {when_phrase(new_d, new_t, now.date())}."
        if updated["kind"] == "meeting" and new_d and new_t:
            others = [m for m in storage.find_meetings_at(user, new_d.isoformat(), new_t) if m["id"] != item["id"]]
            if others:
                reply += f"\nВнимание: в это время уже есть встреча «{others[0]['title']}»."
        return reply, "update"

    # ---------- правка и удаление (сайт) ----------
    def update_item(self, user: str, item_id: int, data: dict) -> dict:
        """Валидация и сохранение правки записи. ValueError — ошибка в данных, KeyError — записи нет."""
        cur = storage.get(user, item_id)
        if cur is None:
            raise KeyError(item_id)
        changes: dict = {}
        if "title" in data:
            title = str(data["title"] or "").strip()
            if not title:
                raise ValueError("Название не может быть пустым.")
            changes["title"] = title[:200]
        if "kind" in data:
            if data["kind"] not in KINDS:
                raise ValueError("Неизвестный тип записи.")
            changes["kind"] = data["kind"]
        if "date" in data:
            raw = str(data["date"] or "").strip()
            if raw and not parse_date(raw):
                raise ValueError("Некорректная дата.")
            changes["due_date"] = parse_date(raw).isoformat() if raw else None
        if "time" in data:
            raw = str(data["time"] or "").strip()
            if raw and not parse_time(raw):
                raise ValueError("Некорректное время.")
            changes["due_time"] = parse_time(raw) if raw else None
        if "status" in data:
            if data["status"] not in storage.STATUSES:
                raise ValueError("Неизвестный статус.")
            changes["status"] = data["status"]
        kind = changes.get("kind", cur["kind"])
        due_date = changes.get("due_date", cur["due_date"])
        due_time = changes.get("due_time", cur["due_time"])
        if kind in ("meeting", "reminder") and not (due_date and due_time):
            raise ValueError("Для встречи и напоминания нужны дата и время.")
        if due_time and not due_date:
            raise ValueError("Укажите дату вместе со временем.")
        return storage.update(user, item_id, **changes)

    def delete_item(self, user: str, item_id: int) -> None:
        if not storage.delete(user, item_id):
            raise KeyError(item_id)
