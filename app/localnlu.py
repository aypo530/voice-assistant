"""Локальный (офлайн) разбор русских запросов правилами — запасной режим, когда Gemini недоступен.

Понимает те же действия, что и облачный разборщик (создать / показать за период / найти / отметить выполненной),
относительные даты («завтра», «в пятницу», «через два часа», «на следующей неделе», «до конца недели»),
время («в 10 утра», «в 7 вечера», «в три», «15:30»). Точность ниже, чем у LLM: нестандартные формулировки
могут быть поняты неверно — тогда ассистент переспросит или не найдёт нужное.
"""
import re
from calendar import monthrange
from datetime import date, datetime, timedelta
from typing import Optional

from command import Command
from textutil import MONTHS

# ---------- числа словами ----------
_UNITS = {"один": 1, "одну": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
          "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
_TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
          "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50}
NUMS = {**_UNITS, **_TEENS, **_TENS, "ноль": 0}
_NUM_WORD = (r"(?:(?:двадцать|тридцать|сорок|пятьдесят)(?:\s+(?:одну|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять))?"
             r"|одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|шестнадцать|семнадцать|восемнадцать|девятнадцать"
             r"|десять|одну|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять)")
_NUM = rf"(?:\d{{1,3}}|{_NUM_WORD})"
_HOUR_WORD = r"(?:одиннадцать|двенадцать|десять|девять|восемь|семь|шесть|пять|четыре|три|два|один)"

_MONTH_RE = "|".join(MONTHS)
_WD = [r"понедельник\w*", r"вторник\w*", r"сред[ауые]\b", r"четверг\w*", r"пятниц\w*", r"суббот\w*", r"воскресень\w*"]
_WD_ANY = "(?:" + "|".join(_WD) + ")"


_GLUED = re.compile(r"\b(?P<h>одиннадцать|двенадцать|десять|девять|восемь|восьм|семь|шесть|пять|четыре|три|два|один)"
                    r"(?P<ap>утра|вечера|дня|ночи)\b", re.I)


def fix_glued(text: str) -> str:
    """Распознавание речи иногда склеивает слова: «восьмутра» → «восемь утра»."""
    return _GLUED.sub(lambda m: ("восемь" if m.group("h").lower() == "восьм" else m.group("h")) + " " + m.group("ap"), text)


_BARE_TIME = re.compile(r"^\s*(?:на\s+|в\s+)?(?P<h>\d{1,2}|" + _HOUR_WORD + r")(?:[:.\s]+(?P<m>[0-5]\d))?\s*(?:час\w*)?\s*$")


def to_int(s: str) -> int:
    s = s.strip()
    if s.isdigit():
        return int(s)
    return sum(NUMS.get(p, 0) for p in s.split())


class Text:
    """Рабочая копия фразы: найденные куски «вырезаются», остаток — это название записи."""

    def __init__(self, raw: str):
        norm = raw.lower().replace("ё", "е")
        self.raw = raw if len(norm) == len(raw) else norm
        self.norm = norm if len(norm) == len(raw) else norm
        self.w = list(self.norm)
        self.cut = [False] * len(self.raw)

    def s(self) -> str:
        return "".join(self.w)

    def search(self, pattern: str):
        return re.search(pattern, self.s())

    def take(self, m, group: int = 0) -> None:
        a, b = m.span(group)
        for i in range(a, b):
            self.w[i] = " "
            self.cut[i] = True

    def take_all(self, pattern: str) -> bool:
        found = False
        while True:
            m = self.search(pattern)
            if not m or m.end() == m.start():
                return found
            self.take(m)
            found = True

    def remainder(self) -> str:
        return re.sub(r"\s+", " ", "".join(" " if k else c for c, k in zip(self.raw, self.cut))).strip()

    def remainder_norm(self) -> str:
        return re.sub(r"\s+", " ", self.s()).strip()


# ---------- даты ----------
def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def weekday_date(idx: int, today: date, mod: str = "", shift: int = 0) -> date:
    base = monday(today)
    if shift or mod.startswith("следующ") or mod.startswith("будущ"):
        return base + timedelta(days=7 + idx)
    if mod in ("эту", "этот", "этой"):
        d = base + timedelta(days=idx)
        return d if d >= today else d + timedelta(days=7)
    delta = (idx - today.weekday()) % 7
    return today + timedelta(days=delta or 7)


def _day_month(day: int, month: int, today: date, year: Optional[int] = None) -> Optional[date]:
    try:
        d = date(year or today.year, month, day)
    except ValueError:
        return None
    if year is None and d < today:
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d


def to24(h: int, ap: Optional[str], literal: str = "") -> int:
    if ap in ("вечера", "дня"):
        return h + 12 if h < 12 else h
    if ap == "ночи":
        return 0 if h == 12 else h
    if ap == "утра":
        return h
    if literal.startswith("0") or h == 0 or h >= 8:
        return h
    return h + 12 if 1 <= h <= 7 else h


class Parsed:
    def __init__(self):
        self.date: Optional[date] = None
        self.time: Optional[str] = None
        self.week_shift = 0          # «на следующей неделе»
        self.this_week = False       # «на этой неделе»
        self.weekend = False
        self.to_week_end = False
        self.range: Optional[tuple] = None
        self.relative_time = False


_MON_STEMS = ["январ", "феврал", "март", "апрел", "ма", "июн", "июл", "август", "сентябр", "октябр", "ноябр", "декабр"]


def _month_range(month: int, today: date, named: bool = False) -> tuple:
    """Границы месяца. month может выйти за 1..12 (соседний год). Для названного месяца далёкий прошлый считаем следующим годом."""
    year = today.year
    if month < 1:
        month, year = month + 12, year - 1
    elif month > 12:
        month, year = month - 12, year + 1
    if named and today.month - month > 6:
        year += 1
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def scan_when(t: Text, now: datetime) -> Parsed:
    """Находит дату/время/период во фразе и вырезает их из Text."""
    p = Parsed()
    today = now.date()

    # «через …»
    m = t.search(rf"\bчерез\s+(?:полчаса|(?:(?P<n>{_NUM})\s+)?(?P<u>час\w*|минут\w*|мин\b|дн\w*|день|недел\w*))")
    if m:
        t.take(m)
        if "полчаса" in m.group(0):
            dt = now + timedelta(minutes=30)
            p.date, p.time, p.relative_time = dt.date(), dt.strftime("%H:%M"), True
        else:
            n = to_int(m.group("n")) if m.group("n") else 1
            u = m.group("u")
            if u.startswith("час"):
                dt = now + timedelta(hours=n)
                p.date, p.time, p.relative_time = dt.date(), dt.strftime("%H:%M"), True
            elif u.startswith("мин"):
                dt = now + timedelta(minutes=n)
                p.date, p.time, p.relative_time = dt.date(), dt.strftime("%H:%M"), True
            elif u.startswith("недел"):
                p.date = today + timedelta(weeks=n)
            else:
                p.date = today + timedelta(days=n)

    # недельные фразы
    m = t.search(r"\b(?:на|в|за)\s+следующ\w+\s+недел\w+|\bна\s+будущ\w+\s+недел\w+")
    if m:
        p.week_shift = 1
        t.take(m)
    m = t.search(r"\b(?:на|в|за)\s+(?:этой|эту|текущей|эту)\s+недел\w+|\bна\s+недел[юие]\b")
    if m:
        p.this_week = True
        t.take(m)
    m = t.search(r"\bна\s+выходн\w+|\bв\s+выходн\w+")
    if m:
        p.weekend = True
        t.take(m)
    m = t.search(r"\bдо\s+конца\s+недели\b")
    if m:
        p.to_week_end = True
        t.take(m)
    m = t.search(r"\bдо\s+конца\s+месяца\b")
    if m:
        p.date = date(today.year, today.month, monthrange(today.year, today.month)[1])
        t.take(m)
    m = t.search(rf"\b(?:на|в)\s+(?:ближайш\w+|следующ\w+)\s+(?P<n>{_NUM})?\s*(?:дн\w+|день)\b")
    if m:
        n = to_int(m.group("n")) if m.group("n") else 7
        p.range = (today, today + timedelta(days=max(n - 1, 0)))
        t.take(m)
    m = t.search(r"\b(?:в|на)\s+(?:этом|текущем)\s+месяц\w*|\bна\s+месяц\b")
    if m:
        p.range = (today, date(today.year, today.month, monthrange(today.year, today.month)[1]))
        t.take(m)
    m = t.search(r"\b(?:в|на|за)\s+прошл\w+\s+недел\w+")
    if m:
        mon = monday(today) - timedelta(days=7)
        p.range = (mon, mon + timedelta(days=6))
        t.take(m)
    m = t.search(r"\b(?:в|на|за)\s+прошл\w+\s+месяц\w*")
    if m:
        p.range = _month_range(today.month - 1, today)
        t.take(m)
    m = t.search(r"\b(?:в|на|за)\s+(?:следующ\w+|будущ\w+)\s+месяц\w*")
    if m:
        p.range = _month_range(today.month + 1, today)
        t.take(m)
    m = t.search(r"\b(?:в|на|за)\s+(?P<mn>январ[еья]|феврал[еья]|март[е]?|апрел[еья]|ма[йе]|июн[еья]|июл[еья]|август[е]?|сентябр[еья]|октябр[еья]|ноябр[еья]|декабр[еья])\b(?!\s*\d)")
    if m:
        mi = next(i for i, stem in enumerate(_MON_STEMS) if m.group("mn").startswith(stem)) + 1
        if mi == today.month:
            p.range = _month_range(mi, today)
        else:
            p.range = _month_range(mi, today, named=True)
        t.take(m)

    # конкретная дата
    if p.date is None:
        m = t.search(r"\b(\d{4})-(\d{2})-(\d{2})\b")
        if m:
            try:
                p.date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                t.take(m)
            except ValueError:
                pass
    if p.date is None:
        m = t.search(rf"\b(?:на\s+|к\s+|до\s+)?(\d{{1,2}})\s+({_MONTH_RE})(?:\s+(\d{{4}}))?")
        if m:
            d = _day_month(int(m.group(1)), MONTHS.index(m.group(2)) + 1, today, int(m.group(3)) if m.group(3) else None)
            if d:
                p.date = d
                t.take(m)
    if p.date is None:
        m = t.search(r"\b(?:на\s+|к\s+|до\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
        if m and 1 <= int(m.group(2)) <= 12:
            y = int(m.group(3)) if m.group(3) else None
            if y is not None and y < 100:
                y += 2000
            d = _day_month(int(m.group(1)), int(m.group(2)), today, y)
            if d:
                p.date = d
                t.take(m)

    # сегодня / завтра / послезавтра / вчера
    if p.date is None:
        m = t.search(r"\b(?:на\s+|с\s+|до\s+)?(сегодня|завтра|послезавтра|вчера|позавчера)\b")
        if m:
            off = {"сегодня": 0, "завтра": 1, "послезавтра": 2, "вчера": -1, "позавчера": -2}[m.group(1)]
            p.date = today + timedelta(days=off)
            t.take(m)

    # день недели
    if p.date is None:
        m = t.search(rf"\b(?:(?:в|во|на|к|до)\s+)?(?:(?P<mod>эту|этот|этой|следующ\w+|ближайш\w+|будущ\w+)\s+)?(?P<wd>{_WD_ANY})")
        if m:
            wd = m.group("wd")
            idx = next(i for i, pat in enumerate(_WD) if re.match(pat, wd))
            mod = m.group("mod") or ""
            p.date = weekday_date(idx, today, mod, p.week_shift)
            t.take(m)

    # время
    if p.time is None:
        m = t.search(r"\bв\s+полдень\b")
        if m:
            p.time = "12:00"
            t.take(m)
    if p.time is None:
        m = t.search(r"\bв\s+полночь\b")
        if m:
            p.time = "00:00"
            t.take(m)
    if p.time is None:
        m = t.search(r"\b(?:(?:в|к|до|около|ровно|на)\s+)?(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?:\s*(?P<ap>утра|вечера|дня|ночи))?")
        if m:
            h = to24(int(m.group("h")), m.group("ap"), m.group("h"))
            p.time = f"{h:02d}:{m.group('m')}"
            t.take(m)
    if p.time is None:      # «8 утра», «на 8 утра», «восемь вечера»: предлог не обязателен, если есть «утра/вечера…»
        m = t.search(rf"\b(?:(?:в|к|до|около|на)\s+)?(?P<h>\d{{1,2}}|{_HOUR_WORD})(?:[:.\s]+(?P<mm>[0-5]\d))?(?:\s+час\w*)?\s+(?P<ap>утра|вечера|дня|ночи)\b")
        if m:
            raw_h = m.group("h")
            h = int(raw_h) if raw_h.isdigit() else NUMS.get(raw_h, -1)
            if 0 <= h <= 23:
                p.time = f"{to24(h, m.group('ap'), raw_h):02d}:{m.group('mm') or '00'}"
                t.take(m)
    if p.time is None:
        m = t.search(rf"\b(?:в|к|до|около|на)\s+(?P<h>\d{{1,2}}|{_HOUR_WORD})(?:\s+час\w*)?(?:\s+(?P<ap>утра|вечера|дня|ночи))?"
                     r"(?!\s*(?:\d|раз\b|человек|штук|шт\b|минут|секунд|дн|недел|месяц|год|лет\b|рубл|тысяч|процент|%))")
        if m:
            raw_h = m.group("h")
            h = int(raw_h) if raw_h.isdigit() else NUMS.get(raw_h, -1)
            if 0 <= h <= 23:
                p.time = f"{to24(h, m.group('ap'), raw_h):02d}:00"
                t.take(m)
    return p


# ---------- намерения ----------
_RE_HELP = r"\b(?:что\s+ты\s+(?:умеешь|можешь)|помощь|помоги\s+мне\s+разобраться|как\s+тобой\s+пользоваться|как\s+(?:это\s+)?работает)\b"
_RE_GREET = r"^(?:привет\w*|здравствуй\w*|добрый\s+(?:день|вечер|утро)|доброе\s+утро|хай|hello|hi|спасибо\w*|благодар\w+|пока|до\s+свидания|ок|окей|ясно|понятно|отлично|хорошо)\W*$"
def _greet_reply(n: str) -> str:
    if re.match(r"^(?:спасибо|благодар)", n):
        return "Пожалуйста! Если нужно что-то записать или найти — скажите."
    if re.match(r"^(?:пока|до\s+свидания)", n):
        return "До свидания! Ваши записи сохранены."
    if re.match(r"^(?:ок|окей|ясно|понятно|отлично|хорошо)", n):
        return "Хорошо. Что записать или найти?"
    return "Здравствуйте! Что записать или найти? Например: «Напомни завтра в 10 позвонить Ивану»."


_RE_CANCEL = r"^(?:нет|отмена|отмени\w*|не\s+надо|не\s+нужно|забудь|стоп)\b"
_RE_COMPLETE = (r"\b(?:отмет\w+|помет\w+|закро\w+|заверш\w+)\b.*\b(?:выполнен\w+|сделан\w+|готов\w+|заверш\w+)\b"
                r"|\b(?:отмет\w+|помет\w+|закро\w+)\s+(?:задач\w*|встреч\w*|напоминани\w*|дел\w*)"
                r"|\b(?:отмет\w+|помет\w+)\b"                                          # «отметь галочкой договор», «отметь, что я…»
                r"|\bгалочк\w*"                                                         # «поставь галочку у договора»
                r"|^(?:выполнено|сделано|готово)\b"                                       # «Выполнено: подписать договор»
                r"|\b(?:выполнен[аоы]?|сделан[аоы]?|готов[аоы]?|завершен[аоы]?)\W*$"      # «Задача про договор выполнена», «Договор готов»
                r"|\b(?:я\s+)?(?:уже\s+)?(?:выполнил\w*|сделал\w*|закончил\w*|завершил\w*)\b"
                r"|^я\s+(?:уже\s+)?\w+(?:ил|ыл|ал|ел|ял|ул|ол)(?:а)?\b")                # «Я подписал договор»
_RE_DELETE = (r"^(?:пожалуйста\s+)?(?:удали\w*|сотри|стери|убери|очисти\w*)\b"
              r"|\b(?:удали\w*|убери)\s+(?:задач\w*|встреч\w*|напоминани\w*|запис\w*|заметк\w*)"
              r"|^(?:пожалуйста\s+)?отмени\w*\s+(?:задач\w*|встреч\w*|напоминани\w*|запис\w*)")
_RE_UPDATE = r"\b(?:перенеси\w*|перенести|перемести\w*|передвин\w*|сдвин\w*|отложи\w*|поменяй\w*\s+(?:время|дат\w*)|измени\w*\s+(?:время|дат\w*))\b"
_RE_FIND = r"\b(?:найди|найти|найдешь|поищи|поискать|искать|ищи|отыщи|вспомни|подскажи\s+где)\b"
_RE_LIST = [
    r"^(?:а\s+)?(?:что|чем)\s+(?:у\s+меня|мне|запланирован\w*|намечен\w*|на\b|по\s+план|в\b|есть|буд\w+|был[аио]?\b)",
    r"\bкакие\s+(?:у\s+меня\s+)?(?:планы|задачи|встречи|дела|напоминания|события)\b",
    r"\b(?:покажи|показать|выведи|назови|перечисли|расскажи|скажи)\b",
    r"\b(?:есть|будут|будет)\s+ли\b",
    r"\bсписок\s+(?:задач|встреч|дел|планов)\b|\bрасписание\b|\bповестка\b|\bпланы\s+на\b|\bчто\s+по\s+план",
    r"\bсколько\s+(?:у\s+меня\s+)?(?:задач|встреч|дел)\b",
    r"\bмо[иеёя]\s+(?:дела|планы|задачи|встречи|записи|напоминания)\b",
    r"^(?:а\s+)?что\s+я\s+(?:планировал\w*|запланировал\w*|делал\w*|собирался)\b",
    r"^(?:а\s+)?что\s+(?:мне\s+)?(?:нужно|надо|делать|предстоит|сделать)\b",
    r"\b(?:все|всё)\s+(?:мои\s+)?(?:задачи|встречи|записи|напоминания|дела|планы)\b",
]
_RE_ALL = r"\b(?:все|всё|всех)\b"
_RE_WHEN_SEARCH = (r"\bкогда\b.*\b(?:был[аио]?|были|ставил\w*|поставил\w*|создавал\w*|записывал\w*|планировал\w*|назначал\w*)\b"
                   r"|\bкогда\s+(?:у\s+меня\s+)?(?:встреча|задача|напоминание|созвон)\b"
                   r"|\bесть\s+(?:ли\s+)?что[\s-]?(?:нибудь|то)\s+(?:про|о|об|по)\b"
                   r"|\bя\s+(?:когда-?то|как-?то|ранее|раньше|давно)\b|\bпомнишь\b|\bгде\s+(?:моя\s+|мое\s+)?(?:задача|встреча|запись|напоминание)\b")
_RE_CREATE_VERB = (r"\b(?:напомни\w*|напоминай\w*|поставь|поставить|добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|"
                   r"назначь|назначить|внеси|не\s+забудь|нужно|надо|необходимо|пора)\b")
_RE_REMINDER = r"\bнапомни\w*|\bнапоминай\w*|\bнапоминани\w*|\bне\s+забуд\w*"
_RE_MEETING = (r"\b(?:встреч\w*|созвон\w*|совещани\w*|планерк\w*|митинг\w*|собеседовани\w*|прием\w*|визит\w*|консультаци\w*)"
               r"|\b(?:к|у)\s+(?:врач\w*|стоматолог\w*|доктор\w*|парикмахер\w*|терапевт\w*|окулист\w*)")

_KIND_NOUNS = [("meeting", r"\b(?:встреч\w*|созвон\w*|совещани\w*)"), ("task", r"\bзадач\w*|\bдел[оа]\b"),
               ("reminder", r"\bнапоминани\w*")]

_Q_DROP = (r"\b(?:найди|найти|найдешь|поищи|поискать|искать|ищи|отыщи|вспомни|покажи|показать|скажи|подскажи|расскажи|"
           r"когда|где|что|какая|какой|какую|какие|был[аио]?|были|будет|будут|ставил\w*|поставил\w*|создавал\w*|создал\w*|"
           r"записывал\w*|записал\w*|добавлял\w*|планировал\w*|назначал\w*|у\s+меня|мне|мой|моя|мою|мои|мое|ее|его|их|"
           r"есть|нибудь|я|мы|ты|вы|когда-?то|как-?то|ранее|раньше|давно|помнишь|пожалуйста|ли|задач\w*|встреч\w*|напоминани\w*|"
           r"запис\w*|дело|дела|дел|про|по|о|об|обо|насчет|относительно|связанн\w*|касающ\w*|"
           r"с|со|в|во|на|к|для|из|от|и|а|но|то|же|бы|это|эту|этот|уже|отмет\w+|помет\w+|закро\w+|заверш\w+|"
           r"выполнен\w*|выполнил\w*|сделан\w*|сделал\w*|закончил\w*|готов\w*|галочк\w*|поставь|как|что|которую|который)\b")


_WD_ACC = {"понедельник": "в понедельник", "вторник": "во вторник", "сред": "в среду", "четверг": "в четверг",
           "пятниц": "в пятницу", "суббот": "в субботу", "воскресень": "в воскресенье"}
_RE_REPEAT_WD = re.compile(r"\b(?:кажд(?:ый|ую|ое)|по)\s+(понедельник|вторник|сред|четверг|пятниц|суббот|воскресень)\w*", re.I)
_RE_REPEAT_OTHER = [
    ("weekdays", re.compile(r"\b(?:кажд(?:ый|ые)\s+будн\w+(?:\s+(?:день|дни))?|по\s+будн\w+|в\s+будни)\b", re.I)),
    ("daily", re.compile(r"\b(?:кажд(?:ый|ые)\s+(?:день|дни|сутки)|ежедневно)\b", re.I)),
    ("weekly", re.compile(r"\b(?:кажд(?:ую|ые)\s+недел\w+|еженедельно|раз\s+в\s+неделю)\b", re.I)),
    ("monthly", re.compile(r"\b(?:кажд(?:ый|ые)\s+месяц\w*|ежемесячно|раз\s+в\s+месяц)\b", re.I)),
]


_RE_DAY_NUM = re.compile(r"\b(\d{1,2})(?:-?го)?\s+числа\b", re.I)


def extract_repeat(text: str, today: Optional[date] = None):
    """«Каждый понедельник в 10 планерка» → («в понедельник в 10 планерка», "weekly")."""
    m = _RE_REPEAT_WD.search(text)
    if m:
        stem = m.group(1).lower()
        return text[:m.start()] + _WD_ACC[stem] + text[m.end():], "weekly"
    for rep, rx_ in _RE_REPEAT_OTHER:
        if rx_.search(text):
            if rep == "monthly" and today:                      # «каждый месяц 25 числа» — первое такое число
                dm = _RE_DAY_NUM.search(text)
                if dm and 1 <= int(dm.group(1)) <= 28:
                    day = int(dm.group(1))
                    d0 = today.replace(day=day) if today.day <= day else (today.replace(day=1) + timedelta(days=32)).replace(day=day)
                    text = _RE_DAY_NUM.sub(f"{day} {MONTHS[d0.month - 1]}", text, count=1)
            return re.sub(r"\s+", " ", rx_.sub(" ", text)).strip(), rep
    return text, None


def _kind_from_nouns(n: str) -> Optional[str]:
    found = [k for k, pat in _KIND_NOUNS if re.search(pat, n)]
    return found[0] if len(found) == 1 else None


def _query(n: str) -> str:
    q = re.sub(_Q_DROP, " ", n)
    q = re.sub(r"[^\w\s]", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _title_clean(t: Text, kind: str) -> str:
    t.take_all(r"\b(?:пожалуйста|плиз)\b")
    t.take_all(r"\bнапомни(?:ть)?(?:\s+мне)?\b|\bнапоминай(?:те)?(?:\s+мне)?\b|\bне\s+забудь(?:те)?\b")
    t.take_all(r"\b(?:поставь|поставить|добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|"
               r"внеси|сделай|назначь|назначить)\s+(?:мне\s+)?(?:новую\s+|новое\s+)?(?:задач\w*|напоминани\w*|заметк\w*)?")
    t.take_all(r"\b(?:мне\s+)?(?:нужно|надо|необходимо|придется|не\s+забудь|не\s+забыть|пора)\b")
    t.take_all(r"\bу\s+меня\b")
    rem = t.remainder()
    words = rem.split(" ") if rem else []
    stop = {"в", "на", "к", "до", "по", "около", "и", "что", "чтобы", "а", "с", "во"}
    while words and (words[0].lower().strip(",.:;") in stop or (kind == "reminder" and words[0].lower() in ("о", "про", "об"))):
        words.pop(0)
    while words and words[-1].lower().strip(",.:;") in stop:
        words.pop()
    title = " ".join(words).strip(" .,!?;:-—")
    title = re.sub(r"^(?:[Вв]стречу)\b", "Встреча", title)
    return title[:1].upper() + title[1:] if title else ""


def _list_period(p: Parsed, today: date) -> tuple:
    if p.range:
        return p.range
    if p.date:
        return p.date, p.date
    if p.week_shift:
        mon = monday(today) + timedelta(days=7)
        return mon, mon + timedelta(days=6)
    if p.this_week or p.to_week_end:
        return today, monday(today) + timedelta(days=6)
    if p.weekend:
        sat = monday(today) + timedelta(days=5)
        return (max(sat, today), sat + timedelta(days=1))
    return today, today


def parse(text: str, now: datetime, pending: Optional[dict] = None) -> Command:
    """pending — недостроенная запись из прошлого уточняющего вопроса: {kind,title,date,time}."""
    text = fix_glued((text or "").strip())
    n = text.lower().replace("ё", "е")
    cmd = Command(transcript=text)
    if not n:
        return cmd
    today = now.date()

    if pending and re.search(_RE_CANCEL, n):
        cmd.intent, cmd.reply = "other", "Хорошо, не буду добавлять."
        return cmd
    if re.search(_RE_GREET, n):
        cmd.intent, cmd.reply = "other", _greet_reply(n)
        return cmd
    if re.search(_RE_HELP, n):
        cmd.intent = "other"
        return cmd

    # 0) удалить / перенести запись
    if "?" not in n and re.search(_RE_DELETE, n):
        return _as_target(cmd, text, n, now, "delete")
    if "?" not in n and re.search(_RE_UPDATE, n):
        return _as_target(cmd, text, n, now, "update")

    # 1) отметить выполненной
    if "?" not in n and re.search(_RE_COMPLETE, n):
        t = Text(text)
        scan_when(t, now)
        cmd.intent, cmd.kind, cmd.query = "complete", _kind_from_nouns(n), _query(t.remainder_norm())
        return cmd

    # 2) поиск по явному глаголу
    if re.search(_RE_FIND, n):
        return _as_search(cmd, text, n, now)

    # 3) список за период
    if any(re.search(p, n) for p in _RE_LIST):
        t = Text(text)
        p = scan_when(t, now)
        has_period = bool(p.date or p.range or p.week_shift or p.this_week or p.weekend or p.to_week_end)
        rest = _query(re.sub(_RE_ALL, " ", t.remainder_norm()))
        if not has_period and rest and re.search(r"\bпокажи|показать|выведи|назови|перечисли\b", n):
            return _as_search(cmd, text, n, now)          # «покажи задачу про договор» — это поиск
        cmd.intent, cmd.kind = "list", _kind_from_nouns(n)
        if not has_period and not rest and (re.search(_RE_ALL, n) or cmd.kind or re.search(r"\bесть\b|\bсколько\b|\bмо[иеёя]\b", n)):
            cmd.range_start = "all"                       # период не назван: «покажи все задачи», «какие у меня встречи?»
            return cmd
        start, end = _list_period(p, today)
        cmd.range_start, cmd.range_end = start.isoformat(), end.isoformat()
        return cmd

    # 4) «когда была встреча с Иваном?»
    if re.search(_RE_WHEN_SEARCH, n):
        return _as_search(cmd, text, n, now)

    # 5) ответ на уточняющий вопрос (если это не новая команда «напомни…», «поставь встречу…»)
    if pending and pending.get("op") == "move":      # ответ на «На какую дату и время перенести?»
        t = Text(text)
        p = scan_when(t, now)
        if p.date or p.time or p.week_shift or p.to_week_end:
            d = p.date or (monday(today) + timedelta(days=7) if p.week_shift else None)
            if d is None and p.to_week_end:
                d = monday(today) + timedelta(days=6)
            cmd.intent, cmd.ref = "update", "it"
            cmd.date, cmd.time = (d.isoformat() if d else None), p.time
            return cmd
        pending = None
    if pending and not re.search(_RE_CREATE_VERB, n) and not re.search(_RE_MEETING, n):
        t = Text(text)
        p = scan_when(t, now)
        if p.time is None and not pending.get("time"):
            b = _BARE_TIME.match(n)             # просто «8», «восемь», «8 30»
            if b:
                raw_h = b.group("h")
                h = int(raw_h) if raw_h.isdigit() else NUMS.get(raw_h, -1)
                if 0 <= h <= 23:
                    p.time = f"{to24(h, None, raw_h):02d}:{b.group('m') or '00'}"
        if p.date or p.time or p.to_week_end or p.week_shift:
            return _build_create(cmd, t, p, now, pending=pending)
        tries = pending.get("_tries", 0) + 1
        if tries < 2:                           # не теряем вопрос: переспрашиваем, а не создаём задачу
            pending["_tries"] = tries
            cmd.intent = "clarify"
            cmd.reply = ("Не понял. Скажите день и время, например «в 10 утра», «в 15:30» или «завтра в три».")
            return cmd

    # 6) создание
    text, repeat = extract_repeat(text, today)
    n = text.lower().replace("ё", "е")
    has_verb = bool(re.search(_RE_CREATE_VERB, n))
    t = Text(text)
    p = scan_when(t, now)
    has_when = bool(p.date or p.time or p.to_week_end or p.week_shift)
    is_meeting = bool(re.search(_RE_MEETING, n))
    if "?" in n and not has_verb:
        cmd.intent = "other"
        return cmd
    if not (has_verb or has_when or is_meeting) and len(n.split()) < 2:
        cmd.intent = "other"
        return cmd
    cmd = _build_create(cmd, t, p, now, pending=None)
    cmd.repeat = repeat
    return cmd


_TARGET_DROP = (r"\b(?:пожалуйста|удали\w*|сотри|стери|убери|очисти\w*|отмени\w*|перенеси\w*|перенести|перемести\w*|передвин\w*|"
                r"сдвин\w*|отложи\w*|поменяй\w*|измени\w*|время|дат[ауы]|последн\w+|заметк\w*|запис\w*|ее|его|их|эту|этот|это|ту|тот|"
                r"мне|мою|мой|моя|мое|мои|про|по|о|об|с|со|в|во|на|к|и|а|для|из|от|у|меня)\b")


def _as_target(cmd: Command, text: str, n: str, now: datetime, intent: str) -> Command:
    """«Удали задачу про договор», «перенеси встречу с Иваном на пятницу в 15:00», «перенеси её на завтра»."""
    t = Text(text)
    p = scan_when(t, now)
    rest = t.remainder_norm()
    d = p.date or (monday(now.date()) + timedelta(days=7) if p.week_shift else None)
    if d is None and p.to_week_end:
        d = monday(now.date()) + timedelta(days=6)
    cmd.intent, cmd.kind = intent, _kind_from_nouns(n)
    q = re.sub(_TARGET_DROP, " ", re.sub(_Q_KIND, " ", rest))
    cmd.query = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", q)).strip()
    if not cmd.query:
        cmd.ref = "last" if re.search(r"\bпоследн\w+", n) else "it"
    if intent == "update":
        cmd.date, cmd.time = (d.isoformat() if d else None), p.time
    return cmd


_Q_KIND = r"\b(?:задач\w*|встреч\w*|напоминани\w*)\b"


def _as_search(cmd: Command, text: str, n: str, now: datetime) -> Command:
    t = Text(text)
    scan_when(t, now)                      # даты из запроса не нужны в ключевых словах
    cmd.intent, cmd.kind, cmd.query = "search", _kind_from_nouns(n), _query(t.remainder_norm())
    return cmd


def _build_create(cmd: Command, t: Text, p: Parsed, now: datetime, pending: Optional[dict]) -> Command:
    today = now.date()
    n = t.norm
    if pending:
        kind = pending.get("kind") or "task"
        title = pending.get("title") or ""
        d = p.date or (parse_iso(pending.get("date")))
        tm = p.time or pending.get("time")
        if p.to_week_end and not d:
            d = monday(today) + timedelta(days=6)
        cmd.intent, cmd.kind, cmd.title = "create", kind, title
        cmd.date, cmd.time = (d.isoformat() if d else None), tm
        cmd.repeat = pending.get("repeat")
        return cmd

    if re.search(_RE_REMINDER, n):
        kind = "reminder"
    elif re.search(_RE_MEETING, n):
        kind = "meeting"
    else:
        kind = "task"
    d = p.date
    if d is None and p.to_week_end:
        d = monday(today) + timedelta(days=6)
    elif d is None and p.week_shift:
        d = monday(today) + timedelta(days=7)
    elif d is None and p.weekend:
        d = max(monday(today) + timedelta(days=5), today)
    tm = p.time
    if d is None and tm and kind in ("reminder", "task"):   # «напомни в 10 …» — ближайшее такое время
        d = today if tm > now.strftime("%H:%M") else today + timedelta(days=1)
    title = _title_clean(t, kind)
    cmd.intent, cmd.kind, cmd.title = "create", kind, title or None
    cmd.date, cmd.time = (d.isoformat() if d else None), tm
    return cmd


def parse_iso(v: Optional[str]) -> Optional[date]:
    try:
        return datetime.strptime((v or "")[:10], "%Y-%m-%d").date() if v else None
    except ValueError:
        return None
