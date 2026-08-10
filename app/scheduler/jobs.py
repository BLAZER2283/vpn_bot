"""Фоновые задачи.

Каждая джоба сама открывает сессию и коммитит: они не связаны с апдейтами
бота и должны выживать независимо друг от друга.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.db import repo
from app.db.base import session_factory
from app.db.models import SubStatus, Subscription
from app.db.repo import utcnow
from app.services import notify, provisioning
from app.services import nodes as node_service
from app.services import subscription as sub_service

log = logging.getLogger(__name__)


async def job_provision() -> None:
    """Разгрести очередь выдачи. Основная рабочая лошадка."""
    try:
        await provisioning.drain(max_batches=5)
    except Exception:  # noqa: BLE001
        log.exception("провиженинг упал")


async def job_expire(bot: Bot) -> None:
    """Погасить истёкшие подписки."""
    async with session_factory() as session:
        due = list(
            (
                await session.execute(
                    select(Subscription).where(
                        Subscription.status == SubStatus.ACTIVE.value,
                        Subscription.expires_at <= utcnow(),
                    )
                )
            ).scalars()
        )
        for sub in due:
            await sub_service.expire_subscription(session, sub)
        await session.commit()

        for sub in due:
            full = await repo.subscription_with_user(session, sub.id)
            if full is None:
                continue
            await notify.send_safe(
                bot,
                full.user.tg_id,
                "Подписка закончилась.\n\n"
                "Ссылка сохранена — после продления доступ вернётся сам, "
                "перенастраивать приложение не нужно.",
            )

    if due:
        log.info("погашено подписок: %d", len(due))


async def job_notify_expiring(bot: Bot) -> None:
    """Напомнить за 3 дня и за 1 день."""
    now = utcnow()
    async with session_factory() as session:
        for days, flag in ((3, "notified_3d"), (1, "notified_1d")):
            stmt = select(Subscription).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
                Subscription.expires_at <= now + timedelta(days=days),
                getattr(Subscription, flag).is_(False),
            )
            subs = list((await session.execute(stmt)).scalars())
            for sub in subs:
                full = await repo.subscription_with_user(session, sub.id)
                if full is None:
                    continue

                left = "1 день" if days == 1 else "3 дня"
                sent = await notify.send_safe(
                    bot,
                    full.user.tg_id,
                    f"Подписка заканчивается через {left} "
                    f"({sub.expires_at:%d.%m}).\n\n"
                    "Продлить можно в «Моя подписка» — ссылка не изменится.",
                )
                # Ставим флаг независимо от доставки: если человек
                # заблокировал бота, ретраить бессмысленно.
                setattr(sub, flag, True)
                if not sent:
                    log.info("не доставлено напоминание %s", full.user.tg_id)
            await session.commit()


async def job_sync_nodes(bot: Bot) -> None:
    """Перечитать инбаунды: подхватить ротацию REALITY-ключей и смену путей.

    Без этого после ротации ключей на сервере все подписки молча перестали бы
    работать, а причина была бы неочевидна.
    """
    async with session_factory() as session:
        nodes = await repo.all_nodes(session)
        broken: list[str] = []
        for node in nodes:
            if not node.is_active:
                continue
            try:
                inbounds = await node_service.sync_inbounds(session, node)
                if not any(i.is_active for i in inbounds):
                    broken.append(f"{node.name}: нет рабочих инбаундов")
            except Exception as exc:  # noqa: BLE001
                broken.append(f"{node.name}: {exc}")
        await session.commit()

    if broken:
        await notify.notify_admins(
            bot, "⚠️ Проблемы с серверами:\n" + "\n".join(broken)
        )


async def job_report_stuck(bot: Bot) -> None:
    """Раз в сутки сообщить, если что-то зависло в выдаче."""
    async with session_factory() as session:
        s = await repo.stats(session)
    if s["stuck"]:
        await notify.notify_admins(
            bot,
            f"⚠️ Зависших выдач: {s['stuck']}. Посмотреть — /nodes, "
            "повторить — /drain",
        )


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
    )

    scheduler.add_job(job_provision, "interval", seconds=30, id="provision")
    scheduler.add_job(job_expire, "interval", minutes=10, args=[bot], id="expire")
    scheduler.add_job(
        job_notify_expiring, "interval", hours=1, args=[bot], id="notify"
    )
    scheduler.add_job(
        job_sync_nodes, "interval", minutes=30, args=[bot], id="sync_nodes"
    )
    scheduler.add_job(
        job_report_stuck, "interval", hours=24, args=[bot], id="report_stuck"
    )
    return scheduler
