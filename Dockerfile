# Публикация на Amvera: облачный режим (GigaChat) + локальный запасной (правила и n-граммный поиск).
# Тяжёлые локальные эмбеддинги здесь не ставятся: на сервере GigaChat доступен из России без VPN.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Сертификаты НУЦ Минцифры обязательны: без них TLS-соединение с api.giga.chat и OAuth-сервером Сбера
# не устанавливается («self-signed certificate in certificate chain»). Сертификаты лежат в репозитории
# (certs/), поэтому сборка не зависит от доступа к gu-st.ru. Ставим и в систему, и в certifi.
COPY certs /usr/local/share/ca-certificates/russian-trusted

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /srv
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt faster-whisper \
 && cat /usr/local/share/ca-certificates/russian-trusted/*.crt >> "$(python -m certifi)"

# Запасное офлайн-распознавание речи (когда GigaChat или его аудио недоступны): модель tiny скачивается при сборке.
# tiny (~75 МБ) — самая быстрая: на слабом процессоре base/small распознают минуту и дольше.
# Точность выше с WHISPER_MODEL=small (нужен более мощный тариф).
ENV WHISPER_MODEL=tiny MODELS_DIR=/srv/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('tiny', device='cpu', compute_type='int8', download_root='/srv/models')" || echo "Модель Whisper не скачалась при сборке — загрузится при первом запросе"
RUN chmod -R a+rX /srv/models 2>/dev/null || true

COPY app app
RUN mkdir -p /data && chmod 777 /data

# Amvera: постоянное хранилище монтируется в /data (см. amvera.yml), записи переживают перезапуск.
# MULTI_USER=1 — у каждого браузера свои записи.
ENV HOST=0.0.0.0 PORT=8000 MULTI_USER=1 PRELOAD_LOCAL=1 DATA_FILE=/data/assistant_data.csv
EXPOSE 8000
CMD ["python", "app/server.py"]
