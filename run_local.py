"""Локальный запуск без Docker и Postgres.

    python run_local.py            прогнать сквозной тест (без Telegram)
    python run_local.py bot        поднять живого бота на SQLite
    python run_local.py net        диагностика доступа к Telegram
    python run_local.py check      проверка генератора ссылок

Для второго нужен только BOT_TOKEN — либо в .env, либо в переменной
окружения. Postgres, Docker и рабочая панель не требуются: пока серверов
нет, бот выдаёт подписку с нулём ссылок, а `/addnode` добавит настоящий.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent


def read_env_file() -> dict[str, str]:
    """Грубый разбор .env — нужен только чтобы понять, что настроено."""
    path = ROOT / ".env"
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def has_token(env: dict[str, str]) -> bool:
    return bool(os.environ.get("BOT_TOKEN") or env.get("BOT_TOKEN"))


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"

    if mode == "test":
        print("Сквозной тест на SQLite (Telegram и панель не нужны)\n")
        return subprocess.call([sys.executable, "-m", "scripts.localtest"])

    if mode == "check":
        return subprocess.call([sys.executable, "-m", "scripts.selfcheck"])

    if mode == "net":
        return subprocess.call([sys.executable, "-m", "scripts.netcheck"])

    if mode == "bot":
        env = read_env_file()
        if not has_token(env):
            print(
                "Нужен BOT_TOKEN.\n\n"
                "  1. Получи токен у @BotFather\n"
                "  2. Скопируй .env.example в .env и вставь его туда\n"
                "     (или задай переменную окружения BOT_TOKEN)\n"
            )
            return 2

        # DATABASE_URL из .env уважаем — но только если он достижим.
        # Хост "db" существует лишь внутри docker-compose: при локальном
        # запуске это гарантированный отказ на первом же запросе.
        dsn = os.environ.get("DATABASE_URL") or env.get("DATABASE_URL", "")
        if "@db:" in dsn or "@db/" in dsn:
            print(
                "В .env указан Postgres на хосте 'db' — он есть только\n"
                "внутри docker-compose. Локально переключаюсь на SQLite.\n"
            )
            os.environ["DATABASE_URL"] = (
                f"sqlite+aiosqlite:///{ROOT / 'vpn_bot.db'}"
            )
            dsn = ""

        if dsn and not dsn.startswith("sqlite"):
            print(f"БД из .env: {dsn.split('@')[-1]}")
        else:
            print("БД: SQLite (vpn_bot.db в корне проекта)")

        proxy = os.environ.get("TELEGRAM_PROXY") or env.get("TELEGRAM_PROXY", "")
        print(f"Telegram: {'через ' + proxy if proxy else 'напрямую'}")

        sub_url = (
            os.environ.get("SUB_BASE_URL")
            or env.get("SUB_BASE_URL")
            or "http://127.0.0.1:8080"
        )
        print(f"Подписки: {sub_url}/s/<token>")
        print("Останов — Ctrl+C\n")

        from app.main import main as bot_main

        try:
            asyncio.run(bot_main())
        except KeyboardInterrupt:
            pass
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
