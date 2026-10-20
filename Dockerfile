# Публикация сайта: облачный режим (Gemini) + локальный запасной (правила и n-граммный поиск).
# Тяжёлые локальные модели (Whisper) здесь не ставятся: на сервере Gemini доступен без VPN.
FROM python:3.12-slim
WORKDIR /srv
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt faster-whisper
# Запасное офлайн-распознавание речи (когда Gemini недоступен): модель base скачивается при сборке, а не при запросе.
# base (~150 МБ) помещается в бесплатный тариф; для точности выше задайте WHISPER_MODEL=small на тарифе с 1+ ГБ памяти.
ENV WHISPER_MODEL=base MODELS_DIR=/srv/models
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8', download_root='/srv/models')" || echo "Модель Whisper не скачалась при сборке — загрузится при первом запросе"
RUN chmod -R a+rX /srv/models 2>/dev/null || true
COPY app app
RUN mkdir -p /data && chmod 777 /data
ENV HOST=0.0.0.0 PORT=8000 MULTI_USER=1 PRELOAD_LOCAL=0 DATA_FILE=/data/assistant_data.csv
EXPOSE 8000
CMD ["python", "app/server.py"]
