"""Запуск: python -m unittest discover -s tests -v   (нужен только Python, без сети и ключей)."""
import os
import sys
import tempfile
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from assistant import dates, nlu, search  # noqa: E402
from assistant.service import Assistant  # noqa: E402
from assistant.store import Store  # noqa: E402

NOW = datetime(2026, 10, 30, 15, 0)  # пятница


class Dates(unittest.TestCase):
    def p(self, t):
        return dates.parse(t, NOW)

    def test_relative(self):
        self.assertEqual(self.p("завтра в 10 утра").date, date(2026, 10, 31))
        self.assertEqual(self.p("завтра в 10 утра").time, "10:00")
        self.assertEqual(self.p("послезавтра").date, date(2026, 11, 1))
        self.assertEqual(self.p("в четверг в 11").date, date(2026, 11, 5))
        self.assertEqual(self.p("в четверг в 11").time, "11:00")
        self.assertEqual(self.p("в пятницу").date, date(2026, 11, 6))

    def test_in_hours(self):
        r = self.p("через два часа")
        self.assertEqual((r.date, r.time), (date(2026, 10, 30), "17:00"))
        self.assertEqual(self.p("через полчаса").time, "15:30")

    def test_spans(self):
        self.assertEqual(self.p("на следующей неделе").span, (date(2026, 11, 2), date(2026, 11, 8)))
        self.assertEqual(self.p("на эту неделю").span, (date(2026, 10, 26), date(2026, 11, 1)))
        r = self.p("до конца недели")
        self.assertTrue(r.deadline)
        self.assertEqual(r.span[1], date(2026, 11, 1))

    def test_time_forms(self):
        self.assertEqual(self.p("в 15:30").time, "15:30")
        self.assertEqual(self.p("в 5 вечера").time, "17:00")
        self.assertEqual(self.p("5 ноября").date, date(2026, 11, 5))


class Rules(unittest.TestCase):
    def r(self, t):
        return nlu.rule_parse(t, NOW)

    def test_create_task(self):
        c = self.r("Напомни мне завтра в 10 утра позвонить Ивану")
        self.assertEqual((c["intent"], c["type"], c["title"]), ("create", "reminder", "Позвонить Ивану"))
        c = self.r("В пятницу нужно отправить отчет")
        self.assertEqual((c["intent"], c["type"], c["title"]), ("create", "task", "Отправить отчет"))
        c = self.r("Поставь задачу купить билеты до конца недели")
        self.assertEqual((c["intent"], c["title"], str(c["range"][1])), ("create", "Купить билеты", "2026-11-01"))

    def test_create_meeting(self):
        c = self.r("Добавь встречу с командой завтра в 15:00")
        self.assertEqual((c["type"], c["title"], c["time"]), ("meeting", "Встреча с командой", "15:00"))
        c = self.r("В четверг в 11 у меня встреча с дизайнером")
        self.assertEqual((c["type"], c["title"]), ("meeting", "Встреча с дизайнером"))

    def test_list_variants(self):
        for t in ["Что у меня сегодня?", "Какие планы на сегодня?", "Покажи мои встречи на сегодня",
                  "Есть ли у меня встречи сегодня?", "Какие задачи у меня на завтра?", "Что запланировано на эту неделю?"]:
            self.assertEqual(self.r(t)["intent"], "list", t)

    def test_search_is_not_task(self):
        for t, q in [("Найди задачу про презентацию", "презентацию"), ("Когда у меня была встреча с Иваном?", "Иваном"),
                     ("Я когда-то ставил задачу по договору, найди ее", "договору")]:
            c = self.r(t)
            self.assertEqual((c["intent"], c["query"]), ("search", q), t)

    def test_create_beats_search_word(self):
        self.assertEqual(self.r("Поставь задачу найти договор")["intent"], "create")


