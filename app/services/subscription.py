"""Жизненный цикл подписки.

Правило, вокруг которого построен модуль: поход в панели никогда не стоит
на пути денег. Платёж и подписка фиксируются в БД одной транзакцией, клиенты
на панелях ставятся в очередь, воркер разгребает её асинхронно. Если панель
лежит — пользователь всё равно с оплаченной активной подпиской, ссылка
валидна и наполнится сама.
"""

from __future__ import annotations

import logging
import secrets
import uuid as uuid_lib
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo
from app.db.models import ClientState, Device, SubStatus, Subscription, User
from app.db.repo import utcnow

log = logging.getLogger(__name__)


def _new_token() -> str:
    return secrets.token_urlsafe(24)


def client_email(sub_id: int) -> str:
    """Идентификатор клиента в панели. Стабилен, легко найти глазами."""
    return f"sub{sub_id}"


def add_months(moment: datetime, months: int) -> datetime:
    """Прибавить месяцы без внешних зависимостей.

    30 дней на месяц — сознательное упрощение: предсказуемо для пользователя
    и не порождает вопросов «почему подписка кончилась 28-го».
    """
    return moment + timedelta(days=30 * months)


async def _enqueue_all_inbounds(session: AsyncSession, sub: Subscription) -> None:
    """Поставить устройства подписки в очередь на разрешенных нодах."""
    inbound_ids = await repo.assigned_inbound_ids(session, sub.id)
    devices = await repo.devices_of_subscription(session, sub.id)
    for device in devices:
        email = client_email(sub.id) if device.id == devices[0].id else f"sub{sub.id}d{device.id}"
        for inbound_id in inbound_ids:
            await repo.enqueue_client(
                session, sub.id, inbound_id, email, device.id
            )
    if not inbound_ids:
        log.warning("нет активных инбаундов — подписке %s нечего выдавать", sub.id)


async def create_subscription(
    session: AsyncSession,
    user: User,
    *,
    days: int | None = None,
    months: int | None = None,
    is_trial: bool = False,
) -> Subscription:
    """Новая подписка. Токен и UUID выдаются один раз и живут до ротации."""
    if days is None and months is None:
        raise ValueError("нужен либо days, либо months")

    now = utcnow()
    expires = now + timedelta(days=days) if days else add_months(now, months or 0)

    sub = Subscription(
        user_id=user.id,
        sub_token=_new_token(),
        client_uuid=str(uuid_lib.uuid4()),
        status=SubStatus.ACTIVE.value,
        expires_at=expires,
        device_limit=settings.device_limit,
        is_trial=is_trial,
    )
    session.add(sub)
    await session.flush()  # нужен sub.id для email клиентов

    device = Device(
        subscription_id=sub.id,
        name="Основное устройство",
        device_token=sub.sub_token,
        client_uuid=sub.client_uuid,
    )
    session.add(device)
    await session.flush()

    for node_id in await repo.active_node_ids(session):
        await repo.assign_node(session, sub.id, node_id)

    await _enqueue_all_inbounds(session, sub)
    return sub


async def extend_subscription(
    session: AsyncSession, sub: Subscription, months: int
) -> Subscription:
    """Продлить существующую подписку.

    Ссылка не меняется — это ключевое требование: у человека уже настроен
    клиент, и продление не должно заставлять его что-то переделывать.
    """
    now = utcnow()
    base = sub.expires_at if sub.expires_at > now else now
    sub.expires_at = add_months(base, months)
    sub.status = SubStatus.ACTIVE.value
    sub.is_trial = False
    sub.notified_3d = False
    sub.notified_1d = False

    # Клиенты могли быть выключены при истечении — включаем обратно
    # и добираем инбаунды, появившиеся за время простоя.
    await repo.mark_clients(session, sub.id, ClientState.PENDING)
    await _enqueue_all_inbounds(session, sub)
    return sub


async def grant_months(
    session: AsyncSession, user: User, months: int
) -> tuple[Subscription, bool]:
    """Начислить месяцы: продлить активную подписку или создать новую.

    Возвращает (подписка, была_ли_создана_новая).
    """
    existing = await repo.latest_subscription(session, user.id)
    if existing is not None and existing.status != SubStatus.BANNED.value:
        return await extend_subscription(session, existing, months), False
    return await create_subscription(session, user, months=months), True


async def grant_days(
    session: AsyncSession, user: User, days: int
) -> tuple[Subscription, bool]:
    """Начислить дни — ручная выдача админом, компенсация, подарок."""
    existing = await repo.latest_subscription(session, user.id)
    if existing is None or existing.status == SubStatus.BANNED.value:
        return await create_subscription(session, user, days=days), True

    now = utcnow()
    base = existing.expires_at if existing.expires_at > now else now
    existing.expires_at = base + timedelta(days=days)
    existing.status = SubStatus.ACTIVE.value
    existing.notified_3d = False
    existing.notified_1d = False
    await repo.mark_clients(session, existing.id, ClientState.PENDING)
    await _enqueue_all_inbounds(session, existing)
    return existing, False


async def start_trial(
    session: AsyncSession, user: User
) -> Subscription | None:
    """Пробный период. None — если уже использован."""
    if user.trial_used:
        return None
    user.trial_used = True
    return await create_subscription(
        session, user, days=settings.trial_days, is_trial=True
    )


async def add_device(
    session: AsyncSession, user: User, name: str
) -> tuple[Device | None, int]:
    sub = await repo.latest_subscription(session, user.id)
    if sub is None or not is_live(sub):
        return None, 0
    devices = await repo.devices_of_subscription(session, sub.id)
    if len(devices) >= settings.device_limit:
        return None, len(devices)

    device = Device(
        subscription_id=sub.id,
        name=name or f"Устройство {len(devices) + 1}",
        device_token=_new_token(),
        client_uuid=str(uuid_lib.uuid4()),
    )
    session.add(device)
    await session.flush()
    await _enqueue_all_inbounds(session, sub)
    return device, len(devices) + 1


async def remove_device(
    session: AsyncSession, user: User, device_id: int
) -> bool:
    sub = await repo.latest_subscription(session, user.id)
    if sub is None:
        return False
    devices = await repo.devices_of_subscription(session, sub.id)
    device = await repo.device_by_id(session, sub.id, device_id)
    if device is None or len(devices) <= 1:
        return False
    await repo.deactivate_device(session, device)
    return True


async def expire_subscription(session: AsyncSession, sub: Subscription) -> None:
    """Погасить подписку: клиенты выключаются, ссылка остаётся валидной.

    Именно выключение, а не удаление: при продлении клиент включается обратно,
    и человеку не нужно переустанавливать конфиг.
    """
    sub.status = SubStatus.EXPIRED.value
    await repo.mark_clients(session, sub.id, ClientState.DISABLING)


async def rotate_token(session: AsyncSession, sub: Subscription) -> Subscription:
    """Сбросить ссылку и UUID — например, если ключ утёк.

    Старая ссылка умирает сразу; клиенты на панелях перевыпускаются.
    """
    sub.sub_token = _new_token()
    sub.client_uuid = str(uuid_lib.uuid4())
    for device in await repo.devices_of_subscription(session, sub.id):
        device.device_token = sub.sub_token if device.name == "Основное устройство" else _new_token()
        device.client_uuid = sub.client_uuid if device.name == "Основное устройство" else str(uuid_lib.uuid4())
    await repo.mark_clients(session, sub.id, ClientState.PENDING)
    return sub


def is_live(sub: Subscription) -> bool:
    return sub.status == SubStatus.ACTIVE.value and sub.expires_at > utcnow()
