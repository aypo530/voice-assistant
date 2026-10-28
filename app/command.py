"""Структура «разобранного запроса» — общий формат для облачного (Gemini) и локального разборщика."""
from dataclasses import dataclass, fields
from typing import Optional

INTENTS = {"create", "list", "search", "complete", "delete", "update", "clarify", "other"}
KINDS = {"task", "meeting", "reminder"}
REPEATS = {"daily", "weekdays", "weekly", "monthly"}


@dataclass
class Command:
    intent: str = "other"
    transcript: str = ""
    kind: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    query: Optional[str] = None
    ref: Optional[str] = None      # "last" — «последнюю запись», "it" — «её / эту» (последняя упомянутая)
    repeat: Optional[str] = None   # daily | weekdays | weekly | monthly — повторяющаяся запись
    reply: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "Command":
        known = {f.name for f in fields(cls)}
        clean = {k: str(v).strip() for k, v in data.items()
                 if k in known and v is not None and str(v).strip().lower() not in ("", "null", "none")}
        cmd = cls(**clean)
        if cmd.intent not in INTENTS:
            cmd.intent = "other"
        if cmd.kind not in KINDS:
            cmd.kind = None
        if cmd.repeat not in REPEATS:
            cmd.repeat = None
        return cmd
