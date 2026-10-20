"""Чистые функции: даты, время, русские формулировки, простая нормализация слов для поиска."""
import re
from datetime import date, datetime, timedelta
from typing import Optional

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_ON = ["в понедельник", "во вторник", "в среду", "в четверг", "в пятницу", "в субботу", "в воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$")


def parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_time(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = _TIME_RE.match(value.strip())
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def calendar_hint(now: datetime) -> str:
    """Таблица ближайших дат — LLM надёжнее переводит «в пятницу» в дату, когда видит календарь."""
    today = now.date()
    lines = []
    for i in range(15):
        d = today + timedelta(days=i)
        tag = " (сегодня)" if i == 0 else " (завтра)" if i == 1 else ""
        lines.append(f"{d.isoformat()} — {WEEKDAYS[d.weekday()]}{tag}")
    sunday = today + timedelta(days=6 - today.weekday())
    next_monday = sunday + timedelta(days=1)
    return (
        "Календарь:\n" + "\n".join(lines)
        + f"\nКонец текущей недели (воскресенье): {sunday.isoformat()}"
        + f"\nСледующая неделя: {next_monday.isoformat()} — {(next_monday + timedelta(days=6)).isoformat()}"
    )


def short_date(d: date, today: date) -> str:
    s = f"{d.day} {MONTHS[d.month - 1]}"
    return s if d.year == today.year else f"{s} {d.year}"


def human_date(d: date, today: date) -> str:
    diff = (d - today).days
    names = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}
    base = short_date(d, today)
    if diff in names:
        return f"{names[diff]}, {base}"
    if 2 < diff < 7:
        return f"{WEEKDAYS_ON[d.weekday()]}, {base}"
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {base}"


def when_phrase(d: Optional[date], t: Optional[str], today: date) -> str:
    parts = []
    if d:
        parts.append(human_date(d, today))
    if t:
        parts.append(f"в {t}")
    return ", ".join(parts)


def period_phrase(start: date, end: date, today: date) -> str:
    if start == end:
        names = {0: "на сегодня", 1: "на завтра", 2: "на послезавтра"}
        return names.get((start - today).days, f"на {short_date(start, today)}")
    if start.month == end.month and start.year == end.year:
        return f"с {start.day} по {end.day} {MONTHS[end.month - 1]}"
    return f"с {short_date(start, today)} по {short_date(end, today)}"


# ---------- лёгкий поиск по словам (без внешних библиотек) ----------
_STOP = {"про", "что", "для", "как", "это", "мой", "моя", "мои", "его", "ней", "она", "они",
         "когда", "где", "был", "была", "было", "были", "есть", "нет", "или", "при", "над",
         "под", "без", "найди", "найти", "задача", "задачу", "встреча", "встречу", "ставил", "поставил"}


def words(text: str) -> list:
    return re.findall(r"\w+", (text or "").lower().replace("ё", "е"))


def query_stems(query: str) -> list:
    """Грубая основа слова запроса: достаточно, чтобы «Ивану»/«Иваном» совпали с «Иван»."""
    out = []
    for w in words(query):
        if len(w) < 3 or w in _STOP:
            continue
        out.append(w[:5] if len(w) > 5 else (w[:-1] if len(w) > 4 else w))
    return out


def match_score(stems: list, title: str) -> int:
    title_words = words(title)
    return sum(1 for s in stems if any(w.startswith(s) for w in title_words))
