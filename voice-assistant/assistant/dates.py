"""Разбор русских дат и времени: «завтра», «в пятницу», «через два часа», «на следующей неделе»."""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
          "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
WD_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WD_STEMS = ["понедельник", "вторник", "сред", "четверг", "пятниц", "суббот", "воскресень"]
MONTH_LIST = list(MONTHS)
NUMS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
        "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
        "пятнадцать": 15, "двадцать": 20, "тридцать": 30, "пару": 2, "несколько": 3}
NUMW = "|".join(sorted(NUMS, key=len, reverse=True))
MON_RE = "|".join(MONTHS)
PART = r"утра|дня|вечера|ночи"


def norm(t: str) -> str:
    return t.lower().replace("ё", "е")


def _num(tok):
    if tok is None:
        return None
    return int(tok) if tok.isdigit() else NUMS.get(tok)


def _h24(h, part):
    if part in ("дня", "вечера"):
        return h + 12 if h < 12 else h
    if part == "ночи":
        return 0 if h == 12 else (h + 12 if h >= 9 else h)
    return h


def _mk(y, m, d):
    try:
        return date(y, m, d)
    except ValueError:
        return None


_Date = date


@dataclass
class Parsed:
    date: _Date | None = None
    time: str | None = None            # "HH:MM"
    span: tuple | None = None          # (date, date) — период для списков
    deadline: bool = False             # «до конца недели», «до пятницы»
    spans: list = field(default_factory=list)  # куски текста, относящиеся к дате/времени

    @property
    def found(self):
        return bool(self.date or self.time or self.span)


