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
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config
import llm
import localnlu
import search
import storage
import stt_local
from command import Command, KINDS
from textutil import (parse_date, parse_time, period_phrase, human_date, short_date, when_phrase, WEEKDAYS_SHORT,
                      REPEAT_COUNT, repeat_dates, repeat_phrase)

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
        self.last_list: list = []                 # id записей из последнего показанного списка («удали первую»)
        self.choice: Optional[dict] = None        # ждём выбор из списка похожих записей / подтверждение «удалить все»


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
        if sess.choice and text:                                   # ответ на «какую именно?» не нужно отправлять в LLM
            res = self._choice_step(sess, text, now, session_id)
            if res:
                return self._choice_result(text, res)
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
        if sess.choice and audio and heard:
            res = self._choice_step(sess, heard, now, session_id)
            if res:
                return self._choice_result(heard, res)
        if cmd.intent != "clarify":      # при переспросе незаконченная запись остаётся в памяти диалога
            sess.pending = None
            if cmd.intent != "other" or cmd.reply:
                sess.choice = None
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
            return self._list(cmd, now, user, sess), "list"
        if cmd.intent == "search":
            return self._search(cmd, heard, now, user, sess), "search"
        if cmd.intent == "complete":
            return self._complete(cmd, heard, now, user, sess), "complete"
        if cmd.intent == "reopen":
            return self._complete(cmd, heard, now, user, sess, done=False), "complete"
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
        repeat = cmd.repeat if cmd.repeat in REPEAT_COUNT else None
        if repeat and not d:                       # «каждый день …» без даты — начинаем с ближайшего подходящего дня
            d = today if (not t or t > now.strftime("%H:%M")) else today + timedelta(days=1)

        if kind in ("meeting", "reminder"):
            noun = "встречу" if kind == "meeting" else "напоминание"
            partial = {"kind": kind, "title": title, "date": d.isoformat() if d else None, "time": t, "repeat": repeat}
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

        if repeat:
            return self._create_repeating(kind, title, d, t, repeat, now, user, sess)

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

    def _create_repeating(self, kind: str, title: str, start, t: Optional[str], repeat: str, now: datetime, user: str, sess: Session):
        """Повторяющаяся запись = серия обычных записей на ближайший период."""
        label = KIND_LABEL[kind]
        dates = repeat_dates(start, repeat)
        added = skipped = 0
        conflict = None
        first_id = None
        for d in dates:
            dd = d.isoformat()
            if storage.find_duplicate(user, kind, title, dd, t):
                skipped += 1
                continue
            if kind == "meeting" and t and conflict is None:
                found = storage.find_meetings_at(user, dd, t)
                if found:
                    conflict = (d, found[0]["title"])
            row = storage.add_item(user, kind, title, dd, t, now.isoformat(timespec="seconds"))
            first_id = first_id or row.get("id")
            added += 1
        sess.last_id = first_id or sess.last_id
        if not added:
            return f"{'Такое повторяющееся' if kind == 'reminder' else 'Такая повторяющаяся'} {label} уже есть: «{title}».", "create"
        every = repeat_phrase(repeat, dates[0]) + (f" в {t}" if t else "")
        adj = "повторяющееся" if kind == "reminder" else "повторяющаяся"
        reply = (f"Записано: {adj} {label} «{title}» — {every}. Создано записей: {added}, "
                 f"до {short_date(dates[-1], now.date())}.")
        if skipped:
            reply += f" Уже были: {skipped}."
        if conflict:
            reply += f"\nВнимание: {human_date(conflict[0], now.date())} в это время уже есть встреча «{conflict[1]}»."
        return reply, "create"

    # ---------- список ----------
    def _list(self, cmd: Command, now: datetime, user: str, sess: Optional[Session] = None) -> str:
        today = now.date()
        kinds = KIND_FILTER.get(cmd.kind or "")
        if (cmd.range_start or "").lower() == "all":              # период не назван — показываем всё
            rows = storage.all_items(user, kinds)
            what = {"meeting": "встречи", "task": "задачи", "reminder": "напоминания"}.get(cmd.kind or "", "записи")
            if not rows:
                return f"Пока нет ни одной записи." if not cmd.kind else f"Пока нет ни одной записи такого типа."
            shown = rows[:30]
            if sess is not None:
                sess.last_list = [r["id"] for r in shown]
                sess.last_id = shown[0]["id"] if len(shown) == 1 else None      # после списка «её» неоднозначно
            lines = [self._line(r, today, with_date=True, num=i) for i, r in enumerate(shown, 1)]
            more = f"\n…и ещё {len(rows) - len(shown)}." if len(rows) > len(shown) else ""
            return f"Все {what}: {len(rows)}.\n" + "\n".join(lines) + more
        start = parse_date(cmd.range_start) or parse_date(cmd.date) or today
        end = parse_date(cmd.range_end) or start
        if end < start:
            start, end = end, start
        rows = storage.list_range(user, start.isoformat(), end.isoformat(), kinds)
        phrase = period_phrase(start, end, today)
        what = {"meeting": "Встреч", "task": "Задач"}.get(cmd.kind or "", "Записей")
        if not rows:
            return f"{what} {phrase} нет."
        multi = start != end
        if sess is not None:
            sess.last_list = [r["id"] for r in rows]
            sess.last_id = rows[0]["id"] if len(rows) == 1 else None
        lines = [self._line(r, today, with_date=multi, num=i) for i, r in enumerate(rows, 1)]
        return f"{what} {phrase}: {len(rows)}.\n" + "\n".join(lines)

    @staticmethod
    def _line(r: dict, today, with_date: bool = False, full_date: bool = False, num: Optional[int] = None) -> str:
        d = parse_date(r["due_date"])
        t = r["due_time"] or ("без времени" if d else "без срока")
        if full_date and d:
            prefix = f"{human_date(d, today)}, {t}"
        elif with_date and d:
            prefix = f"{WEEKDAYS_SHORT[d.weekday()]} {short_date(d, today)}, {t}"
        else:
            prefix = t
        done = " — выполнено" if r.get("status") == "done" else ""
        return (f"{num}. " if num else "• ") + f"{prefix} — {r['title']} ({KIND_LABEL[r['kind']]}){done}"

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
        lines = [self._line(r, today, full_date=True, num=i) for i, r in enumerate(found, 1)]
        if sess is not None:
            sess.last_list = [r["id"] for r in found]
            if len(found) == 1:
                sess.last_id = found[0]["id"]
        if mode == "semantic":
            head = "Точных совпадений нет. Возможно, вы имели в виду:"
        else:
            head = "Нашёл одну запись:" if len(found) == 1 else f"Нашёл записей: {len(found)}."
        return head + "\n" + "\n".join(lines)

    # ---------- выбор из списка ----------
    def _ask_choice(self, sess: Optional[Session], op: str, rows: list, now: datetime, head: str, cmd: Optional[Command] = None,
                    allow_all: bool = True) -> str:
        """Показывает пронумерованный список и запоминает, что ждём номер."""
        rows = rows[:8]
        lines = [self._line(r, now.date(), full_date=True, num=i) for i, r in enumerate(rows, 1)]
        if sess is not None:
            sess.choice = {"op": op, "ids": [r["id"] for r in rows], "date": cmd.date if cmd else None,
                           "time": cmd.time if cmd else None, "all": allow_all}
            sess.last_list = [r["id"] for r in rows]
        tail = "Назовите номер, например «первую» или «2»" + (", или скажите «все»." if allow_all and len(rows) > 1 else ".")
        return head + "\n" + "\n".join(lines) + "\n" + tail

    def _ord_item(self, cmd: Command, sess: Optional[Session], user: str):
        """«Первую», «номер 2» → запись из последнего показанного списка. Возвращает (запись, None) или (None, ответ)."""
        k = int(cmd.ref.split(":", 1)[1])
        ids = sess.last_list if sess is not None else []
        if not ids:
            return None, "Сначала попросите показать список, например «Покажи все задачи», и назовите номер."
        if not 1 <= k <= len(ids):
            return None, f"В списке {len(ids)} записей, номера от 1 до {len(ids)}."
        item = storage.get(user, ids[k - 1])
        if item is None:
            return None, "Эта запись уже изменилась. Покажите список заново."
        return item, None

    def _choice_step(self, sess: Session, heard: str, now: datetime, user: str):
        """Пользователь отвечает на «какую именно?» или «удалить все?». Возвращает (ответ, intent) или None, если это новая команда."""
        ch = sess.choice
        n = " ".join(heard.lower().replace("ё", "е").split())
        if not n:
            return None
        if re.match(r"^(?:нет|отмена|отмени\w*|не\s+надо|не\s+нужно|стоп|не\s+удаляй)\b", n):
            sess.choice = None
            return "Хорошо, ничего не меняю.", "other"
        items = [storage.get(user, i) for i in ch["ids"]]
        items = [r for r in items if r]
        if ch["op"] == "delete_all":
            if re.match(r"^(?:да|ага|угу|подтверждаю|подтвердить|точно|конечно|давай|ок|окей|yes)\b", n):
                sess.choice = None
                for r in items:
                    storage.delete(user, r["id"])
                sess.last_id, sess.last_list = None, []
                return f"Удалено записей: {len(items)}.", "delete"
            sess.choice = None
            return None
        if not items:
            sess.choice = None
            return None
        pick = None
        if len(n.split()) <= 6:
            if ch.get("all") and re.search(r"\b(?:все|всё|обе|оба)\b", n):
                pick = "all"
            elif re.search(r"\bпоследн\w+", n):
                pick = len(items)
            else:
                m = re.fullmatch(r"(?:номер\s+|№\s*)?(\d{1,2})\W*", n)
                pick = int(m.group(1)) if m else localnlu.ordinal_in(n)
        if pick is None:
            sess.choice = None
            return None
        if pick != "all" and not 1 <= pick <= len(items):
            return f"В списке {len(items)} записей, назовите номер от 1 до {len(items)}.", "clarify"
        chosen = items if pick == "all" else [items[pick - 1]]
        sess.choice = None
        op = ch["op"]
        if op == "update":
            cmd = Command(intent="update", date=ch.get("date"), time=ch.get("time"))
            return self._move(chosen[0], cmd, now, user, sess)
        for r in chosen:
            if op == "delete":
                storage.delete(user, r["id"])
            else:
                storage.update(user, r["id"], status="done" if op == "complete" else "open")
        sess.last_id = None if op == "delete" else chosen[-1]["id"]
        if op == "delete":
            sess.last_list = []                                    # номера в показанном списке больше не совпадают
        if len(chosen) == 1:
            r = chosen[0]
            verb = {"delete": "Удалил", "complete": "Отметил выполненной", "reopen": "Вернул в работу"}[op]
            return f"{verb}: {KIND_LABEL[r['kind']]} «{r['title']}».", "delete" if op == "delete" else "complete"
        verb = {"delete": "Удалено записей", "complete": "Отмечено выполненными", "reopen": "Возвращено в работу"}[op]
        return f"{verb}: {len(chosen)}.", "delete" if op == "delete" else "complete"

    def _choice_result(self, heard: str, res: tuple) -> dict:
        reply, intent = res
        out = {"ok": True, "transcript": heard, "reply": reply, "intent": intent, "engine": "cloud" if llm.usable() else "local"}
        if out["engine"] == "local":
            out["notice"] = llm.fallback_message()
        return out

    # ---------- отметка выполненной ----------
    def _complete(self, cmd: Command, heard: str, now: datetime, user: str, sess: Optional[Session] = None, done: bool = True) -> str:
        """done=True — отметить выполненной, done=False — снять отметку («отмени выполнение»)."""
        want = "open" if done else "done"
        verb = "Отметил выполненной" if done else "Вернул в работу"
        op = "complete" if done else "reopen"
        pool = [r for r in storage.all_items(user, KIND_FILTER.get(cmd.kind or "")) if r["status"] == want]
        if not pool:
            return "Невыполненных записей нет." if done else "Выполненных записей нет."
        new_status = "done" if done else "open"

        def apply(item) -> str:
            if sess is not None:
                sess.last_id = item["id"]
            storage.update(user, item["id"], status=new_status)
            return f"{verb}: {KIND_LABEL[item['kind']]} «{item['title']}»."

        if (cmd.ref or "").startswith("ord:"):
            item, ask = self._ord_item(cmd, sess, user)
            if ask:
                return ask
            if item["status"] != want:
                return "Эта запись уже выполнена." if done else "Эта запись и так не отмечена выполненной."
            return apply(item)
        if cmd.ref == "all":
            found = search.find(cmd.query, pool, limit=50)[0] if cmd.query else pool
            if not found:
                return "Не нашёл такие записи. Назовите их по-другому."
            for r in found:
                storage.update(user, r["id"], status=new_status)
            return f"{'Отмечено выполненными' if done else 'Возвращено в работу'}: {len(found)}."
        if not cmd.query and sess is not None and sess.last_id:      # «отметь её выполненной», «отмени выполнение»
            found = [r for r in pool if r["id"] == sess.last_id]
            if found:
                return apply(found[0])
        if not cmd.query and not done:
            if len(pool) == 1:
                return apply(pool[0])
            return self._ask_choice(sess, op, pool, now, "Какую запись вернуть в работу? Выполненные:", cmd)
        found, _mode = search.find(cmd.query or "", pool, limit=5)
        if not cmd.query or not found:
            return "Не нашёл такую невыполненную запись. Назовите её по-другому." if done else "Не нашёл такую выполненную запись. Назовите её по-другому."
        if len(found) > 1:
            return self._ask_choice(sess, op, found, now, "Нашёл несколько подходящих записей.", cmd)
        return apply(found[0])

    # ---------- удаление и перенос голосом ----------
    def _resolve(self, cmd: Command, now: datetime, user: str, sess: Session, op: str = "delete"):
        """Находит запись, о которой речь. Возвращает (запись, None) или (None, ответ-уточнение).
        Для необратимых действий принимаем только совпадение по словам, а не «по смыслу»."""
        pool = storage.all_items(user, KIND_FILTER.get(cmd.kind or ""))
        if not pool:
            return None, "Записей пока нет."
        if (cmd.ref or "").startswith("ord:"):
            return self._ord_item(cmd, sess, user)
        if cmd.query:
            found, mode = search.find(cmd.query, pool, limit=5)
            if not found:
                return None, "Не нашёл такую запись. Назовите её по-другому."
            if mode != "exact":
                return None, self._ask_choice(sess, op, found, now, "Точного совпадения нет. Возможно, вы имели в виду:", cmd, allow_all=False)
            if len(found) > 1:
                return None, self._ask_choice(sess, op, found, now, "Нашёл несколько подходящих записей.", cmd, allow_all=(op == "delete"))
            return found[0], None
        if cmd.ref == "last":
            return max(pool, key=lambda r: r["id"]), None
        item = storage.get(user, sess.last_id) if sess.last_id else None
        if item is None:
            return None, "О какой записи речь? Назовите её, например: «задачу про договор»."
        return item, None

    def _delete(self, cmd: Command, now: datetime, user: str, sess: Session) -> str:
        if cmd.ref in ("all", "all_done"):                          # «удали все…» — только после подтверждения
            pool = storage.all_items(user, KIND_FILTER.get(cmd.kind or ""))
            if cmd.ref == "all_done":
                pool = [r for r in pool if r["status"] == "done"]
            if cmd.query:
                found, mode = search.find(cmd.query, pool, limit=50)
                pool = found if mode == "exact" else []
            if not pool:
                return "Нечего удалять."
            sess.choice = {"op": "delete_all", "ids": [r["id"] for r in pool]}
            what = {"meeting": "встречи", "task": "задачи", "reminder": "напоминания"}.get(cmd.kind or "", "записи")
            if cmd.ref == "all_done":
                what = "выполненные " + what
            return f"Удалить все {what}: {len(pool)}? Скажите «да», чтобы подтвердить, или «нет»."
        item, ask = self._resolve(cmd, now, user, sess, "delete")
        if ask:
            return ask
        storage.delete(user, item["id"])
        sess.last_list = []
        if sess.last_id == item["id"]:
            sess.last_id = None
        return f"Удалил: {KIND_LABEL[item['kind']]} «{item['title']}»."

    def _update(self, cmd: Command, now: datetime, user: str, sess: Session):
        item, ask = self._resolve(cmd, now, user, sess, "update")
        if ask:
            return ask, "clarify"
        return self._move(item, cmd, now, user, sess)

    def _move(self, item: dict, cmd: Command, now: datetime, user: str, sess: Session):
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

    def import_items(self, user: str, items: list) -> dict:
        """Добавляет записи, пришедшие из браузера (созданные без связи или восстановленные из файла). Дубликаты и мусор пропускаются."""
        added = skipped = 0
        for it in items:
            kind, title = it.get("kind"), (it.get("title") or "").strip()[:300]
            d, t = parse_date(it.get("date")), parse_time(it.get("time"))
            dd = d.isoformat() if d else None
            if kind not in KIND_LABEL or not title or storage.find_duplicate(user, kind, title, dd, t):
                skipped += 1
                continue
            row = storage.add_item(user, kind, title, dd, t, str(it.get("created_at") or ""))
            if it.get("status") == "done":
                storage.update(user, row["id"], status="done")
            added += 1
        return {"added": added, "skipped": skipped}

    def delete_item(self, user: str, item_id: int) -> None:
        if not storage.delete(user, item_id):
            raise KeyError(item_id)
