"""GigaChat: токен доступа живёт 30 минут и обновляется автоматически. Любая ошибка → запасной режим на правилах."""
import base64
import json
import logging
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta

from . import dates

log = logging.getLogger("llm")
INTENTS = {"create", "list", "search", "complete", "delete", "move", "unknown"}
TYPES = {"task", "meeting", "reminder"}


class GigaChat:
    OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    API = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self, auth_key=None, client_id=None, client_secret=None, scope="GIGACHAT_API_PERS",
                 model="GigaChat", timeout=10, verify_ssl=False, ca_bundle=None):
        if not auth_key and client_id and client_secret:
            auth_key = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        self.auth_key, self.scope, self.model, self.timeout = auth_key, scope, model, timeout
        ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
        if not verify_ssl and not ca_bundle:  # сертификаты Минцифры отсутствуют в стандартном хранилище
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        self.ctx = ctx
        self._tok, self._exp = None, 0.0
        self._lock = threading.Lock()
        self._down_until = 0.0

    @property
    def enabled(self):
        return bool(self.auth_key)

    def available(self):
        return self.enabled and time.time() >= self._down_until

    def _token(self, force=False):
        with self._lock:
            if not force and self._tok and time.time() < self._exp - 60:
                return self._tok
            req = urllib.request.Request(
                self.OAUTH, data=urllib.parse.urlencode({"scope": self.scope}).encode(), method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                         "RqUID": str(uuid.uuid4()), "Authorization": f"Basic {self.auth_key}"})
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                data = json.loads(r.read().decode())
            self._tok = data["access_token"]
            self._exp = data["expires_at"] / 1000 if data.get("expires_at", 0) > 1e11 else time.time() + 1500
            return self._tok

    def chat(self, messages, temperature=0.1, max_tokens=350) -> str:
        body = json.dumps({"model": self.model, "messages": messages, "temperature": temperature,
                           "max_tokens": max_tokens}).encode()
        for attempt in (0, 1):
            try:
                req = urllib.request.Request(
                    self.API, data=body, method="POST",
                    headers={"Content-Type": "application/json", "Accept": "application/json",
                             "Authorization": f"Bearer {self._token(force=attempt == 1)}"})
                with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as r:
                    return json.loads(r.read().decode())["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    continue
                self._down_until = time.time() + 30
                raise
            except Exception:
                self._down_until = time.time() + 30
                raise


def _json(s: str):
    m = re.search(r"\{.*\}", s, re.S)
    return json.loads(m.group(0)) if m else None


def _d(v):
    try:
        return date.fromisoformat(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
    except ValueError:
        return None


def _t(v):
    return v if isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v) else None


def _calendar(now):
    return "; ".join(f"{dates.WD_NAMES[(now + timedelta(days=i)).weekday()]} {(now + timedelta(days=i)).date()}"
                     for i in range(9))


SYSTEM = """Ты — разборщик команд голосового ассистента задач и встреч. Сегодня {wd}, {today}, сейчас {hm}.
Ближайшие дни: {cal}.
Верни ТОЛЬКО один JSON-объект без пояснений и без markdown:
{{"intent":"create|list|search|complete|delete|move|unknown","type":"task|meeting|reminder|null","title":"...","date":"YYYY-MM-DD|null","time":"HH:MM|null","range_start":"YYYY-MM-DD|null","range_end":"YYYY-MM-DD|null","query":"...","filter_type":"task|meeting|reminder|null"}}
Правила:
- create: добавить задачу/напоминание/встречу. type: meeting — встреча, созвон, совещание; reminder — если просят «напомни»; иначе task. title — суть без дат и слов-команд, с заглавной буквы («Позвонить Ивану», «Встреча с командой»). date/time — только если названы в запросе, иначе null (не выдумывай).
- list: пользователь спрашивает, что запланировано (сегодня, завтра, неделя). Заполни range_start/range_end; filter_type если спрашивают только про встречи или только про задачи.
- search: найти старую запись по смыслу. query — ключевые слова искомой записи.
- complete: отметить выполненным; delete: удалить; move: перенести (date/time — новые). query — ключевые слова записи.
- Если непонятно — intent "unknown".
Примеры:
«Напомни мне завтра в 10 утра позвонить Ивану» → {{"intent":"create","type":"reminder","title":"Позвонить Ивану","date":"<завтра>","time":"10:00"}}
«Какие планы на сегодня?» → {{"intent":"list","range_start":"<сегодня>","range_end":"<сегодня>"}}
«Я когда-то ставил задачу по договору, найди её» → {{"intent":"search","query":"договор","filter_type":"task"}}"""


def parse_command(llm: GigaChat, text: str, now: datetime):
    """Возвращает команду в том же формате, что и nlu.rule_parse, или None при любой проблеме."""
    try:
        sys_msg = SYSTEM.format(wd=dates.WD_NAMES[now.weekday()], today=now.date(), hm=now.strftime("%H:%M"),
                                cal=_calendar(now))
        raw = llm.chat([{"role": "system", "content": sys_msg}, {"role": "user", "content": text}])
        j = _json(raw)
        if not isinstance(j, dict) or j.get("intent") not in INTENTS:
            return None
        title = j.get("title") if isinstance(j.get("title"), str) else None
        rs, re_ = _d(j.get("range_start")), _d(j.get("range_end"))
        return {"intent": j["intent"], "type": j.get("type") if j.get("type") in TYPES else None,
                "title": (title or "").strip()[:120] or None, "date": _d(j.get("date")), "time": _t(j.get("time")),
                "range": (rs, re_ or rs) if rs else None, "deadline": False,
                "query": (j.get("query") or "").strip() if isinstance(j.get("query"), str) else None,
                "filter_type": j.get("filter_type") if j.get("filter_type") in TYPES else None, "source": "llm"}
    except Exception as e:  # сеть, таймаут, формат — не важно, есть запасной режим
        log.warning("GigaChat parse failed: %s", e)
        return None


def semantic_pick(llm: GigaChat, query: str, items):
    """Семантический поиск: просим модель выбрать подходящие записи по смыслу. Возвращает список id."""
    try:
        catalog = "\n".join(f"{i['id']}: {i['title']}" for i in items[:80])
        raw = llm.chat([
            {"role": "system", "content": "Ты помогаешь искать записи. Из списка «id: название» выбери те, что по смыслу "
             "подходят под запрос. Верни ТОЛЬКО JSON-массив id, например [3,7]. Если подходящих нет — []."},
            {"role": "user", "content": f"Запрос: {query}\nЗаписи:\n{catalog}"}], max_tokens=60)
        m = re.search(r"\[.*?\]", raw, re.S)
        ids = json.loads(m.group(0)) if m else []
        valid = {i["id"] for i in items}
        return [int(x) for x in ids if isinstance(x, (int, str)) and str(x).isdigit() and int(x) in valid][:5]
    except Exception as e:
        log.warning("GigaChat semantic search failed: %s", e)
        return []
