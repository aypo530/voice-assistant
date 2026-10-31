"""Локальное (офлайн) распознавание речи: faster-whisper. Включается, когда GigaChat не может обработать аудио.

Модель скачивается один раз (WHISPER_MODEL, по умолчанию small ≈ 0.5 ГБ) в папку models/ и дальше работает без интернета.
"""
import io
import logging
import threading
import wave
from typing import Optional

import config

log = logging.getLogger("stt")

_lock = threading.Lock()
_model = None
_state = {"state": "unknown", "message": ""}     # unknown | loading | ready | missing | error
_PROMPT = "Напомни завтра в десять утра позвонить Ивану. Поставь встречу с командой. Что у меня на этой неделе? Найди задачу про договор."


class STTUnavailable(Exception):
    pass


def state() -> dict:
    return dict(_state)


def _load():
    global _model
    with _lock:
        if _model is not None:
            return _model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            _state.update(state="missing", message="faster-whisper не установлен (pip install -r app/requirements-local.txt)")
            raise STTUnavailable(_state["message"]) from e
        try:
            _state.update(state="loading", message="загрузка модели распознавания речи…")
            config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
            _model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8",
                                  download_root=str(config.MODELS_DIR))
            _state.update(state="ready", message="")
        except Exception as e:
            _state.update(state="error", message=f"не удалось загрузить модель: {str(e)[:150]}")
            log.warning("Whisper: %s", e)
            raise STTUnavailable(_state["message"]) from e
        return _model


def preload_async() -> None:
    def run():
        try:
            _load()
            log.info("Локальное распознавание речи готово (%s)", config.WHISPER_MODEL)
        except STTUnavailable as e:
            log.info("Локальное распознавание речи недоступно: %s", e)
    threading.Thread(target=run, daemon=True, name="stt-preload").start()


def _wav_to_array(data: bytes):
    import numpy as np
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise ValueError("ожидается 16-битный WAV")
        frames = w.readframes(w.getnframes())
        ch, rate = w.getnchannels(), w.getframerate()
    a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 16000 and len(a):
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * 16000 / rate)), np.arange(len(a)), a).astype(np.float32)
    return a


def transcribe(audio: bytes, mime: str = "audio/wav") -> str:
    model = _load()
    try:
        source = _wav_to_array(audio) if "wav" in mime else io.BytesIO(audio)
    except Exception:
        source = io.BytesIO(audio)           # пусть декодирует PyAV (входит в faster-whisper)
    try:
        segments, _ = model.transcribe(source, language="ru", beam_size=1, vad_filter=True,
                                       initial_prompt=_PROMPT, condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as e:
        log.warning("Whisper: ошибка распознавания: %s", e)
        raise STTUnavailable("не удалось распознать запись локально") from e
