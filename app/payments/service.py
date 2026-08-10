"""Единственная точка, где деньги превращаются в подписку.

Идемпотентность здесь не опция: Телеграм пересылает successful_payment
повторно, пользователь жмёт кнопку дважды, платёжки ретраят вебхуки.
Двойное продление за одну оплату — это то, что потом разбирают вручную.

Защита ровно одна и она на уровне БД: уникальный provider_payment_id.
Если вставка не произошла — платёж уже зачтён, выходим не начисляя.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS_BY_CODE, Plan, settings
from app.db import repo
from app.db.models import Subscription, User
from app.services import subscription as sub_service

log = logging.getLogger(__name__)


class AlreadyProcessed(Exception):
    """Платёж с таким id уже зачтён. Начислять повторно нельзя."""


async def credit(
    session: AsyncSession,
    *,
    user: User,
    plan: Plan,
    provider: str,
    provider_payment_id: str,
    amount: int | None = None,
    currency: str | None = None,
) -> tuple[Subscription, bool]:
    """Зачесть оплату: продлить активную подписку или создать новую.

    Возвращает (подписка, создана_ли_новая).
    Бросает AlreadyProcessed, если этот платёж уже обрабатывался.
    """
    payment_id = await repo.register_payment(
        session,
        user_id=user.id,
        provider=provider,
        provider_payment_id=provider_payment_id,
        plan_code=plan.code,
        months=plan.months,
        amount=amount if amount is not None else plan.price,
        currency=currency or settings.currency,
    )
    if payment_id is None:
        raise AlreadyProcessed(provider_payment_id)

    sub, created = await sub_service.grant_months(session, user, plan.months)
    await session.flush()
    await repo.attach_payment_subscription(session, payment_id, sub.id)

    log.info(
        "оплата %s:%s зачтена — юзер %s, +%d мес, до %s",
        provider,
        provider_payment_id,
        user.tg_id,
        plan.months,
        sub.expires_at,
    )
    return sub, created


def plan_by_code(code: str) -> Plan | None:
    return PLANS_BY_CODE.get(code)
