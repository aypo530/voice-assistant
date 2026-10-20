"""Бизнес-логика, хранилище, поиск, переключение облако → локально. Без сети."""
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import helpers  # noqa: F401
import config
import llm
import search
import storage
import stt_local
from assistant import Assistant
from command import Command


def tomorrow():
    return (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=1)).strftime("%Y-%m-%d")


class Base(unittest.TestCase):
    def setUp(self):
        if config.DATA_FILE.exists():
            config.DATA_FILE.unlink()
        storage.init()
        config.MULTI_USER = False
        self.a = Assistant()

    def say(self, text, sid="s1"):
        return self.a.handle(sid, "Europe/Moscow", text=text)


class LocalFlow(Base):
    def test_create_list_search_complete(self):
        r = self.say("Добавь встречу с командой завтра в 15:00")
        self.assertIn("Записано", r["reply"]); self.assertEqual(r["engine"], "local"); self.assertIn("notice", r)
        self.say("Напомни завтра в 10 утра позвонить Ивану")
        self.say("Поставь задачу подписать договор до конца недели")
        r = self.say("Какие встречи у меня на завтра?")
        self.assertIn("Встреча с командой", r["reply"]); self.assertNotIn("Ивану", r["reply"])
        r = self.say("Найди задачу про договор")
        self.assertIn("Подписать договор", r["reply"])
        r = self.say("Когда у меня была встреча с командой?")
        self.assertIn("Встреча с командой", r["reply"])
        r = self.say("Отметь задачу про договор выполненной")
        self.assertIn("Отметил выполненной", r["reply"])
        self.assertEqual([x["status"] for x in storage.all_items("s1") if "договор" in x["title"].lower()], ["done"])
        self.assertIn("выполнено", self.say("Найди задачу про договор")["reply"])

    def test_clarification_dialog(self):
        r = self.say("Поставь встречу с Иваном завтра")
        self.assertEqual(r["intent"], "clarify"); self.assertIn("время", r["reply"])
        self.assertEqual(storage.all_items("s1"), [])
        r = self.say("в три")
        self.assertEqual(r["intent"], "create"); self.assertIn("Встреча с Иваном", r["reply"])
        it = storage.all_items("s1")[0]
        self.assertEqual((it["due_date"], it["due_time"]), (tomorrow(), "15:00"))

    def test_clarification_cancel(self):
        self.say("Поставь встречу с Иваном завтра")
        self.say("нет, отмена")
        self.assertEqual(storage.all_items("s1"), [])

    def test_semantic_search_without_word_match(self):
        self.say("Подписать контракт с Петровым")
        r = self.say("Найди задачу про договор")
        self.assertIn("Точных совпадений нет", r["reply"]); self.assertIn("контракт", r["reply"].lower())

    def test_duplicate_and_past_and_conflict(self):
        self.say("Задача купить билеты")
        r = self.say("Задача купить билеты")
        self.assertIn("уже есть", r["reply"]); self.assertEqual(len(storage.all_items("s1")), 1)
        self.say("Встреча с Олегом завтра в 12:00")
        r = self.say("Встреча с Анной завтра в 12:00")
        self.assertIn("Внимание", r["reply"]); self.assertEqual(len(storage.all_items("s1")), 3)

    def test_audio_without_local_stt(self):
        def boom(*a, **k): raise stt_local.STTUnavailable("модель не загружена")
        orig, stt_local.transcribe = stt_local.transcribe, boom
        try:
            r = self.a.handle("s1", None, audio=b"0" * 3000)
        finally:
            stt_local.transcribe = orig
        self.assertFalse(r["ok"]); self.assertIn("текстом", r["reply"])

    def test_local_audio_path(self):
        orig, stt_local.transcribe = stt_local.transcribe, lambda a, m="": "что у меня сегодня"
        try:
            r = self.a.handle("s1", None, audio=b"0" * 3000)
        finally:
            stt_local.transcribe = orig
        self.assertEqual((r["intent"], r["transcript"]), ("list", "что у меня сегодня"))


class CloudFallback(Base):
    def setUp(self):
        super().setUp()
        self._orig = (config.GEMINI_API_KEY, llm.parse, llm.usable)
        config.GEMINI_API_KEY = "test-key"
        llm.usable = lambda: True

    def tearDown(self):
        config.GEMINI_API_KEY, llm.parse, llm.usable = self._orig

    def test_cloud_used_when_available(self):
        llm.parse = lambda now, h, text=None, audio=None, mime="": Command(intent="create", kind="task", title="Облачная задача", transcript=text)
        r = self.say("что угодно")
        self.assertEqual(r["engine"], "cloud"); self.assertIn("Облачная задача", r["reply"]); self.assertNotIn("notice", r)

    def test_automatic_fallback_to_local(self):
        def down(*a, **k): raise llm.LLMNetworkError("нет сети")
        llm.parse = down
        r = self.say("Напомни завтра в 10 утра позвонить Ивану")
        self.assertEqual(r["engine"], "local"); self.assertIn("Записано", r["reply"]); self.assertTrue(r["ok"])

    def test_region_block_is_also_fallback(self):
        def blocked(*a, **k): raise llm.LLMRegionError("User location is not supported")
        llm.parse = blocked
        self.assertEqual(self.say("что у меня сегодня")["engine"], "local")


