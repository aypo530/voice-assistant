"""HTTP-слой: тонкий Flask-сервер. Вся логика — в пакете assistant/."""
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    """Мини-загрузчик .env (без внешних зависимостей). Переменные окружения платформы приоритетнее."""
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

from assistant.llm import GigaChat  # noqa: E402
from assistant.service import Assistant  # noqa: E402
from assistant.store import Store  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DB_PATH = os.getenv("DB_PATH") or ("/data/assistant.db" if os.path.isdir("/data") else os.path.join(BASE, "data", "assistant.db"))
store = Store(DB_PATH)
llm = GigaChat(
    auth_key=os.getenv("GIGACHAT_AUTH_KEY"),
    client_id=os.getenv("GIGACHAT_CLIENT_ID"), client_secret=os.getenv("GIGACHAT_CLIENT_SECRET"),
    scope=os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"), model=os.getenv("GIGACHAT_MODEL", "GigaChat"),
    verify_ssl=os.getenv("GIGACHAT_VERIFY_SSL", "0") == "1", ca_bundle=os.getenv("GIGACHAT_CA_BUNDLE") or None,
)
assistant = Assistant(store, llm if llm.enabled else None)

app = Flask(__name__, static_folder=os.path.join(BASE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024
UID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def _user(data=None):
    u = (data or {}).get("user") or request.args.get("user") or ""
    return u if UID_RE.match(u) else None


def _now(raw):
    """Время берём у клиента (его часовой пояс); при ошибке — Москва."""
    utc = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        n = datetime.fromisoformat(str(raw)[:19])
        if abs((n - utc).total_seconds()) < 36 * 3600:
            return n
    except ValueError:
        pass
    return utc + timedelta(hours=3)


@app.get("/")
def index():
    r = send_from_directory(app.static_folder, "index.html")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/health")
def health():
    return jsonify(ok=True, llm=llm.enabled, llm_ready=llm.available())


@app.post("/api/message")
def message():
    data = request.get_json(silent=True) or {}
    user = _user(data)
    if not user:
        return jsonify(error="bad user"), 400
    try:
        res = assistant.handle(user, str(data.get("text", "")), _now(data.get("now")))
    except Exception:
        logging.exception("handle failed")
        res = {"reply": "Что-то пошло не так. Попробуйте переформулировать запрос.", "speech": "Что-то пошло не так.",
               "intent": "error", "source": "rules", "refresh": False}
    return jsonify(res)


@app.get("/api/items")
def items():
    user = _user()
    if not user:
        return jsonify(error="bad user"), 400
    status = request.args.get("status")
    return jsonify(items=store.all(user, status if status in ("open", "done") else None))


@app.post("/api/items/<int:item_id>/done")
def done(item_id):
    user = _user(request.get_json(silent=True))
    if not user:
        return jsonify(error="bad user"), 400
    store.set_status(user, item_id, "done")
    return jsonify(ok=True)


@app.delete("/api/items/<int:item_id>")
def remove(item_id):
    user = _user()
    if not user:
        return jsonify(error="bad user"), 400
    store.delete(user, item_id)
    return jsonify(ok=True)


@app.get("/api/export.csv")
def export():
    user = _user()
    if not user:
        return jsonify(error="bad user"), 400
    return Response(store.to_csv(user), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=assistant_items.csv"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
