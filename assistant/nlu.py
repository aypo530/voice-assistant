"""Понимание команд правилами. Работает без нейросети: это и запасной режим, и надёжный разбор дат.

Порядок проверки важен: «поставь задачу найти договор» — это создание, а не поиск.
"""
import re
from datetime import datetime

from . import dates

R_DONE = re.compile(r"\b(отмет\w+|пометь|закрой|выполнил\w*|сделал\w*|вычеркни)\b")
R_DELETE = re.compile(r"\b(удали\w*|убери|сотри|отмени\w*)\b")
R_MOVE = re.compile(r"\b(перенес\w+|перенеси|передвин\w+|сдвин\w+|перепланир\w+)\b")
R_CREATE = re.compile(r"\b(напомни\w*|добав\w+|постав\w+|созда\w+|запиши\w*|запланируй\w*|не\s+забуд\w+|не\s+забыть)\b")
R_SEARCH = re.compile(r"\b(найд\w+|найти|поищ\w+|поиск\w*|отыщ\w+|вспомни|давно|раньше|ранее|когда[-\s]то)\b|^\W*когда\b")
R_LIST = re.compile(
    r"(что\s+у\s+меня|что\s+(?:запланировано|на\s|по\s+плану|там)|какие\s+(?:у\s+меня\s+)?(?:планы|задачи|встречи|дела|напоминания)|"
    r"есть\s+ли|покажи|мои\s+(?:задачи|встречи|дела|напоминания)|расписани|\bпланы\b|план\s+на|\bсписок\b|чем\s+занят|"
    r"сколько\s+(?:у\s+меня\s+)?(?:задач|встреч)|дела\s+на|что\s+мне\s+делать|что\s+планируется)")
R_IMPLICIT = re.compile(r"\b(нужно|надо|необходимо|встреч\w+|созвон\w*|совещани\w+|собеседовани\w+|планерк\w+|звонок)\b")
R_MEETING = re.compile(r"встреч|созвон|совещан|собеседован|планерк|митинг|консультаци|прием\s+у")
R_CANCEL = re.compile(r"\b(отмена|отмени|не\s+надо|не\s+нужно|забудь|стоп|неважно)\b")

STOP_TOKEN = re.compile(
    r"^(найд\w*|найти|поищ\w*|поиск\w*|отыщ\w*|вспомни|когда(-то)?|давно|раньше|ранее|задач\w*|встреч\w*|напоминани\w*|"
    r"мне|меня|мой|моя|мои|мою|у|я|про|по|о|об|на|в|во|с|со|к|ее|его|их|была|было|был|были|будет|ставил\w*|"
    r"создавал\w*|пожалуйста|какая|какой|ту|эту|тот|этот|что|это|отмет\w*|пометь|закрой|выполнен\w*|выполнил\w*|"
    r"сделал\w*|удали\w*|убери|сотри|отмени\w*|перенес\w*|перенеси|передвин\w*|сдвин\w*|вычеркни|и|а|ли|же|мое|моё)$")

FILLER = re.compile(
    r"\b(пожалуйста|напомни(?:ть)?(?:\s+мне)?|добавь|добавить|поставь|поставить|создай|создать|запиши|записать|"
    r"запланируй|мне|у\s+меня|нужно|надо|необходимо|не\s+забудь|не\s+забыть|задачу|задача|напоминание|будет|что|чтобы)\b",
    re.I)


def _cut(text, spans):
    for s, e in sorted(spans, reverse=True):
        text = text[:s] + " " + text[e:]
    return text


def keywords(text, spans=()):
    """Содержательные слова запроса (без команд, служебных слов и дат)."""
    words = re.findall(r"[\w-]+", _cut(text, spans))
    return " ".join(w for w in words if not STOP_TOKEN.match(dates.norm(w)))


def clean_title(text, spans, type_):
    t = _cut(text, spans)
    t = FILLER.sub(" ", t)
    if type_ == "meeting":
        t = re.sub(r"\bвстречу\b", "встреча", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,.;:!?-—")
    while True:
        n = re.sub(r"^(в|на|до|к|по|у)\s+", "", t, flags=re.I).strip(" ,.;:!?-—")
        if n == t:
            break
        t = n
    t = re.sub(r"\s+(в|на|до|к|по)$", "", t, flags=re.I)
    return (t[:1].upper() + t[1:]) if t else ""


def detect_type(t):
    if R_MEETING.search(t):
        return "meeting"
    if "напомни" in t:
        return "reminder"
    return "task"


def filter_type(t):
    if "встреч" in t or "созвон" in t:
        return "meeting"
    if "задач" in t or "дел" in t:
        return "task"
    if "напоминан" in t:
        return "reminder"
    return None


def rule_parse(text: str, now: datetime) -> dict:
    t = dates.norm(text)
    P = dates.parse(text, now)
    out = {"intent": "unknown", "type": None, "title": None, "date": P.date, "time": P.time,
           "range": P.span or ((P.date, P.date) if P.date else None), "deadline": P.deadline,
           "query": None, "filter_type": filter_type(t), "source": "rules"}

    if R_MOVE.search(t):
        out.update(intent="move", query=keywords(text, P.spans))
    elif R_DELETE.search(t):
        out.update(intent="delete", query=keywords(text, P.spans))
    elif R_DONE.search(t):
        out.update(intent="complete", query=keywords(text, P.spans))
    elif R_CREATE.search(t):
        out["intent"] = "create"
    elif R_SEARCH.search(t):
        out.update(intent="search", query=keywords(text, P.spans))
    elif R_LIST.search(t):
        out["intent"] = "list"
    elif R_IMPLICIT.search(t) or (P.found and len(re.findall(r"\w+", t)) >= 3 and not t.rstrip().endswith("?")):
        out["intent"] = "create"

    if out["intent"] == "create":
        out["type"] = detect_type(t)
        out["title"] = clean_title(text, P.spans, out["type"])
        if not out["title"] and out["type"] == "meeting":
            out["title"] = "Встреча"
    return out
