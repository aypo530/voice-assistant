"""Хранение записей: SQLite (один файл, сервер БД не нужен). Каждая запись принадлежит пользователю."""
import csv
import io
import os
import sqlite3

def type_match(item_type, flt):
    """Фильтр «задачи» включает и напоминания: для пользователя это одно и то же."""
    return not flt or item_type == flt or (flt == "task" and item_type == "reminder")


SCHEMA = """CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, type TEXT NOT NULL,
  title TEXT NOT NULL, date TEXT, time TEXT, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_user_date ON items(user_id, date);"""


class Store:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        c = self._c()
        c.executescript(SCHEMA)
        c.commit()
        c.close()

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _run(self, sql, args=(), fetch=None):
        c = self._c()
        try:
            cur = c.execute(sql, args)
            out = None
            if fetch == "all":
                out = [dict(r) for r in cur.fetchall()]
            elif fetch == "one":
                r = cur.fetchone()
                out = dict(r) if r else None
            elif fetch == "id":
                out = cur.lastrowid
            c.commit()
            return out
        finally:
            c.close()

    def add(self, user, type_, title, date, time, created_at) -> int:
        return self._run("INSERT INTO items(user_id,type,title,date,time,created_at) VALUES(?,?,?,?,?,?)",
                         (user, type_, title, date, time, created_at), "id")

    def get(self, user, item_id):
        return self._run("SELECT * FROM items WHERE id=? AND user_id=?", (item_id, user), "one")

    def all(self, user, status=None):
        q, a = "SELECT * FROM items WHERE user_id=?", [user]
        if status:
            q += " AND status=?"
            a.append(status)
        q += " ORDER BY (date IS NULL), date, COALESCE(time,'99:99'), id"
        return self._run(q, a, "all")

    def between(self, user, start, end, type_=None, status="open"):
        rows = [r for r in self.all(user, status) if r["date"] and start <= r["date"] <= end]
        return [r for r in rows if type_match(r["type"], type_)]

    def upcoming(self, user, from_date, type_=None):
        rows = [r for r in self.all(user, "open") if (not r["date"]) or r["date"] >= from_date]
        return [r for r in rows if type_match(r["type"], type_)]

    def set_status(self, user, item_id, status):
        self._run("UPDATE items SET status=? WHERE id=? AND user_id=?", (status, item_id, user))

    def move(self, user, item_id, date, time):
        self._run("UPDATE items SET date=?, time=? WHERE id=? AND user_id=?", (date, time, item_id, user))

    def delete(self, user, item_id):
        self._run("DELETE FROM items WHERE id=? AND user_id=?", (item_id, user))

    def to_csv(self, user) -> str:
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["id", "type", "title", "date", "time", "status", "created_at"])
        for r in self.all(user):
            w.writerow([r["id"], r["type"], r["title"], r["date"] or "", r["time"] or "", r["status"], r["created_at"]])
        return "\ufeff" + buf.getvalue()
