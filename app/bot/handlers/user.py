"""Пользовательские хендлеры: старт, триал, подписка, инструкция."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.config import settings
from app.db import repo
from app.db.models import User
from app.services import provisioning
from app.services import subscription as sub_service

log = logging.getLogger(__name__)
router = Router()


async def _send_home(message: Message, session: AsyncSession, user: User) -> None:
    await message.answer(
        texts.greeting(),
        reply_markup=kb.main_menu(),
    )
    await message.answer(
        texts.no_subscription() if not user.trial_used else texts.TRIAL_USED,
        reply_markup=kb.start_inline(trial_available=not user.trial_used),
    )


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, user: User) -> None:
    sub = await repo.latest_subscription(session, user.id)
    if sub is not None and sub_service.is_live(sub):
        await message.answer(texts.greeting(), reply_markup=kb.main_menu())
        await _show_subscription(message, session, sub)
        return
    await _send_home(message, session, user)


async def _show_subscription(
    target: Message, session: AsyncSession, sub
) -> None:
    ready, total = await provisioning.ready_count(session, sub.id)
    text = texts.subscription_card(
        expires_at=sub.expires_at,
        is_trial=sub.is_trial,
        sub_url=settings.sub_url(sub.sub_token),
        ready=ready,
        total=total,
        devices=sub.device_limit,
    )
    await target.answer(text, reply_markup=kb.subscription_kb(has_sub=True))


@router.message(F.text == "🔑 Моя подписка")
@router.callback_query(F.data == kb.CB_SUB)
async def show_subscription(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    message = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()

    sub = await repo.latest_subscription(session, user.id)
    if sub is None:
        await message.answer(
            texts.no_subscription(),
            reply_markup=kb.start_inline(trial_available=not user.trial_used),
        )
        return

    if not sub_service.is_live(sub):
        await message.answer(
            texts.expired_card(sub.expires_at),
            reply_markup=kb.subscription_kb(has_sub=False),
        )
        return

    await _show_subscription(message, session, sub)


@router.callback_query(F.data == kb.CB_TRIAL)
async def start_trial(
    call: CallbackQuery, session: AsyncSession, user: User
) -> None:
    existing = await repo.latest_subscription(session, user.id)
    if existing is not None and sub_service.is_live(existing):
        await call.answer("Подписка уже активна", show_alert=True)
        return

    sub = await sub_service.start_trial(session, user)
    if sub is None:
        await call.answer("Пробный период уже использован", show_alert=True)
        await call.message.answer(texts.PAY_INTRO, reply_markup=kb.plans_kb())
        return

    await call.answer()
    # Коммит произойдёт в middleware; провиженинг подхватит задачи из очереди
    # уже после этого — здесь панель не ждём.
    await session.flush()
    await _show_subscription(call.message, session, sub)


@router.message(F.text == "📱 Как подключить")
@router.callback_query(F.data == kb.CB_HELP)
async def show_help(event: Message | CallbackQuery) -> None:
    message = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await message.answer(
        texts.SETUP_GUIDE,
        reply_markup=kb.back_home_kb(),
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


@router.callback_query(F.data == kb.CB_HOME)
async def go_home(
    call: CallbackQuery, session: AsyncSession, user: User
) -> None:
    await call.answer()
    sub = await repo.latest_subscription(session, user.id)
    if sub is not None and sub_service.is_live(sub):
        await _show_subscription(call.message, session, sub)
        return
    await call.message.answer(
        texts.no_subscription() if not user.trial_used else texts.TRIAL_USED,
        reply_markup=kb.start_inline(trial_available=not user.trial_used),
    )


@router.callback_query(F.data == kb.CB_ROTATE)
async def ask_rotate(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.answer(
        "Сбросить ссылку?\n\n"
        "Старая перестанет работать сразу — на всех устройствах придётся "
        "вставить новую. Нужно, если ссылка утекла.",
        reply_markup=kb.rotate_confirm_kb(),
    )


@router.callback_query(F.data == kb.CB_ROTATE_OK)
async def do_rotate(
    call: CallbackQuery, session: AsyncSession, user: User
) -> None:
    sub = await repo.latest_subscription(session, user.id)
    if sub is None or not sub_service.is_live(sub):
        await call.answer("Активной подписки нет", show_alert=True)
        return

    await sub_service.rotate_token(session, sub)
    await session.flush()
    await call.answer("Готово")
    await call.message.answer(
        "Ссылка сброшена. Новая — ниже, старая больше не работает."
    )
    await _show_subscription(call.message, session, sub)
