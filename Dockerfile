# Публикация сайта: облачный режим (Gemini) + локальный запасной (правила и n-граммный поиск).
# Тяжёлые локальные модели (Whisper) здесь не ставятся: на сервере Gemini доступен без VPN.
FROM python:3.12-slim
WORKDIR /srv
COPY app/requirements.txt app/requirements.txt
RUN pip install --no-cache-dir -r app/requirements.txt
COPY app app
RUN mkdir -p /data && chmod 777 /data
ENV HOST=0.0.0.0 PORT=8000 MULTI_USER=1 PRELOAD_LOCAL=0 DATA_FILE=/data/assistant_data.csv
EXPOSE 8000
CMD ["python", "app/server.py"]