class Storage(Base):
    def test_update_delete_persist(self):
        self.say("Добавь встречу с командой завтра в 15:00")
        item = storage.all_items("s1")[0]
        upd = self.a.update_item("s1", item["id"], {"title": "Встреча с клиентом", "time": "16:30", "kind": "meeting"})
        self.assertEqual((upd["title"], upd["due_time"]), ("Встреча с клиентом", "16:30"))
        self.a = Assistant()                                   # «перезапуск»
        self.assertEqual(storage.all_items("s1")[0]["title"], "Встреча с клиентом")
        self.a.delete_item("s1", item["id"])
        self.assertEqual(storage.all_items("s1"), [])

    def test_update_validation(self):
        self.say("Задача купить билеты")
        iid = storage.all_items("s1")[0]["id"]
        for bad in ({"title": "  "}, {"kind": "zzz"}, {"date": "32.13"}, {"time": "25:00"}, {"kind": "meeting"}):
            with self.assertRaises(ValueError):
                self.a.update_item("s1", iid, bad)
        with self.assertRaises(KeyError):
            self.a.update_item("s1", 999, {"title": "x"})
        with self.assertRaises(KeyError):
            self.a.delete_item("s1", 999)
        self.assertEqual(self.a.update_item("s1", iid, {"status": "done"})["status"], "done")

    def test_csv_format_and_legacy_rows(self):
        self.say("Задача подписать договор")
        head = config.DATA_FILE.read_text(encoding="utf-8-sig").splitlines()[0]
        self.assertEqual(head, "id;type;title;date;time;status;created_at;user_id")
        with open(config.DATA_FILE, "w", encoding="utf-8-sig") as f:                  # старый формат без user_id
            f.write("id;type;title;date;time;status;created_at\n1;task;Старая задача;;;open;2026-01-01\n")
        self.assertEqual(storage.all_items("s1")[0]["title"], "Старая задача")

    def test_multi_user_isolation(self):
        config.MULTI_USER = True
        try:
            self.say("Задача для первого", "u1"); self.say("Задача для второго", "u2")
            self.assertEqual([r["title"] for r in storage.all_items("u1")], ["Задача для первого"])
            iid = storage.all_items("u2")[0]["id"]
            with self.assertRaises(KeyError):
                self.a.delete_item("u1", iid)                  # чужую запись удалить нельзя
        finally:
            config.MULTI_USER = False


class Search(unittest.TestCase):
    ITEMS = [{"id": 1, "title": "Подписать контракт с Петровым"}, {"id": 2, "title": "Купить билеты в Казань"},
             {"id": 3, "title": "Подготовить слайды для доклада"}, {"id": 4, "title": "Записаться к стоматологу"}]

    def test_word_forms(self):
        found, mode = search.find("Ивану", [{"id": 1, "title": "Позвонить Ивану"}, {"id": 2, "title": "Купить хлеб"}])
        self.assertEqual((mode, [r["id"] for r in found]), ("exact", [1]))

    def test_meaning(self):
        for q, want in [("договор", 1), ("презентация", 3), ("авиабилеты", 2), ("поликлиника", 4)]:
            found, mode = search.find(q, self.ITEMS)
            self.assertEqual((mode, found[0]["id"]), ("semantic", want), q)

    def test_nothing_found(self):
        self.assertEqual(search.find("космос", self.ITEMS), ([], "none"))


if __name__ == "__main__":
    unittest.main()


class PendingMemory(Base):
    """Сценарии со скриншота: ответ на уточняющий вопрос не должен превращаться в задачу."""

    def _only(self):
        return storage.all_items("s1")

    def test_bare_time_answers(self):
        for answer in ("Восемь утра.", "На 8 утра.", "Восьмутра", "8 утра", "в 8 утра"):
            if config.DATA_FILE.exists():
                config.DATA_FILE.unlink()
            storage.init()
            self.a = Assistant()
            r = self.say("Сегодня 9 вечера встреча с Ульяной")
            self.assertEqual(r["intent"], "create", r)
            self.say("Поставь встречу с Ульяной завтра")
            r = self.say(answer)
            self.assertEqual(r["intent"], "create", (answer, r))
            its = [i for i in self._only() if i["kind"] == "meeting" and i["due_date"] == tomorrow()]
            self.assertEqual([i["due_time"] for i in its], ["08:00"], answer)
            self.assertFalse([i for i in self._only() if i["kind"] == "task"], answer)

    def test_time_without_preposition_in_first_phrase(self):
        r = self.say("Завтра 8 утра встреча с Ульяной")
        self.assertEqual(r["intent"], "create")
        it = self._only()[0]
        self.assertEqual((it["kind"], it["due_time"], it["title"]), ("meeting", "08:00", "Встреча с Ульяной"))

    def test_bare_number_answer(self):
        self.say("Напомни завтра позвонить Ивану")
        r = self.say("десять")
        self.assertEqual(r["intent"], "create"); self.assertEqual(self._only()[0]["due_time"], "10:00")

    def test_unclear_answer_reasks_not_task(self):
        r = self.say("Поставь встречу с Ульяной завтра")
        self.assertEqual(r["intent"], "clarify")
        r = self.say("эээ ну это")
        self.assertEqual(r["intent"], "clarify"); self.assertEqual(self._only(), [])
        r = self.say("в три")
        self.assertEqual(r["intent"], "create"); self.assertEqual(self._only()[0]["due_time"], "15:00")

    def test_new_command_overrides_pending(self):
        self.say("Поставь встречу с Ульяной завтра")
        r = self.say("Напомни завтра в 10 утра позвонить Ивану")
        self.assertEqual(r["intent"], "create")
        self.assertEqual([i["kind"] for i in self._only()], ["reminder"])



