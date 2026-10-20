"""Запуск: python run.py (его же вызывают start.bat / start.sh).

Сам делает всё нужное: доустанавливает библиотеки (при первом запуске), стартует сервер, открывает браузер.
Ключ Gemini необязателен: без него (или если Gemini недоступен без VPN) ассистент работает локально.
"""
import importlib
import os
import socket
import subprocess
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CORE = [("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("google.genai", "google-genai"),
        ("dotenv", "python-dotenv"), ("tzdata", "tzdata")]
LOCAL = [("faster_whisper", "faster-whisper"), ("fastembed", "fastembed")]


def _missing(spec) -> list:
    out = []
    for module, package in spec:
        try:
            importlib.import_module(module)
        except ImportError:
            out.append(package)
    return out


def _pip(packages: list) -> bool:
    print("Устанавливаю библиотеки:", ", ".join(packages), "(первый запуск, может занять несколько минут)")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_deps() -> None:
    core = _missing(CORE)
    if core and not _pip(core):
        raise SystemExit("Не удалось установить обязательные библиотеки. Проверьте интернет и запустите снова.")
    if os.getenv("LITE", "0") != "1":
        local = _missing(LOCAL)
        if local and not _pip(local):
            print("\n!!! Не удалось установить библиотеки для локального режима (faster-whisper, fastembed).\n"
                  "    Облачный режим и текстовый ввод будут работать; офлайн-распознавание речи — нет.\n")


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return preferred


def main() -> None:
    sys.path.insert(0, str(APP_DIR))
    ensure_deps()
    import uvicorn
    import config
    import server

    port = free_port(config.PORT)
    url = f"http://127.0.0.1:{port}"
    print(f"\nАссистент запущен: {url}\nДанные сохраняются в файл: {config.DATA_FILE}")
    if config.GEMINI_API_KEY:
        print("Режим: облачный (Gemini). Если Gemini недоступен (например, нужен VPN) — ассистент сам переключится\n"
              "на локальный режим: ответ придёт без задержки, но понимание запросов и распознавание речи будут менее точными.")
    else:
        print("Режим: локальный (ключ Gemini не задан). Точность ниже, чем в облачном режиме. Ключ можно ввести в окне при входе на сайт или в файле .env (GEMINI_API_KEY=...)")
    print("При первом запуске локальные модели скачиваются в фоне (нужен интернет, потом — офлайн).")
    print("Не закрывайте это окно, пока работаете. Закройте его (или Ctrl+C), чтобы остановить.\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code:
            print(e.code)
    except Exception:
        traceback.print_exc()
        input("\nОшибка запуска. Нажмите Enter, чтобы закрыть…")
