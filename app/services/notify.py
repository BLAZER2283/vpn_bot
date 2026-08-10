"""Отправка сообщений пользователям и админам.

Телеграм активно наказывает за игнорирование лимитов, поэтому рассылка идёт
с паузами, а блокировка бота пользователем — не ошибка, а причина пометить
его в БД и больше не пытаться.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.config import settings
from app.db import repo
from app.db.base import session_factory

log = logging.getLogger(__name__)

# ~25 сообщений в секунду — практический предел для массовых рассылок.
BROADCAST_DELAY = 0.05


async def send_safe(
    bot: Bot, tg_id: int, text: str, *, reply_markup=None
) -> bool:
    """Отправить, не роняя вызывающий код. False — доставить не удалось."""
    try:
        await bot.send_message(tg_id, text, reply_markup=reply_markup)
        return True
    except TelegramForbiddenError:
        # Пользователь заблокировал бота. Ретраить бессмысленно.
        async with session_factory() as session:
            await repo.set_banned(session, tg_id, True)
            await session.commit()
        return False
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        try:
            await bot.send_message(tg_id, text, reply_markup=reply_markup)
            return True
        except Exception:  # noqa: BLE001
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning("не отправилось %s: %s", tg_id, exc)
        return False


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in settings.admins:
        await send_safe(bot, admin_id, text)


async def broadcast(bot: Bot, text: str) -> tuple[int, int]:
    """Рассылка всем незаблокированным. Возвращает (доставлено, всего)."""
    async with session_factory() as session:
        tg_ids = await repo.all_user_tg_ids(session)

    sent = 0
    for tg_id in tg_ids:
        if await send_safe(bot, tg_id, text):
            sent += 1
        await asyncio.sleep(BROADCAST_DELAY)
    return sent, len(tg_ids)
