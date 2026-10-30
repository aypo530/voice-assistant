"""Нечёткий поиск по записям: основы слов, группы синонимов, опечатки. Без внешних зависимостей."""
import difflib
import re

from .store import type_match

ENDINGS = re.compile(r"(иями|ями|ами|ого|его|ому|ему|ыми|ими|ией|ием|ию|ия|ах|ях|ов|ев|ей|ой|ом|ем|ам|ям|ую|юю|ая|яя|ое|ее|"
                     r"ые|ие|ый|ий|ть|ти|ет|ут|ют|ит|ат|ят|ал|ла|ли|а|я|у|ю|е|ы|и|о|ь|й)$")

GROUPS = [
    {"договор", "контракт", "соглашени"},
    {"позвони", "звонок", "созвон", "звони", "звон"},
    {"презентаци", "слайд", "доклад", "питч"},
    {"отчет", "репорт", "сводк"},
    {"врач", "доктор", "поликлиник", "медик", "прием"},
    {"купи", "покупк", "магазин", "заказ"},
    {"встреч", "митинг", "совещан", "планерк", "переговор"},
    {"оплат", "плат", "счет", "оплати"},
    {"письм", "почт", "емейл", "email", "мейл"},
]


def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if len(w) <= 4:
        return w
    s = ENDINGS.sub("", w)
    return s if len(s) >= 3 else w


def _canon(s):
    for i, g in enumerate(GROUPS):
        if any(s.startswith(x) or x.startswith(s) for x in g if min(len(s), len(x)) >= 4):
            return i
    return None


def _sim(a, b):
    if a == b:
        return 1.0
    ca, cb = _canon(a), _canon(b)
    if ca is not None and ca == cb:
        return 0.9
    if min(len(a), len(b)) >= 4 and (a.startswith(b) or b.startswith(a)):
        return 0.9
    r = difflib.SequenceMatcher(None, a, b).ratio()
    return r if r >= 0.75 else 0.0


def tokens(text: str):
    return [stem(w) for w in re.findall(r"[\w]+", text.lower()) if len(w) > 1]


def score(query_tokens, item) -> float:
    it = tokens(item["title"])
    if not query_tokens or not it:
        return 0.0
    return sum(max(_sim(q, t) for t in it) for q in query_tokens) / len(query_tokens)


def find(items, query: str, type_=None, limit=5, min_score=0.6):
    """Возвращает [(score, item)] по убыванию похожести. Пустой запрос — последние записи нужного типа."""
    pool = [i for i in items if type_match(i["type"], type_)]
    q = tokens(query or "")
    if not q:
        return [(0.0, i) for i in sorted(pool, key=lambda i: -i["id"])[:limit]]
    res = [(score(q, i), i) for i in pool]
    res = [(s, i) for s, i in res if s >= min_score]
    res.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return res[:limit]
