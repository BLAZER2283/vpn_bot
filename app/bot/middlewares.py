"""Middleware: сессия БД и запись пользователя в каждом апдейте."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User as TgUser

from app.db import repo
from app.db.base import session_factory

log = logging.getLogger(__name__)


class DbSessionMiddleware(BaseMiddleware):
    """Одна сессия на апдейт с коммитом в конце.

    Хендлеры не коммитят сами: если хендлер упал на середине, откатывается
    всё — не бывает подписки без платежа или наоборот.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with session_factory() as session:
            data["session"] = session
            try:
                result = await handler(event, data)
            except Exception:
                await session.rollback()
                raise
            await session.commit()
            return result


class UserMiddleware(BaseMiddleware):
    """Подкладывает в data актуальную запись пользователя."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        session = data.get("session")
        if tg_user is not None and session is not None:
            user = await repo.get_or_create_user(
                session, tg_user.id, tg_user.username
            )
            # Пользователь мог заблокировать бота, а потом вернуться —
            # снимаем отметку, чтобы снова получать уведомления.
            if user.is_banned:
                user.is_banned = False
            data["user"] = user
        return await handler(event, data)
