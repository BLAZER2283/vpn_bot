"""Точка входа.

Бот, subscription-endpoint и планировщик живут в одном процессе: так проще
деплоить и не нужен второй пул коннектов к БД. Разнести можно потом —
код к этому готов, каждая часть запускается отдельной функцией.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Запуск как `python app/main.py` (или из папки app) не видит пакет `app`.
# Вместо ModuleNotFoundError добавляем корень проекта в путь импорта.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError

from app.bot.handlers import build_router
from app.bot.middlewares import DbSessionMiddleware, UserMiddleware
from app.config import settings
from app.db.base import create_all
from app.panel import pool
from app.scheduler.jobs import setup_scheduler
from app.web.server import start_web

log = logging.getLogger(__name__)


def setup_logging() -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # httpx логирует каждый запрос к панели вместе с URL, а в URL —
    # секретный путь панели. В обычном режиме это лишнее.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def build_session() -> AiohttpSession | None:
    """Сессия с прокси, если он задан.

    aiogram сам разбирает и http://, и socks5:// — для socks нужен только
    установленный aiohttp-socks (он в requirements).
    """
    if not settings.telegram_proxy:
        return None
    return AiohttpSession(proxy=settings.telegram_proxy)


async def main() -> None:
    setup_logging()

    if not settings.admins:
        log.warning("ADMIN_IDS пуст — админ-команды будут недоступны")

    if settings.should_create_schema:
        await create_all()
        log.info("схема создана напрямую (локальный режим)")

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=build_session(),
    )
    dp = Dispatcher()

    # Порядок регистрации важен: сессия должна появиться раньше, чем
    # UserMiddleware попробует ей воспользоваться.
    for observer in (dp.message, dp.callback_query, dp.pre_checkout_query):
        observer.middleware(DbSessionMiddleware())
        observer.middleware(UserMiddleware())

    dp.include_router(build_router())

    runner = await start_web()
    scheduler = setup_scheduler(bot)
    scheduler.start()
    log.info("бот запускается")

    try:
        await dp.start_polling(bot)
    except TelegramNetworkError as exc:
        # Самая частая причина — блокировка api.telegram.org, а не ошибка кода.
        # Из голого трейсбека это неочевидно, поэтому говорим прямо.
        log.error("Telegram недоступен: %s", exc)
        log.error(
            "Нужен прокси. Пропиши в .env:\n"
            "  TELEGRAM_PROXY=http://127.0.0.1:PORT\n"
            "Либо включи VPN на машине, где запускаешь бота."
        )
    finally:
        scheduler.shutdown(wait=False)
        await runner.cleanup()
        await pool.close_all()
        await bot.session.close()
        log.info("остановлено")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
