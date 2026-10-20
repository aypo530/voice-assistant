"""Слой хранения: один CSV-файл (assistant_data.csv). Только чтение/запись, без бизнес-логики.

Столбцы: id;type;title;date;time;status;created_at;user_id. Файл открывается в Excel (UTF-8 с BOM, разделитель «;»).
Новые записи дописываются в конец файла; правка и удаление переписывают файл целиком через временный файл
(os.replace), поэтому при сбое данные не теряются.
В режиме MULTI_USER (публичный сайт) каждый пользователь видит только свои записи.
"""
import csv
import os
import tempfile
import threading
from typing import Iterable, Optional

import config

FIELDS = ["id", "type", "title", "date", "time", "status", "created_at", "user_id"]
STATUSES = ("open", "done")
_lock = threading.RLock()


class StorageError(Exception):
    """Файл данных недоступен (например, открыт в Excel и заблокирован)."""


def init() -> None:
    path = config.DATA_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                csv.writer(f, delimiter=";").writerow(FIELDS)
    except OSError as e:
        raise StorageError(f"Не удалось создать файл данных {path}: {e}") from e


def _read() -> list:
    init()
    try:
        with open(config.DATA_FILE, encoding="utf-8-sig", newline="") as f:
            raw = list(csv.DictReader(f, delimiter=";"))
    except OSError as e:
        raise StorageError(f"Не удалось прочитать файл {config.DATA_FILE.name}: {e}") from e
    rows = []
    for r in raw:
        if not (r.get("title") or "").strip():
            continue
        try:
            rid = int(r.get("id") or 0)
        except ValueError:
            rid = 0
        status = (r.get("status") or "open").strip()
        rows.append({"id": rid, "kind": (r.get("type") or "task").strip(), "title": r["title"].strip(),
                     "due_date": (r.get("date") or "").strip() or None,
                     "due_time": (r.get("time") or "").strip() or None,
                     "status": status if status in STATUSES else "open",
                     "created_at": (r.get("created_at") or "").strip(),
                     "user_id": (r.get("user_id") or "").strip()})
    return rows


def _row_to_csv(r: dict) -> list:
    return [r["id"], r["kind"], r["title"], r["due_date"] or "", r["due_time"] or "", r["status"],
            r["created_at"], r.get("user_id", "")]


def _write_all(rows: list) -> None:
    """Атомарная перезапись файла."""
    path = config.DATA_FILE
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(FIELDS)
            for r in rows:
                w.writerow(_row_to_csv(r))
        os.replace(tmp_name, path)
    except PermissionError as e:
        raise StorageError(f"Файл {path.name} занят другой программой. Закройте его (например, в Excel) и повторите.") from e
    except OSError as e:
        raise StorageError(f"Не удалось записать файл {path.name}: {e}") from e
    finally:
        if tmp_name and os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _visible(rows: list, user: str) -> list:
    if not config.MULTI_USER:
        return rows
    return [r for r in rows if r["user_id"] in ("", user)]


def _filter_kinds(rows: list, kinds: Optional[Iterable[str]]) -> list:
    kinds = tuple(kinds or ())
    return [r for r in rows if r["kind"] in kinds] if kinds else rows


def _sort_key(r: dict):
    return (r["due_date"] is None, r["due_date"] or "", r["due_time"] or "", r["id"])


def add_item(user: str, kind: str, title: str, due_date: Optional[str], due_time: Optional[str], created_at: str) -> dict:
    with _lock:
        new_id = max((r["id"] for r in _read()), default=0) + 1
        row = {"id": new_id, "kind": kind, "title": title, "due_date": due_date, "due_time": due_time,
               "status": "open", "created_at": created_at, "user_id": user}
        try:
            with open(config.DATA_FILE, "a", encoding="utf-8", newline="") as f:
                csv.writer(f, delimiter=";").writerow(_row_to_csv(row))
        except PermissionError as e:
            raise StorageError(f"Файл {config.DATA_FILE.name} занят другой программой. "
                               "Закройте его (например, в Excel) и повторите запрос.") from e
        except OSError as e:
            raise StorageError(f"Не удалось записать в файл {config.DATA_FILE.name}: {e}") from e
        return row


def get(user: str, item_id: int) -> Optional[dict]:
    with _lock:
        for r in _visible(_read(), user):
            if r["id"] == item_id:
                return r
    return None


def update(user: str, item_id: int, **fields) -> Optional[dict]:
    """Меняет поля записи (kind, title, due_date, due_time, status). None — если записи нет."""
    allowed = {"kind", "title", "due_date", "due_time", "status"}
    with _lock:
        rows = _read()
        target = next((r for r in _visible(rows, user) if r["id"] == item_id), None)
        if target is None:
            return None
        target.update({k: v for k, v in fields.items() if k in allowed})
        _write_all(rows)
        return dict(target)


def delete(user: str, item_id: int) -> bool:
    with _lock:
        rows = _read()
        target = next((r for r in _visible(rows, user) if r["id"] == item_id), None)
        if target is None:
            return False
        rows.remove(target)
        _write_all(rows)
        return True


def find_duplicate(user: str, kind: str, title: str, due_date: Optional[str], due_time: Optional[str]) -> Optional[dict]:
    with _lock:
        for r in _visible(_read(), user):
            if (r["kind"] == kind and r["due_date"] == due_date and r["due_time"] == due_time
                    and r["title"].casefold() == title.casefold()):
                return r
    return None


def find_meetings_at(user: str, due_date: str, due_time: str) -> list:
    """Невыполненные встречи на то же время (для предупреждения о конфликте)."""
    with _lock:
        return [r for r in _visible(_read(), user)
                if r["kind"] == "meeting" and r["status"] != "done" and r["due_date"] == due_date and r["due_time"] == due_time]


def list_range(user: str, start: str, end: str, kinds: Optional[Iterable[str]] = None) -> list:
    with _lock:
        rows = [r for r in _filter_kinds(_visible(_read(), user), kinds) if r["due_date"] and start <= r["due_date"] <= end]
    return sorted(rows, key=_sort_key)


def all_items(user: str, kinds: Optional[Iterable[str]] = None, limit: Optional[int] = None) -> list:
    with _lock:
        rows = _filter_kinds(_visible(_read(), user), kinds)
    rows.sort(key=_sort_key)
    return rows[:int(limit)] if limit else rows
