"""Загрузка .env из корня проекта в os.environ (только стандартная библиотека).
Уже заданные переменные окружения не перезаписываются.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path=os.path.join(ROOT, ".env")):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