class VoiceEdit(Base):
    def test_delete_by_name_and_never_creates_task(self):
        self.say("Поставь задачу подписать договор до конца недели")
        r = self.say("Удали задачу про договор")
        self.assertEqual(r["intent"], "delete"); self.assertIn("Удалил", r["reply"])
        self.assertEqual(storage.all_items("s1"), [])
        r = self.say("Удали последнюю заметку.")
        self.assertEqual(storage.all_items("s1"), []); self.assertNotIn("Записано", r["reply"])

    def test_delete_last(self):
        self.say("Поставь задачу купить хлеб"); self.say("Поставь задачу позвонить маме")
        self.say("Удали последнюю запись")
        self.assertEqual([i["title"] for i in storage.all_items("s1")], ["Купить хлеб"])

    def test_move_meeting_by_name(self):
        self.say("Добавь встречу с Иваном завтра в 10:00")
        r = self.say("Перенеси встречу с Иваном на послезавтра в 15:00")
        self.assertEqual(r["intent"], "update"); self.assertIn("Перенёс", r["reply"])
        it = storage.all_items("s1")[0]
        d2 = (datetime.now(ZoneInfo("Europe/Moscow")) + timedelta(days=2)).strftime("%Y-%m-%d")
        self.assertEqual((it["due_date"], it["due_time"]), (d2, "15:00"))

    def test_context_move_it(self):
        self.say("Добавь встречу с Иваном завтра в 10:00")
        r = self.say("А перенеси её на послезавтра")
        self.assertEqual(r["intent"], "update")
        it = storage.all_items("s1")[0]
        self.assertEqual((it["due_date"] > tomorrow(), it["due_time"]), (True, "10:00"))

    def test_move_asks_then_applies(self):
        self.say("Добавь встречу с Иваном завтра в 10:00")
        r = self.say("Перенеси встречу с Иваном")
        self.assertEqual(r["intent"], "clarify"); self.assertIn("перенести", r["reply"])
        r = self.say("на 16:30")
        self.assertEqual(r["intent"], "update")
        self.assertEqual(storage.all_items("s1")[0]["due_time"], "16:30")

    def test_complete_it(self):
        self.say("Поставь задачу подписать договор")
        self.assertIn("Отметил", self.say("Отметь её выполненной")["reply"])

    def test_ambiguous_not_deleted(self):
        self.say("Поставь задачу купить хлеб"); self.say("Поставь задачу купить молоко")
        r = self.say("Удали задачу купить")
        self.assertEqual(len(storage.all_items("s1")), 2); self.assertIn("несколько", r["reply"])

    def test_no_semantic_delete(self):
        self.say("Поставь задачу подписать контракт")
        self.say("Удали задачу про договор")
        self.assertEqual(len(storage.all_items("s1")), 1)

    def test_cloud_update_intent(self):
        orig = (config.GEMINI_API_KEY, llm.parse, llm.usable)
        config.GEMINI_API_KEY = "k"; llm.usable = lambda: True
        try:
            llm.parse = lambda now, h, text=None, audio=None, mime="": Command(intent="create", kind="meeting", title="Встреча с Иваном", date=tomorrow(), time="10:00", transcript=text)
            self.say("встреча")
            llm.parse = lambda now, h, text=None, audio=None, mime="": Command(intent="update", ref="it", time="12:00", transcript=text)
            self.assertIn("Перенёс", self.say("а сдвинь на двенадцать")["reply"])
            self.assertEqual(storage.all_items("s1")[0]["due_time"], "12:00")
        finally:
            config.GEMINI_API_KEY, llm.parse, llm.usable = orig


class UserKey(Base):
    def test_user_key_has_priority_and_isolated_state(self):
        tok = llm.set_request_key("user-key")
        try:
            self.assertEqual(llm.current_key(), "user-key"); self.assertTrue(llm.usable())
            llm._mark_down("auth")
            self.assertFalse(llm.usable())
        finally:
            llm._req_key.reset(tok)
        self.assertEqual(llm.current_key(), config.GEMINI_API_KEY)
        self.assertFalse(llm.has_key())          # у сервера ключа нет, чужое состояние не протекло
        self.assertEqual(llm.status()["state"], "no_key")