def parse(text: str, now: datetime) -> Parsed:
    w = norm(text)
    p = Parsed()
    today = now.date()

    def take(m):
        nonlocal w
        p.spans.append((m.start(), m.end()))
        w = w[:m.start()] + " " * (m.end() - m.start()) + w[m.end():]

    # 1. «через ...»
    m = re.search(rf"через\s+(?:(\d+|{NUMW}|полтора)\s+)?(полчаса|минут\w*|час\w*|дн\w+|день|недел\w+)", w)
    if m:
        n, unit = m.group(1), m.group(2)
        k = 1.5 if n == "полтора" else (_num(n) or 1)
        if unit == "полчаса":
            delta = timedelta(minutes=30)
        elif unit.startswith("минут"):
            delta = timedelta(minutes=k)
        elif unit.startswith("час"):
            delta = timedelta(hours=k)
        elif unit.startswith("дн") or unit == "день":
            delta = timedelta(days=k)
        else:
            delta = timedelta(weeks=k)
        t = now + delta
        p.date = t.date()
        if delta < timedelta(days=1):
            p.time = t.strftime("%H:%M")
        take(m)

    # 2. явные даты
    m = re.search(rf"(?:\b(?:в|на|до)\s+)?\b(\d{{1,2}})\s+({MON_RE})(?:\s+(\d{{4}}))?", w)
    if m and not p.date:
        d, mo = int(m.group(1)), MONTHS[m.group(2)]
        y = int(m.group(3)) if m.group(3) else today.year
        cand = _mk(y, mo, d)
        if cand and not m.group(3) and (today - cand).days > 180:
            cand = _mk(y + 1, mo, d)
        if cand:
            p.date = cand
            p.deadline = w[m.start():].lstrip().startswith("до ")
            take(m)
    m = re.search(r"(?<![\d:.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])", w)
    if m and not p.date:
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        y = y + 2000 if y < 100 else y
        cand = _mk(y, mo, d)
        if cand and not m.group(3) and (today - cand).days > 180:
            cand = _mk(y + 1, mo, d)
        if cand:
            p.date = cand
            take(m)
    m = re.search(r"(?:\b(?:на|к|до)\s+)?\b(\d{1,2})\s+числа", w)
    if m and not p.date:
        d = int(m.group(1))
        cand = _mk(today.year, today.month, d)
        if not cand or cand < today:
            ny, nm = (today.year + (today.month == 12), today.month % 12 + 1)
            cand = _mk(ny, nm, d)
        if cand:
            p.date = cand
            take(m)

    # 3. сегодня / завтра / послезавтра / вчера
    m = re.search(r"\b(сегодня|завтра|послезавтра|вчера|позавчера)\b", w)
    if m and not p.date:
        off = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}[m.group(1)]
        p.date = today + timedelta(days=off)
        take(m)

    # 4. день недели
    m = re.search(r"(?:\b(?:в|во|на|до|к)\s+)?(?:(следующ\w+|ближайш\w+)\s+)?"
                  r"\b(понедельник\w*|вторник\w*|сред[аыуе]\b|четверг\w*|пятниц\w*|суббот\w*|воскресень\w*)", w)
    if m and not p.date:
        wd = [i for i, k in enumerate(WD_STEMS) if m.group(2).startswith(k)][0]
        if m.group(1) and m.group(1).startswith("следующ"):
            p.date = today - timedelta(days=today.weekday()) + timedelta(days=7 + wd)
        else:
            p.date = today + timedelta(days=(wd - today.weekday()) % 7 or 7)
        p.deadline = w[m.start():].lstrip().startswith("до ")
        take(m)

    # 5. периоды
    if not p.date:
        mon = today - timedelta(days=today.weekday())
        m = re.search(r"(?:\b(?:на|в|за|до)\s+)?\b(конца\s+недели|выходных|выходные|"
                      r"(?:следующ\w+|будущ\w+)\s+недел\w+|(?:эт\w+|текущ\w+)\s+недел\w+|неделю|"
                      r"конца\s+месяца|(?:этом|текущем)\s+месяце|следующем\s+месяце|месяц)\b", w)
        if m:
            k = m.group(1)
            if k.startswith("конца недели"):
                p.span, p.deadline = (today, mon + timedelta(days=6)), True
            elif k.startswith("выходн"):
                sat = mon + timedelta(days=5)
                if today > sat + timedelta(days=1):
                    sat += timedelta(days=7)
                p.span = (max(sat, today), sat + timedelta(days=1))
            elif k.startswith(("следующ", "будущ")) and "недел" in k:
                p.span = (mon + timedelta(days=7), mon + timedelta(days=13))
            elif "недел" in k and k != "неделю":
                p.span = (mon, mon + timedelta(days=6))
            elif k == "неделю":
                p.span = (today, today + timedelta(days=6))
            else:
                first = today.replace(day=1)
                nxt = (first + timedelta(days=32)).replace(day=1)
                if k.startswith("конца месяца"):
                    p.span, p.deadline = (today, nxt - timedelta(days=1)), True
                elif k.startswith("следующ"):
                    p.span = (nxt, (nxt + timedelta(days=32)).replace(day=1) - timedelta(days=1))
                elif k == "месяц":
                    p.span = (today, today + timedelta(days=30))
                else:
                    p.span = (first, nxt - timedelta(days=1))
            take(m)

    # 6. время
    if not p.time:
        m = re.search(r"(?:\b(?:в|к|до|на)\s+)?(?<![\d:])(\d{1,2}):(\d{2})\b(?:\s+(" + PART + r"))?", w)
        if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
            h = _h24(int(m.group(1)), m.group(3)) if m.group(3) else int(m.group(1))
            p.time = f"{h % 24:02d}:{m.group(2)}"
            take(m)
    if not p.time:
        m = re.search(rf"(?:\b(?:в|к|до|около)\s+)?\b(\d{{1,2}}|{NUMW})\s*(?:час\w*\s+)?({PART})\b", w)
        if m and (_num(m.group(1)) or 99) <= 12:
            p.time = f"{_h24(_num(m.group(1)), m.group(2)) % 24:02d}:00"
            take(m)
    if not p.time:
        m = re.search(rf"\b(?:в|к)\s+(\d{{1,2}}|{NUMW})(?:\s*час\w*)?\b"
                      rf"(?!\s*(?:\d|[:.%]|числ|раз|год|лет|штук|минут|секунд|дн|недел|месяц|руб|{MON_RE}))", w)
        if m and _num(m.group(1)) is not None and _num(m.group(1)) <= 23:
            h = _num(m.group(1))
            h = h + 12 if 1 <= h <= 6 else h
            p.time = f"{h:02d}:00"
            take(m)

    p.spans.sort()
    return p


def human_date(d: date, today: date) -> str:
    diff = (d - today).days
    base = f"{d.day} {MONTH_LIST[d.month - 1]}"
    if diff == 0:
        return f"сегодня, {base}"
    if diff == 1:
        return f"завтра, {base}"
    if diff == 2:
        return f"послезавтра, {base}"
    if diff == -1:
        return f"вчера, {base}"
    return f"{WD_NAMES[d.weekday()]}, {base}"
