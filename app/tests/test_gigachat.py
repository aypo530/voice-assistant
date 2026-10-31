"""Транспорт GigaChat: токен и его обновление, chat, аудио, эмбеддинги. Без сети (HTTP подменяется)."""
import json
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import helpers  # noqa: F401
import config
import gigachat as gc
import llm
from command import Command

CRED = "dGVzdDp0ZXN0"          # base64("test:test")


class FakeResp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text or json.dumps(body or {})
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    """Очередь ответов вместо сети; параметры каждого запроса сохраняются."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.responses:
            raise AssertionError(f"Неожиданный запрос {method} {url}")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item() if callable(item) else item

    def token_requests(self):
        return [c for c in self.calls if c["url"].endswith("/oauth")]

    def chat_requests(self):
        return [c for c in self.calls if c["url"].endswith("/chat/completions")]


def token_body(token="t1", ttl=1800, ms=False):
    exp = time.time() + ttl
    return {"access_token": token, "expires_at": exp * 1000 if ms else exp}


class Base(unittest.TestCase):
    def setUp(self):
        self._orig_request = gc._request
        self._orig_chat = gc.chat
        self._orig_cred = config.GIGACHAT_CREDENTIALS
        config.GIGACHAT_CREDENTIALS = CRED
        config.TOKEN_SKEW_S = 60
        gc._tokens.clear()
        gc._scope_ok.clear()
        llm._states.clear()
        llm._model.clear()
        llm._no_schema.clear()
        llm._req_mode.set("auto")
        self.addCleanup(self._restore)

    def _restore(self):
        gc._request = self._orig_request
        gc.chat = self._orig_chat
        config.GIGACHAT_CREDENTIALS = self._orig_cred
        gc._tokens.clear()
        gc._scope_ok.clear()
        llm._states.clear()
        llm._model.clear()
        llm._no_schema.clear()
        llm._req_key.set("")

    def http(self, *responses):
        fake = FakeHTTP(*responses)
        gc._request = fake
        return fake


class TokenTests(Base):
    def test_token_is_fetched_once_and_cached(self):
        fake = self.http(FakeResp(200, token_body("abc")))
        self.assertEqual(gc.token(CRED), "abc")
        self.assertEqual(gc.token(CRED), "abc")
        self.assertEqual(len(fake.token_requests()), 1)
        self.assertGreater(gc.token_ttl(CRED), 1700)

    def test_expires_at_in_milliseconds(self):
        self.http(FakeResp(200, token_body("ms", ms=True)))
        gc.token(CRED)
        self.assertGreater(gc.token_ttl(CRED), 1700)

    def test_token_refreshed_before_expiry(self):
        fake = self.http(FakeResp(200, token_body("old", ttl=30)), FakeResp(200, token_body("new")))
        self.assertEqual(gc.token(CRED), "old")
        self.assertEqual(gc.token(CRED), "new")          # 30 < skew 60 — токен уже «протух»
        self.assertEqual(len(fake.token_requests()), 2)

    def test_missing_expires_at_uses_30_minutes(self):
        self.http(FakeResp(200, {"access_token": "noexp"}))
        gc.token(CRED)
        self.assertGreater(gc.token_ttl(CRED), 1700)

    def test_scope_is_detected_after_rejection(self):
        fake = self.http(FakeResp(401, {"message": "scope mismatch"}), FakeResp(200, token_body("ok")))
        self.assertEqual(gc.token(CRED), "ok")
        scopes = [c["data"]["scope"] for c in fake.token_requests()]
        self.assertEqual(scopes[0], config.GIGACHAT_SCOPE)
        self.assertNotEqual(scopes[1], config.GIGACHAT_SCOPE)
        self.assertEqual(scopes[1], gc._scopes(CRED)[0])   # рабочий scope запомнился

    def test_rejected_key_raises_auth_error(self):
        self.http(*[FakeResp(401, {"message": "bad key"}) for _ in config.GIGACHAT_SCOPES])
        with self.assertRaises(gc.AuthError):
            gc.token(CRED)

    def test_network_error_is_mapped(self):
        self.http(gc.NetworkError("нет сети"))
        with self.assertRaises(gc.NetworkError):
            gc.token(CRED)


class ChatTests(Base):
    def test_401_refreshes_token_and_repeats_request(self):
        fake = self.http(
            FakeResp(200, token_body("old")),
            FakeResp(401, {"message": "token expired"}),
            FakeResp(200, token_body("new")),
            FakeResp(200, {"choices": [{"message": {"content": "привет"}, "finish_reason": "stop"}]}),
        )
        self.assertEqual(gc.chat(CRED, config.GIGACHAT_MODEL, [{"role": "user", "content": "hi"}]), "привет")
        self.assertEqual(len(fake.token_requests()), 2)
        self.assertEqual(len(fake.chat_requests()), 2)

    def test_request_contains_schema_and_attachments(self):
        fake = self.http(
            FakeResp(200, token_body()),
            FakeResp(200, {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}),
        )
        gc.chat(CRED, "GigaChat-2-Max", [{"role": "user", "content": "hi"}],
                json_schema={"type": "object"}, attachments=["f-1"], max_tokens=100)
        body = fake.chat_requests()[0]["json"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["strict"])
        self.assertEqual(body["messages"][-1]["attachments"], ["f-1"])
        self.assertEqual(body["function_call"], "auto")
        self.assertEqual(body["max_tokens"], 100)

    def test_blacklist_answer_is_bad_request(self):
        self.http(
            FakeResp(200, token_body()),
            FakeResp(200, {"choices": [{"message": {"content": ""}, "finish_reason": "blacklist"}]}),
        )
        with self.assertRaises(gc.BadRequest):
            gc.chat(CRED, config.GIGACHAT_MODEL, [{"role": "user", "content": "x"}])

    def test_402_reports_quota(self):
        self.http(FakeResp(200, token_body()), FakeResp(402, {"message": "no tokens left"}))
        with self.assertRaises(gc.QuotaError):
            gc.chat(CRED, config.GIGACHAT_MODEL, [{"role": "user", "content": "x"}])

    def test_429_is_busy_error(self):
        self.http(FakeResp(200, token_body()), FakeResp(429, {"message": "too many requests"}))
        with self.assertRaises(gc.BusyError):
            gc.chat(CRED, config.GIGACHAT_MODEL, [{"role": "user", "content": "x"}])


class FilesTests(Base):
    def test_upload_and_delete(self):
        fake = self.http(
            FakeResp(200, token_body()),
            FakeResp(200, {"id": "file-1"}),
            FakeResp(200, {}),
        )
        self.assertEqual(gc.upload(CRED, b"0" * 100, "voice.wav", "audio/wav"), "file-1")
        gc.delete_file(CRED, "file-1")
        self.assertTrue(fake.calls[1]["url"].endswith("/files"))
        self.assertTrue(fake.calls[2]["url"].endswith("/files/file-1/delete"))


class EmbedTests(Base):
    def test_vectors_sorted_by_index(self):
        self.http(
            FakeResp(200, token_body()),
            FakeResp(200, {"data": [{"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}),
        )
        self.assertEqual(gc.embed(CRED, "Embeddings", ["a", "b"]), [[1.0, 0.0], [0.0, 1.0]])

    def test_embeddings_are_batched(self):
        old = config.EMBED_BATCH
        config.EMBED_BATCH = 2
        try:
            fake = self.http(
                FakeResp(200, token_body()),
                FakeResp(200, {"data": [{"index": 0, "embedding": [1.0]}, {"index": 1, "embedding": [1.0]}]}),
                FakeResp(200, {"data": [{"index": 0, "embedding": [1.0]}]}),
            )
            gc.embed(CRED, "Embeddings", ["a", "b", "c"])
            self.assertEqual(len([c for c in fake.calls if c["url"].endswith("/embeddings")]), 2)
        finally:
            config.EMBED_BATCH = old


class CredentialTests(unittest.TestCase):
    def test_pair_is_encoded(self):
        self.assertEqual(gc.normalize("client:secret"), "Y2xpZW50OnNlY3JldA==")

    def test_prefix_and_padding(self):
        self.assertEqual(gc.normalize("GIGACHAT_CREDENTIALS=YWJjOmRlZg=="), "YWJjOmRlZg==")
        self.assertEqual(gc.normalize("YWJjOmRlZg"), "YWJjOmRlZg==")

    def test_garbage_is_rejected(self):
        for bad in ("", "не ключ", "YWJj"):       # без client_id:secret внутри
            with self.assertRaises(gc.ConfigError):
                gc.normalize(bad)


class ParseTests(Base):
    TARGET = {"intent": "create", "title": "Позвонить Ивану", "kind": "reminder",
              "date": "2026-11-01", "time": "10:00", "transcript": "напомни завтра в десять позвонить Ивану",
              "range_start": "", "range_end": "", "query": "", "ref": "", "repeat": "", "reply": ""}

    def _fake_chat(self, answer, calls):
        def fake(cred, model, messages, **kw):
            calls.append({"model": model, "messages": messages, **kw})
            return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)
        gc.chat = fake

    def test_parse_requests_json_schema(self):
        calls = []
        self._fake_chat(self.TARGET, calls)
        cmd = llm.parse(datetime.now(ZoneInfo("Europe/Moscow")), [], text="напомни завтра в 10 позвонить Ивану")
        self.assertIsInstance(cmd, Command)
        self.assertEqual((cmd.intent, cmd.title, cmd.time), ("create", "Позвонить Ивану", "10:00"))
        self.assertEqual(calls[0]["json_schema"], llm.COMMAND_SCHEMA)
        self.assertIn("модуль понимания речи", calls[0]["messages"][0]["content"])

    def test_schema_rejection_falls_back_to_plain_json(self):
        calls = []
        state = {"n": 0}

        def fake(cred, model, messages, **kw):
            calls.append(kw)
            state["n"] += 1
            if state["n"] == 1:
                raise gc.BadRequest("response_format is not supported", 400)
            return json.dumps(self.TARGET, ensure_ascii=False)

        gc.chat = fake
        cmd = llm.parse(datetime.now(ZoneInfo("Europe/Moscow")), [], text="напомни")
        self.assertEqual(cmd.title, "Позвонить Ивану")
        self.assertIsNone(calls[1]["json_schema"])
        self.assertIn(config.GIGACHAT_MODEL, llm._no_schema)

    def test_audio_upload_and_cleanup(self):
        uploaded, deleted, calls = [], [], []
        self._fake_chat(self.TARGET, calls)
        gc.upload = lambda cred, data, name, mime: uploaded.append((name, mime)) or "file-9"
        gc.delete_file = lambda cred, fid: deleted.append(fid)
        cmd = llm.parse(datetime.now(ZoneInfo("Europe/Moscow")), [], audio=b"0" * 4000, mime="audio/wav")
        self.assertEqual(cmd.transcript, "напомни завтра в десять позвонить Ивану")
        self.assertEqual(uploaded[0][1], "audio/wav")
        self.assertEqual(calls[0]["attachments"], ["file-9"])
        self.assertEqual(deleted, ["file-9"])

    def test_audio_moves_to_next_model_when_one_rejects_files(self):
        calls = []

        def fake(cred, model, messages, **kw):
            calls.append(model)
            if len(calls) == 1:
                raise gc.BadRequest("model does not support audio attachments", 400)
            return json.dumps({"intent": "list", "range_start": "2026-11-01", "range_end": "2026-11-01",
                               "transcript": "что у меня завтра"}, ensure_ascii=False)

        gc.chat = fake
        gc.upload = lambda cred, data, name, mime: "file-3"
        gc.delete_file = lambda cred, fid: None
        cmd = llm.parse(datetime.now(ZoneInfo("Europe/Moscow")), [], audio=b"0" * 4000)
        self.assertEqual(cmd.intent, "list")
        self.assertEqual(calls[0], config.GIGACHAT_MODEL)
        self.assertNotEqual(calls[1], config.GIGACHAT_MODEL)      # сработала другая модель

    def test_audio_falls_back_to_transcription(self):
        calls = []

        def fake(cred, model, messages, **kw):
            calls.append({"messages": messages, **kw})
            if kw.get("attachments") and kw.get("json_schema"):
                raise gc.BadRequest("model cannot combine audio and json schema", 400)
            if kw.get("attachments"):
                return "купить хлеб"                   # отдельная расшифровка (без схемы) работает
            return json.dumps({"intent": "create", "title": "Купить хлеб", "kind": "task",
                               "transcript": "купить хлеб"}, ensure_ascii=False)

        gc.chat = fake
        gc.upload = lambda cred, data, name, mime: "file-7"
        gc.delete_file = lambda cred, fid: None
        cmd = llm.parse(datetime.now(ZoneInfo("Europe/Moscow")), [], audio=b"0" * 4000)
        self.assertEqual(cmd.title, "Купить хлеб")
        self.assertEqual(cmd.transcript, "купить хлеб")
        self.assertIsNone(calls[-1]["attachments"])    # текст разбирается уже без файла
        self.assertTrue(any("расшифруй" in c["messages"][-1]["content"].lower() for c in calls))


class StatusTests(Base):
    def test_no_key(self):
        config.GIGACHAT_CREDENTIALS = ""
        self.assertFalse(llm.has_key())
        self.assertEqual(llm.status()["state"], "no_key")
        self.assertFalse(llm.usable())

    def test_offline_mode(self):
        llm._req_mode.set("offline")
        self.assertEqual(llm.status()["state"], "offline")
        self.assertFalse(llm.usable())

    def test_cooldown_blocks_cloud_then_expires(self):
        llm._mark_down("busy")
        self.assertFalse(llm.usable())
        self.assertEqual(llm.status()["reason"], "busy")
        llm._states[llm._kid()]["until"] = time.time() - 1
        self.assertTrue(llm.usable())


if __name__ == "__main__":
    unittest.main()
