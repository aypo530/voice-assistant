"""Бизнес-логика: принимает текст, понимает команду (GigaChat + правила), выполняет действие, формирует ответ.

Ответы формируются кодом, а не нейросетью: модель не может «выдумать» несуществующую запись.
"""
import re
from datetime import datetime, timedelta

from . import dates, nlu, search
from .store import type_match
from .llm import GigaChat, parse_command, semantic_pick

TYPE_RU = {"task": "Задача", "meeting": "Встреча", "reminder": "Напоминание"}
TYPE_ACC = {"task": "задачу", "meeting": "встречу", "reminder": "напоминание"}
HELP = ("Не совсем понял. Попробуйте, например: «Напомни завтра в 10 позвонить Ивану», "
        "«Добавь встречу с командой завтра в 15:00», «Что у меня сегодня?», «Найди задачу про договор».")
BARE_TIME = re.compile(r"^\s*(?:в\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:час\w*)?\s*(утра|дня|вечера|ночи)?\s*$")


class Assistant:
    def __init__(self, store, llm: GigaChat | None):
        self.store, self.llm = store, llm
        self.sessions = {}

    # ---------- вход ----------
    def handle(self, user: str, text: str, now: datetime) -> dict:
        text = (text or "").strip()[:500]
        if not text:
            return self._r("Не расслышал. Повторите, пожалуйста.", "unknown", "rules")
        if len(self.sessions) > 3000:
            self.sessions.clear()
        s = self.sessions.setdefault(user, {})

        pend = s.pop("pending", None)
        if pend:
            res = self._continue(user, s, pend, text, now)
            if res:
                return res

        rules = nlu.rule_parse(text, now)
        cmd, source = rules, "rules"
        if self.llm and self.llm.available():
            cmd, source = self._merge(rules, parse_command(self.llm, text, now))

        i = cmd["intent"]
        if i == "create":
            res = self._create(user, s, cmd, now)
        elif i == "list":
            res = self._list(user, cmd, now)
        elif i == "search":
            res = self._search(user, cmd, now)
        elif i in ("complete", "delete", "move"):
            res = self._modify(user, s, cmd, i, now)
        else:
            res = self._r(HELP, "unknown", source)
        res["source"] = source
        return res

    @staticmethod
    def _merge(rules, llm):
        """Даты и время всегда берём из правил (они точные), смысл — у нейросети."""
        if not llm or (llm["intent"] == "unknown" and rules["intent"] != "unknown"):
            return rules, "rules"
        out = dict(llm)
        for k in ("date", "time", "range"):
            if rules.get(k):
                out[k] = rules[k]
        out["deadline"] = rules.get("deadline", False)
        for k in ("type", "title", "query", "filter_type"):
            if not out.get(k):
                out[k] = rules.get(k)
        if out["intent"] == "create" and not out.get("type"):
            out["type"] = "task"
        return out, "llm"

    # ---------- создание ----------
    def _create(self, user, s, cmd, now):
        d, t = cmd.get("date"), cmd.get("time")
        rng = cmd.get("range")
        if not d and rng and cmd.get("intent") == "create":
            d = rng[1] if cmd.get("deadline") else rng[0]
        draft = {"type": cmd.get("type") or "task", "title": cmd.get("title") or "", "date": d, "time": t}
        return self._finish(user, s, draft, now)

    def _finish(self, user, s, dr, now):
        today = now.date()
        if dr["time"] and not dr["date"]:
            dr["date"] = today if dr["time"] > now.strftime("%H:%M") else today + timedelta(days=1)
        what = TYPE_ACC[dr["type"]]
        if not dr["title"]:
            return self._ask(s, dr, "title", f"Что именно записать в {what}?" if dr["type"] != "meeting"
                             else "С кем или по какому поводу встреча?")
        if dr["type"] == "meeting":
            if not dr["date"] and not dr["time"]:
                return self._ask(s, dr, "when", "На какой день и время поставить встречу?")
            if not dr["date"]:
                return self._ask(s, dr, "date", "На какой день поставить встречу?")
            if not dr["time"]:
                return self._ask(s, dr, "time", "На какое время поставить встречу?")
        if dr["type"] == "reminder":
            if not dr["date"] and not dr["time"]:
                return self._ask(s, dr, "when", "Когда напомнить?")
            if not dr["time"]:
                return self._ask(s, dr, "time", "Во сколько напомнить?")

        note = ""
        if dr["type"] == "meeting":
            clash = [r for r in self.store.between(user, dr["date"].isoformat(), dr["date"].isoformat(), "meeting")
                     if r["time"] == dr["time"]]
            if clash:
                note = f" Внимание: в это время уже есть встреча «{clash[0]['title']}»."
        item_id = self.store.add(user, dr["type"], dr["title"], dr["date"].isoformat() if dr["date"] else None,
                                 dr["time"], now.isoformat(timespec="seconds"))
        s["last_id"] = item_id
        when = self._when(dr["date"], dr["time"], today)
        return self._r(f"Готово: {TYPE_ACC[dr['type']]} «{dr['title']}»{(' — ' + when) if when else ''}.{note}",
                       "create", refresh=True)

    def _ask(self, s, draft, need, question):
        s["pending"] = {"draft": draft, "need": need}
        return self._r(question, "clarify")

    def _continue(self, user, s, pend, text, now):
        """Ответ на уточняющий вопрос. Если это новая команда — возвращаем None."""
        t = dates.norm(text)
        if nlu.R_CANCEL.search(t) and len(t.split()) <= 4:
            return self._r("Хорошо, отменил.", "cancel")
        dr, need = pend["draft"], pend["need"]
        # новая самостоятельная команда важнее уточнения
        if (nlu.R_CREATE.search(t) or nlu.R_LIST.search(t) or nlu.R_SEARCH.search(t) or nlu.R_DONE.search(t)
                or nlu.R_DELETE.search(t) or nlu.R_MOVE.search(t) or t.rstrip().endswith("?")):
            return None
        if need == "title":
            dr["title"] = nlu.clean_title(text, [], dr["type"]) or text.strip()
            return self._finish(user, s, dr, now)
        P = dates.parse(text, now)
        m = BARE_TIME.match(t)
        if not P.time and m and need in ("time", "when"):
            h = int(m.group(1))
            if m.group(3):
                h = dates._h24(h, m.group(3))
            elif 1 <= h <= 6:
                h += 12
            if h < 24:
                P.time = f"{h:02d}:{m.group(2) or '00'}"
        if not (P.date or P.time or P.span):
            return None
        dr["date"] = P.date or (P.span[0] if P.span else dr["date"])
        dr["time"] = P.time or dr["time"]
        return self._finish(user, s, dr, now)

    # ---------- список ----------
    def _list(self, user, cmd, now):
        today = now.date()
        rng, ft = cmd.get("range"), cmd.get("filter_type")
        if rng:
            a, b = rng
            items = self.store.between(user, a.isoformat(), b.isoformat(), ft)
            label = "на " + (dates.human_date(a, today) if a == b else
                             f"период {a.day} {dates.MONTH_LIST[a.month - 1]} — {b.day} {dates.MONTH_LIST[b.month - 1]}")
            if a == b and a == today:
                label = "на сегодня"
        else:
            items = self.store.upcoming(user, today.isoformat(), ft)
            label = "в ближайшее время"
        noun = {"meeting": "встреч", "task": "задач", "reminder": "напоминаний"}.get(ft, "записей")
        if not items:
            return self._r(f"{label.capitalize()} — {noun} нет.", "list")
        lines = [f"{label.capitalize()}:"]
        cur = object()
        for r in items:
            if r["date"] != cur and (not rng or rng[0] != rng[1]):
                cur = r["date"]
                lines.append(("• " + dates.human_date(dates.date.fromisoformat(cur), today).capitalize() + ":")
                             if cur else "• Без даты:")
            lines.append("  " + self._line(r, today))
        return self._r("\n".join(lines), "list")

    # ---------- поиск ----------
    def _search(self, user, cmd, now):
        items = self.store.all(user)
        q, ft = cmd.get("query") or "", cmd.get("filter_type")
        hits = [i for _, i in search.find(items, q, ft)]
        if not hits and q and self.llm and self.llm.available():
            pool = [i for i in items if type_match(i["type"], ft)]
            ids = semantic_pick(self.llm, q, pool)
            hits = [i for i in pool if i["id"] in ids]
        if not hits:
            return self._r("Ничего похожего не нашёл. Попробуйте другие слова.", "search")
        today = now.date()
        lines = [f"Нашёл {len(hits)}:" if len(hits) > 1 else "Нашёл:"]
        lines += ["• " + self._line(r, today, full=True) for r in hits]
        return self._r("\n".join(lines), "search")

    # ---------- выполнено / удалить / перенести ----------
    def _modify(self, user, s, cmd, intent, now):
        status = "open" if intent in ("complete", "move") else None
        pool = self.store.all(user, status)
        q = cmd.get("query") or ""
        if q.strip():
            res = search.find(pool, q, cmd.get("filter_type"))
        else:
            last = self.store.get(user, s.get("last_id")) if s.get("last_id") else None
            res = [(1.0, last)] if last else []
        if not res:
            return self._r("Не нашёл подходящую запись. Назовите её точнее.", intent)
        if len(res) > 1 and res[1][0] >= res[0][0] - 0.05 and res[0][0] < 1.0:
            names = "; ".join(f"«{i['title']}»" for _, i in res[:3])
            return self._r(f"Подходит несколько записей: {names}. Уточните, какую именно.", intent)
        it = res[0][1]
        s["last_id"] = it["id"]
        if intent == "complete":
            self.store.set_status(user, it["id"], "done")
            return self._r(f"Отметил выполненным: «{it['title']}».", intent, refresh=True)
        if intent == "delete":
            self.store.delete(user, it["id"])
            return self._r(f"Удалил: «{it['title']}».", intent, refresh=True)
        d, t = cmd.get("date"), cmd.get("time")
        if not d and not t:
            return self._r(f"На когда перенести «{it['title']}»?", "clarify")
        new_d = d.isoformat() if d else it["date"]
        new_t = t or it["time"]
        self.store.move(user, it["id"], new_d, new_t)
        when = self._when(dates.date.fromisoformat(new_d) if new_d else None, new_t, now.date())
        return self._r(f"Перенёс «{it['title']}» — {when}.", intent, refresh=True)

    # ---------- форматирование ----------
    @staticmethod
    def _when(d, t, today):
        if not d and not t:
            return ""
        parts = [dates.human_date(d, today)] if d else []
        if t:
            parts.append(f"в {t}")
        return " ".join(parts)

    def _line(self, r, today, full=False):
        mark = " (выполнено)" if r["status"] == "done" else ""
        when = ""
        if full and r["date"]:
            when = dates.human_date(dates.date.fromisoformat(r["date"]), today) + (f" {r['time']}" if r["time"] else "") + " — "
        elif r["time"]:
            when = r["time"] + " — "
        return f"{when}{TYPE_RU[r['type']]}: {r['title']}{mark}"

    @staticmethod
    def _r(text, intent, source="rules", refresh=False):
        speech = re.sub(r"\s*[•\n]+\s*", ". ", text).replace("..", ".").strip()
        return {"reply": text, "speech": speech, "intent": intent, "source": source, "refresh": refresh}
