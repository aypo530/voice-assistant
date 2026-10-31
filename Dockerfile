# Публикация сайта: облачный режим (GigaChat) + локальный запасной (правила и n-граммный поиск).
# GigaChat работает из России без VPN, поэтому сертификаты Минцифры уже лежат в образе (app/certs).
FROM python:3.12-slim
WORKDIR /srv

# ca-certificates нужны для TLS-проверки api.giga.chat вместе с сертификатом Минцифры из app/certs
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt faster-whisper

# Запасное офлайн-распознавание речи (когда GigaChat не может обработать аудио): модель tiny скачивается при сборке.
# tiny (~75 МБ) — самая быстрая: на слабом процессоре base/small распознают минуту и дольше.
# Точность ниже; для неё нужен тариф помощнее и WHISPER_MODEL=small.
ENV WHISPER_MODEL=tiny MODELS_DIR=/srv/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8', download_root='/srv/models')" || echo "Модель Whisper не скачалась при сборке — загрузится при первом запросе"
RUN chmod -R a+rX /srv/models 2>/dev/null || true

COPY app app
RUN mkdir -p /data && chmod 777 /data
# PRELOAD_LOCAL=0: локальные модели грузятся по требованию — на старте меньше памяти.
ENV HOST=0.0.0.0 PORT=8000 MULTI_USER=1 PRELOAD_LOCAL=0 DATA_FILE=/data/assistant_data.csv \
    GIGACHAT_SCOPE=GIGACHAT_API_PERS
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/api/health', timeout=4)"
CMD ["python", "app/server.py"]
