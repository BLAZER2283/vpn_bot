"""Сборка роутеров. Порядок важен: admin раньше user, иначе общий
текстовый хендлер перехватит админские команды.
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import admin, payments, user


def build_router() -> Router:
    router = Router()
    router.include_router(admin.router)
    router.include_router(payments.router)
    router.include_router(user.router)
    return router
