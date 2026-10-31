"""Поиск по записям: слова (с учётом падежей) → поиск по смыслу.

Поиск по смыслу работает в три уровня, каждый следующий включается автоматически, если предыдущий недоступен:
  1) эмбеддинги GigaChat (облако, самые точные);
  2) локальная многоязычная модель эмбеддингов (fastembed, офлайн, нужна однократная загрузка модели);
  3) n-граммы символов + словарь синонимов (без зависимостей, работает всегда).
"""
import logging
import math
import re
import threading
from collections import Counter
from typing import Optional

import config
import llm
from textutil import match_score, query_stems, words

log = logging.getLogger("search")

# Минимальная похожесть для «по смыслу» (подбирается под модель; можно править при необходимости).
_FLOOR = {"gigachat": config.SEARCH_FLOOR_CLOUD, "local-embed": 0.35, "ngram": 0.18}

# Группы синонимов: корни слов, считающиеся близкими по смыслу (используется только уровнем 3).
SYNONYMS = [
    ("договор", "контракт", "соглашен", "подписать"),
    ("презентац", "доклад", "слайд", "выступлен"),
    ("отчет", "отчёт", "справк", "сводк", "документ"),
    ("встреч", "созвон", "совещан", "планерк", "митинг", "переговор"),
    ("врач", "доктор", "поликлиник", "стоматолог", "терапевт", "больниц", "приём", "прием"),
    ("купить", "покупк", "заказать", "приобрест", "магазин"),
    ("билет", "авиабилет", "поезд", "рейс", "самолет", "самолёт"),
    ("позвонить", "звонок", "созвониться", "набрать", "телефон"),
    ("письмо", "почт", "email", "емейл", "написать"),
    ("оплатить", "оплат", "счет", "счёт", "платеж", "платёж", "квитанц"),
    ("деньги", "перевод", "перевести", "банк", "карта"),
    ("уборк", "убрать", "пропылесосить", "помыть"),
    ("тренировк", "спортзал", "зал", "фитнес", "бег", "пробежк"),
    ("день рождения", "подарок", "поздравить", "праздник"),
    ("машин", "авто", "автомобил", "техосмотр", "шиномонтаж"),
]
_STOP_NG = {"про", "что", "для", "как", "это", "или", "при", "над", "под", "без", "найди", "задача", "встреча"}


def _expand(text: str) -> str:
    ws = words(text)
    extra = []
    for grp in SYNONYMS:
        if any(w.startswith(s.replace("ё", "е")[:6]) for w in ws for s in grp):
            extra += list(grp)
    return text + " " + " ".join(extra) if extra else text


def _grams(text: str) -> Counter:
    c: Counter = Counter()
    for w in words(text):
        if len(w) < 2 or w in _STOP_NG:
            continue
        p = f"_{w}_"
        c.update(p[i:i + 3] for i in range(len(p) - 2))
    return c


def _cos(a, b) -> float:
    if isinstance(a, Counter):
        dot = sum(v * b.get(k, 0) for k, v in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b))
        na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _ngram_rank(query: str, items: list) -> list:
    docs = [_grams(_expand(i["title"])) for i in items]
    q = _grams(_expand(query))
    n = len(docs) + 1
    df: Counter = Counter()
    for d in docs + [q]:
        df.update(d.keys())
    idf = {k: math.log(n / v) + 1.0 for k, v in df.items()}
    wq = Counter({k: v * idf[k] for k, v in q.items()})
    out = []
    for it, d in zip(items, docs):
        wd = Counter({k: v * idf[k] for k, v in d.items()})
        out.append((it, _cos(wq, wd)))
    return out


# ---------- эмбеддинги ----------
_cache: dict = {}                    # (провайдер, текст) -> вектор
_local = {"model": None, "state": "unknown", "message": ""}
_local_lock = threading.Lock()


def _load_local():
    with _local_lock:
        if _local["model"] is not None:
            return _local["model"]
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            _local.update(state="missing", message="fastembed не установлен")
            raise RuntimeError("fastembed не установлен") from e
        try:
            _local["state"] = "loading"
            config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            _local["model"] = TextEmbedding(model_name=config.LOCAL_EMBED_MODEL, cache_dir=str(config.MODELS_DIR))
            _local.update(state="ready", message="")
        except Exception as e:
            _local.update(state="error", message=str(e)[:200])
            raise
        return _local["model"]


def preload_async() -> None:
    def run():
        try:
            _load_local()
        except Exception as e:
            log.info("Локальная модель эмбеддингов недоступна (%s) — будет использован n-граммный поиск", e)
    threading.Thread(target=run, daemon=True, name="embed-preload").start()


def local_state() -> dict:
    return {"state": _local["state"], "message": _local["message"]}


def _embed(provider: str, texts: list, is_query: bool) -> list:
    """Эмбеддинги с кэшем. GigaChat кодирует запрос и документы одинаково, поэтому кэш общий."""
    todo = [t for t in dict.fromkeys(texts) if (provider, is_query, t) not in _cache]
    if todo:
        if provider == "gigachat":
            vecs = llm.embed(todo, query=is_query)
        else:
            vecs = [list(map(float, v)) for v in _load_local().embed(todo)]
        for t, v in zip(todo, vecs):
            _cache[(provider, is_query, t)] = v
    return [_cache[(provider, is_query, t)] for t in texts]


def _vector_rank(provider: str, query: str, items: list) -> list:
    q = _embed(provider, [query], True)[0]
    docs = _embed(provider, [i["title"] for i in items], False)
    return [(it, _cos(q, d)) for it, d in zip(items, docs)]


def semantic(query: str, items: list, limit: int = 5) -> tuple:
    """Возвращает (провайдер, [(запись, похожесть)...]) — только достаточно похожие, лучшие первыми."""
    if not items:
        return "none", []
    tiers = []
    if llm.usable():
        tiers.append("gigachat")
    if _local["state"] != "missing":
        tiers.append("local-embed")
    tiers.append("ngram")
    for provider in tiers:
        try:
            ranked = _ngram_rank(query, items) if provider == "ngram" else _vector_rank(provider, query, items)
        except Exception as e:
            log.info("Поиск по смыслу (%s) недоступен: %s", provider, e)
            continue
        ranked = [(it, s) for it, s in ranked if s >= _FLOOR[provider]]
        ranked.sort(key=lambda x: -x[1])
        if ranked:
            top = ranked[0][1]
            ranked = [(it, s) for it, s in ranked if s >= top - 0.12][:limit]
        return provider, ranked
    return "none", []


def find(query: str, items: list, limit: int = 10) -> tuple:
    """Главная функция поиска. Возвращает (записи, режим): 'exact' — совпали слова, 'semantic' — по смыслу, 'none'."""
    query = (query or "").strip()
    if not items:
        return [], "none"
    stems = query_stems(query)
    if not stems:
        return sorted(items, key=lambda r: r["id"])[-limit:][::-1], "exact"      # «найди мою встречу» — последние
    scored = [(match_score(stems, r["title"]), r) for r in items]
    hits = [r for s, r in sorted(scored, key=lambda x: -x[0]) if s > 0][:limit]
    if hits:
        return hits, "exact"
    _, ranked = semantic(query, sorted(items, key=lambda r: r["id"])[-150:])
    return [it for it, _ in ranked], ("semantic" if ranked else "none")
