"""HTTP-слой (FastAPI): принимает текст или аудио, отдаёт ответ ассистента; правка и удаление записей. Логики здесь нет."""
import csv
import io
import logging
import re
import threading
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

import config
import llm
import search
import storage
import stt_local
from assistant import Assistant

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")
assistant = Assistant()


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        storage.init()
    except storage.StorageError as e:
        log.error("%s", e)
    if llm.has_key():
        threading.Thread(target=llm.probe, daemon=True, name="gemini-probe").start()
    else:
        log.warning("GEMINI_API_KEY не задан — работаю в локальном режиме (точность ниже).")
    if config.PRELOAD_LOCAL:
        stt_local.preload_async()
        search.preload_async()
    yield


app = FastAPI(title="Голосовой ассистент задач и встреч", lifespan=lifespan)


class UserKeyMiddleware:
    """Кладёт ключ Gemini пользователя (заголовок X-Gemini-Key) в контекст запроса. Ключ нигде не сохраняется и не логируется."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for k, v in scope.get("headers", []):
                if k == b"x-gemini-key":
                    llm.set_request_key(v.decode("latin-1"))
                elif k == b"x-mode":
                    llm.set_request_mode(v.decode("latin-1").strip().lower())
        await self.inner(scope, receive, send)


app.add_middleware(UserKeyMiddleware)


class TextIn(BaseModel):
    session_id: str
    text: str
    tz: Optional[str] = None


class ItemIn(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    status: Optional[str] = None


def _uid(request: Request) -> str:
    return (request.headers.get("x-session-id") or "anon")[:64]


@app.get("/")
def index():
    return FileResponse(config.RESOURCE_DIR / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    cloud = llm.status()
    return {"status": "ok", "cloud": cloud, "mode": llm.current_mode(), "server_key": bool(config.GEMINI_API_KEY), "local_stt": stt_local.state(),
            "local_embed": search.local_state(), "multi_user": config.MULTI_USER}


class KeyIn(BaseModel):
    key: str


@app.post("/api/key/check")
async def key_check(body: KeyIn):
    """Проверяет введённый пользователем ключ Gemini. Ничего не сохраняет."""
    key = body.key.strip()
    if not key or len(key) > 200 or re.search(r"\s", key):
        return {"state": "auth", "message": "Это не похоже на ключ Gemini."}
    return await run_in_threadpool(llm.check_key, key)


@app.post("/api/message")
async def message(body: TextIn):
    text = body.text.strip()[:1000]
    if not text:
        return JSONResponse({"ok": False, "reply": "Пустой запрос.", "transcript": "", "intent": "error"})
    return await run_in_threadpool(assistant.handle, body.session_id[:64], body.tz, text)


@app.post("/api/voice")
async def voice(request: Request):
    audio = await request.body()
    sid = _uid(request)
    tz = request.headers.get("x-timezone")
    mime = (request.headers.get("content-type") or "audio/wav").split(";")[0].strip()
    if len(audio) > config.MAX_AUDIO_BYTES:
        return JSONResponse({"ok": False, "reply": "Запись слишком длинная (максимум около минуты).",
                             "transcript": "", "intent": "error"})
    if len(audio) < 2000:
        return JSONResponse({"ok": True, "reply": "Запись получилась слишком короткой. Попробуйте ещё раз.",
                             "transcript": "", "intent": "other"})
    return await run_in_threadpool(assistant.handle, sid, tz, None, audio, mime)


@app.get("/api/items")
def items(request: Request):
    return storage.all_items(_uid(request))


@app.get("/api/search")
async def search_items(request: Request, q: str = ""):
    q = q.strip()[:200]
    if not q:
        return {"items": storage.all_items(_uid(request)), "mode": "all"}
    found, mode = await run_in_threadpool(assistant.search_items, _uid(request), q)
    return {"items": found, "mode": mode}


@app.patch("/api/items/{item_id}")
def patch_item(item_id: int, body: ItemIn, request: Request):
    data = body.model_dump(exclude_unset=True)
    try:
        return assistant.update_item(_uid(request), item_id, data)
    except KeyError:
        raise HTTPException(404, "Запись не найдена.")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except storage.StorageError as e:
        raise HTTPException(500, str(e))


@app.delete("/api/items/{item_id}")
def delete_item(item_id: int, request: Request):
    try:
        assistant.delete_item(_uid(request), item_id)
    except KeyError:
        raise HTTPException(404, "Запись не найдена.")
    except storage.StorageError as e:
        raise HTTPException(500, str(e))
    return {"ok": True}


@app.get("/api/export.csv")
def export_csv(request: Request):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "type", "title", "date", "time", "status", "created_at"])
    for r in storage.all_items(_uid(request)):
        w.writerow([r["id"], r["kind"], r["title"], r["due_date"] or "", r["due_time"] or "", r["status"], r["created_at"]])
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=assistant_items.csv"})


if __name__ == "__main__":
    uvicorn.run(app, host=config.HOST, port=config.PORT)
