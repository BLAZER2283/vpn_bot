"""Оплата: выбор тарифа, счёт, зачисление.

Критичное место — зачисление. Telegram может доставить successful_payment
повторно, а админ может дважды нажать «Зачесть». Оба случая упираются в
уникальный provider_payment_id, поэтому начисление происходит ровно раз.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot import texts
from app.config import settings
from app.db import repo
from app.db.models import User
from app.payments import manual
from app.payments import service as pay_service
from app.payments.stars import provider as stars
from app.services import notify, provisioning

log = logging.getLogger(__name__)
router = Router()


async def _send_subscription_card(
    message: Message, session: AsyncSession, sub, *, head: str
) -> None:
    ready, total = await provisioning.ready_count(session, sub.id)
    await message.answer(
        f"<b>{head}</b>\n\nДоступ до {sub.expires_at:%d.%m.%Y}."
    )
    await message.answer(
        texts.subscription_card(
            expires_at=sub.expires_at,
            is_trial=False,
            sub_url=settings.sub_url(sub.sub_token),
            ready=ready,
            total=total,
            devices=sub.device_limit,
        ),
        reply_markup=kb.subscription_kb(has_sub=True),
    )


@router.message(F.text == "💳 Купить")
@router.callback_query(F.data == kb.CB_BUY)
async def show_plans(event: Message | CallbackQuery) -> None:
    message = event if isinstance(event, Message) else event.message
    if isinstance(event, CallbackQuery):
        await event.answer()
    await message.answer(texts.PAY_INTRO, reply_markup=kb.plans_kb())


@router.callback_query(F.data.startswith(f"{kb.CB_PLAN}:"))
async def choose_method(call: CallbackQuery) -> None:
    code = call.data.split(":", 1)[1]
    plan = pay_service.plan_by_code(code)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return
    await call.answer()
    await call.message.answer(
        f"<b>{plan.title}</b> — {plan.price} ⭐\n\nКак платим?",
        reply_markup=kb.pay_method_kb(plan.code),
    )


# ── Telegram Stars ──────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith(f"{kb.CB_PAY_STARS}:"))
async def send_invoice(call: CallbackQuery) -> None:
    code = call.data.split(":", 1)[1]
    plan = pay_service.plan_by_code(code)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    await call.answer()
    invoice = stars.build_invoice(plan, call.from_user.id)
    await call.message.answer_invoice(**invoice.send_invoice_kwargs)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Ответить обязательно в течение 10 секунд, иначе платёж отменится."""
    code = (query.invoice_payload or "").removeprefix("plan:")
    if pay_service.plan_by_code(code) is None:
        await query.answer(ok=False, error_message="Тариф больше недоступен")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, session: AsyncSession, user: User) -> None:
    payment = message.successful_payment
    code = (payment.invoice_payload or "").removeprefix("plan:")
    plan = pay_service.plan_by_code(code)

    if plan is None:
        # Деньги ушли, а тариф исчез. Молчать нельзя — разбираем руками.
        log.error(
            "оплата за неизвестный тариф %r от %s, charge_id=%s",
            code, user.tg_id, payment.telegram_payment_charge_id,
        )
        await message.answer(
            "Оплата получена, но тариф не распознан. Уже разбираемся — "
            "напиши в поддержку, если не активируется в течение часа."
        )
        await notify.notify_admins(
            message.bot,
            f"⚠️ Оплата за неизвестный тариф {code!r} от {user.tg_id}, "
            f"charge_id={payment.telegram_payment_charge_id}",
        )
        return

    try:
        sub, created = await pay_service.credit(
            session,
            user=user,
            plan=plan,
            provider=stars.code,
            provider_payment_id=payment.telegram_payment_charge_id,
            amount=payment.total_amount,
            currency=payment.currency,
        )
    except pay_service.AlreadyProcessed:
        # Повторная доставка того же успешного платежа — штатная ситуация.
        log.info(
            "повторный successful_payment %s проигнорирован",
            payment.telegram_payment_charge_id,
        )
        return

    await session.flush()
    await _send_subscription_card(
        message,
        session,
        sub,
        head="Подписка оформлена" if created else "Подписка продлена",
    )


# ── Перевод с подтверждением админом ────────────────────────────────────────


@router.callback_query(F.data.startswith(f"{kb.CB_PAY_MANUAL}:"))
async def request_manual(call: CallbackQuery, user: User) -> None:
    code = call.data.split(":", 1)[1]
    plan = pay_service.plan_by_code(code)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return

    if not settings.admins:
        await call.answer(
            "Этот способ сейчас недоступен", show_alert=True
        )
        return

    await call.answer()
    # Всё состояние — в callback_data кнопки, поэтому заявка переживёт
    # перезапуск бота и не требует таблицы «висящих» платежей.
    ref = f"{user.tg_id}:{plan.code}:{manual.new_request_id()}"

    await call.message.answer(manual.USER_PENDING_TEXT)
    for admin_id in settings.admins:
        await notify.send_safe(
            call.bot,
            admin_id,
            manual.admin_request_text(
                user_tg_id=user.tg_id,
                username=user.username,
                plan_title=plan.title,
                amount=plan.price,
            ),
            reply_markup=kb.manual_review_kb(ref),
        )


@router.callback_query(F.data.startswith(f"{kb.CB_PAY_OK}:"))
async def confirm_manual(
    call: CallbackQuery, session: AsyncSession
) -> None:
    if call.from_user.id not in settings.admins:
        await call.answer("Недоступно", show_alert=True)
        return

    try:
        tg_id_raw, plan_code, request_id = call.data.split(":", 1)[1].split(":")
    except ValueError:
        await call.answer("Испорченная заявка", show_alert=True)
        return

    plan = pay_service.plan_by_code(plan_code)
    target = await repo.get_user_by_tg(session, int(tg_id_raw))
    if plan is None or target is None:
        await call.answer("Тариф или пользователь не найдены", show_alert=True)
        return

    try:
        sub, created = await pay_service.credit(
            session,
            user=target,
            plan=plan,
            provider=manual.CODE,
            provider_payment_id=f"{manual.CODE}:{request_id}",
            currency=manual.CURRENCY,
        )
    except pay_service.AlreadyProcessed:
        # Вторая кнопка от второго админа или двойной клик.
        await call.answer("Эта заявка уже зачтена", show_alert=True)
        return

    await session.flush()
    await call.answer("Зачтено")
    await call.message.edit_text(
        f"{call.message.html_text}\n\n✅ Зачтено, до {sub.expires_at:%d.%m.%Y}"
    )
    await notify.send_safe(
        call.bot,
        target.tg_id,
        f"Оплата подтверждена. "
        f"{'Подписка оформлена' if created else 'Подписка продлена'} "
        f"до {sub.expires_at:%d.%m.%Y}.\n\n"
        "Ссылка — в «Моя подписка».",
    )


@router.callback_query(F.data.startswith(f"{kb.CB_PAY_NO}:"))
async def reject_manual(call: CallbackQuery, session: AsyncSession) -> None:
    if call.from_user.id not in settings.admins:
        await call.answer("Недоступно", show_alert=True)
        return

    tg_id_raw = call.data.split(":")[1]
    await call.answer("Отклонено")
    await call.message.edit_text(f"{call.message.html_text}\n\n✖️ Отклонено")

    if tg_id_raw.isdigit():
        await notify.send_safe(
            call.bot,
            int(tg_id_raw),
            "Оплата не подтверждена. Если перевод был — напиши в поддержку.",
        )