class Search(unittest.TestCase):
    def test_fuzzy_and_synonyms(self):
        items = [{"id": 1, "type": "task", "title": "Подготовить презентацию для клиента"},
                 {"id": 2, "type": "task", "title": "Подписать контракт с поставщиком"},
                 {"id": 3, "type": "meeting", "title": "Встреча с Иваном"}]
        self.assertEqual(search.find(items, "презентации")[0][1]["id"], 1)
        self.assertEqual(search.find(items, "договор")[0][1]["id"], 2)  # синоним «контракт»
        self.assertEqual(search.find(items, "Иваном", "meeting")[0][1]["id"], 3)
        self.assertEqual(search.find(items, "самолёт"), [])


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = Assistant(Store(os.path.join(self.tmp, "t.db")), None)

    def say(self, t, user="u1"):
        return self.a.handle(user, t, NOW)["reply"]

    def test_full_cycle_and_persistence(self):
        self.assertIn("Готово", self.say("Напомни мне завтра в 10 утра позвонить Ивану"))
        self.assertIn("Готово", self.say("В пятницу нужно отправить отчет"))
        self.assertIn("Позвонить Ивану", self.say("Какие задачи у меня на завтра?"))
        self.assertIn("Отправить отчет", self.say("Найди задачу про отчёт"))
        # данные переживают пересоздание сервиса (новая сессия, тот же файл)
        b = Assistant(Store(os.path.join(self.tmp, "t.db")), None)
        self.assertIn("Позвонить Ивану", b.handle("u1", "Что у меня завтра?", NOW)["reply"])

    def test_clarification(self):
        self.assertEqual(self.say("Поставь встречу с Иваном завтра"), "На какое время поставить встречу?")
        self.assertIn("Готово", self.say("в 15"))
        self.assertIn("Встреча с Иваном", self.say("Что у меня завтра?"))

    def test_conflict_warning(self):
        self.say("Добавь встречу с командой завтра в 15:00")
        self.assertIn("уже есть встреча", self.say("Добавь встречу с клиентом завтра в 15:00"))

    def test_done_delete_move(self):
        self.say("Добавь задачу купить билеты")
        self.assertIn("выполненным", self.say("Отметь задачу про билеты выполненной"))
        self.say("Добавь встречу с дизайнером завтра в 11:00")
        self.assertIn("Перенёс", self.say("Перенеси встречу с дизайнером на пятницу"))
        self.assertIn("Удалил", self.say("Удали встречу с дизайнером"))

    def test_users_isolated(self):
        self.say("Добавь задачу секрет", user="u1")
        self.assertNotIn("секрет", self.say("Найди задачу секрет", user="u2").lower())


class FakeLLM:
    """Имитация GigaChat: проверяем слияние «смысл от модели, даты от правил» и откат при сбое."""
    enabled = True

    def __init__(self, reply=None, fail=False):
        self.reply, self.fail = reply, fail

    def available(self):
        return True

    def chat(self, messages, **kw):
        if self.fail:
            raise RuntimeError("down")
        return self.reply


class WithLLM(unittest.TestCase):
    def test_llm_intent_rules_dates(self):
        j = '{"intent":"create","type":"meeting","title":"Встреча с Иваном","date":"2030-01-01","time":null}'
        a = Assistant(Store(os.path.join(tempfile.mkdtemp(), "t.db")), FakeLLM(j))
        r = a.handle("u1", "Поставь встречу с Иваном завтра в 12:00", NOW)
        self.assertEqual(r["source"], "llm")
        self.assertIn("завтра, 31 октября в 12:00", r["reply"])  # дата из правил, а не выдуманная моделью

    def test_fallback_on_failure(self):
        a = Assistant(Store(os.path.join(tempfile.mkdtemp(), "t.db")), FakeLLM(fail=True))
        r = a.handle("u1", "Напомни завтра в 10 утра позвонить Ивану", NOW)
        self.assertEqual((r["source"], r["intent"]), ("rules", "create"))

    def test_garbage_json_fallback(self):
        a = Assistant(Store(os.path.join(tempfile.mkdtemp(), "t.db")), FakeLLM("не json"))
        self.assertEqual(a.handle("u1", "Что у меня сегодня?", NOW)["source"], "rules")

    def test_new_command_beats_pending(self):
        a = Assistant(Store(os.path.join(tempfile.mkdtemp(), "t.db")), None)
        a.handle("u1", "Поставь встречу", NOW)
        self.assertEqual(a.handle("u1", "Что на следующей неделе?", NOW)["intent"], "list")


if __name__ == "__main__":
    unittest.main()
