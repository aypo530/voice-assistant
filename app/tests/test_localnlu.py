"""Локальный разборщик: все фразы из ТЗ + относительные даты. now = среда, 21.10.2026 14:30."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import helpers  # noqa: F401
import localnlu

NOW = datetime(2026, 10, 21, 14, 30, tzinfo=ZoneInfo("Europe/Moscow"))


def p(text, pending=None):
    return localnlu.parse(text, NOW, pending)


class Create(unittest.TestCase):
    def check(self, text, kind, title, date, time):
        c = p(text)
        self.assertEqual((c.intent, c.kind, c.title, c.date, c.time), ("create", kind, title, date, time), text)

    def test_reminder(self):
        self.check("Напомни мне завтра в 10 утра позвонить Ивану", "reminder", "Позвонить Ивану", "2026-10-22", "10:00")

    def test_task_friday(self):
        self.check("В пятницу нужно отправить отчет", "task", "Отправить отчет", "2026-10-23", None)

    def test_task_end_of_week(self):
        self.check("Поставь задачу купить билеты до конца недели", "task", "Купить билеты", "2026-10-25", None)

    def test_meeting_tomorrow(self):
        self.check("Добавь встречу с командой завтра в 15:00", "meeting", "Встреча с командой", "2026-10-22", "15:00")

    def test_meeting_thursday(self):
        self.check("В четверг в 11 у меня встреча с дизайнером", "meeting", "Встреча с дизайнером", "2026-10-22", "11:00")

    def test_relative_hours(self):
        self.check("Напомни через два часа выпить таблетку", "reminder", "Выпить таблетку", "2026-10-21", "16:30")

    def test_relative_minutes_digits(self):
        self.check("напомни через 30 минут проверить почту", "reminder", "Проверить почту", "2026-10-21", "15:00")

    def test_day_after_tomorrow(self):
        self.check("Купить молоко и хлеб послезавтра", "task", "Купить молоко и хлеб", "2026-10-23", None)

    def test_evening_and_ё(self):
        self.check("Напомни в 7 вечера забрать посылку", "reminder", "Забрать посылку", "2026-10-21", "19:00")
        self.check("завтра в 8:30 сдать отчёт", "task", "Сдать отчёт", "2026-10-22", "08:30")

    def test_date_with_month(self):
        self.check("Встреча с врачом 25 октября в 9:30", "meeting", "Встреча с врачом", "2026-10-25", "09:30")

    def test_next_week(self):
        self.check("на следующей неделе подготовить презентацию", "task", "Подготовить презентацию", "2026-10-26", None)

    def test_same_weekday_goes_to_next_week(self):
        self.check("в среду отправить письмо", "task", "Отправить письмо", "2026-10-28", None)

    def test_bare_task(self):
        self.check("Позвонить маме", "task", "Позвонить маме", None, None)

    def test_meeting_without_time_is_incomplete(self):
        c = p("Поставь встречу с Иваном завтра")
        self.assertEqual((c.kind, c.title, c.date, c.time), ("meeting", "Встреча с Иваном", "2026-10-22", None))

    def test_pending_answer(self):
        c = p("в три", {"kind": "meeting", "title": "Встреча с Иваном", "date": "2026-10-22", "time": None})
        self.assertEqual((c.intent, c.kind, c.title, c.date, c.time), ("create", "meeting", "Встреча с Иваном", "2026-10-22", "15:00"))


class ListAndSearch(unittest.TestCase):
    def test_list_phrasings_same_action(self):
        for s in ["Что у меня сегодня?", "Какие планы на сегодня?", "Покажи мои встречи на сегодня", "Есть ли у меня встречи сегодня?"]:
            c = p(s)
            self.assertEqual((c.intent, c.range_start, c.range_end), ("list", "2026-10-21", "2026-10-21"), s)

    def test_list_kinds(self):
        self.assertEqual(p("Покажи мои встречи на сегодня").kind, "meeting")
        c = p("Какие задачи у меня на завтра?")
        self.assertEqual((c.intent, c.kind, c.range_start), ("list", "task", "2026-10-22"))
        self.assertIsNone(p("Что у меня сегодня?").kind)

    def test_list_week(self):
        c = p("Что запланировано на эту неделю?")
        self.assertEqual((c.range_start, c.range_end), ("2026-10-21", "2026-10-25"))
        c = p("Что у меня на следующей неделе?")
        self.assertEqual((c.range_start, c.range_end), ("2026-10-26", "2026-11-01"))

    def test_search(self):
        c = p("Найди задачу про презентацию")
        self.assertEqual((c.intent, c.kind, c.query), ("search", "task", "презентацию"))
        c = p("Когда у меня была встреча с Иваном?")
        self.assertEqual((c.intent, c.kind, c.query), ("search", "meeting", "иваном"))
        c = p("Я когда-то ставил задачу по договору, найди ее")
        self.assertEqual((c.intent, c.query), ("search", "договору"))

    def test_show_task_about_is_search(self):
        self.assertEqual(p("Покажи задачу про договор").intent, "search")

    def test_complete(self):
        c = p("Отметь задачу про договор выполненной")
        self.assertEqual((c.intent, c.query), ("complete", "договор"))

    def test_greeting_and_help(self):
        self.assertEqual(p("Привет").intent, "other")
        self.assertEqual(p("Что ты умеешь?").intent, "other")


if __name__ == "__main__":
    unittest.main()
