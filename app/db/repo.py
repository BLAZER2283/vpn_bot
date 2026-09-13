"""Запросы к БД. Никакой бизнес-логики — только выборки и точечные вставки."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Select, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db.base import IS_SQLITE
from app.db.models import (
    ClientState,
    Inbound,
    Node,
    Payment,
    PaymentStatus,
    SubStatus,
    Subscription,
    SubscriptionClient,
    User,
)

# ON CONFLICT есть в обоих диалектах, но конструктор разный.
# Синтаксис .on_conflict_do_*() у них совпадает, поэтому дальше код общий.
insert = sqlite_insert if IS_SQLITE else pg_insert


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── Пользователи ────────────────────────────────────────────────────────────


async def get_or_create_user(
    session: AsyncSession, tg_id: int, username: str | None
) -> User:
    stmt = (
        insert(User)
        .values(tg_id=tg_id, username=username)
        .on_conflict_do_update(
            index_elements=[User.tg_id], set_={"username": username}
        )
        .returning(User.id)
    )
    user_id = (await session.execute(stmt)).scalar_one()
    # Перечитываем через ORM: объект из RETURNING не привязан к сессии,
    # и изменения на нём (например, снятие бана) не сохранились бы.
    user = await session.get(User, user_id)
    assert user is not None
    return user


async def get_user_by_tg(session: AsyncSession, tg_id: int) -> User | None:
    return (
        await session.execute(select(User).where(User.tg_id == tg_id))
    ).scalar_one_or_none()


async def set_banned(session: AsyncSession, tg_id: int, banned: bool) -> None:
    await session.execute(
        update(User).where(User.tg_id == tg_id).values(is_banned=banned)
    )


# ── Подписки ────────────────────────────────────────────────────────────────


async def latest_subscription(
    session: AsyncSession, user_id: int
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def subscription_by_token(
    session: AsyncSession, token: str
) -> Subscription | None:
    stmt = select(Subscription).where(Subscription.sub_token == token)
    return (await session.execute(stmt)).scalar_one_or_none()


async def subscription_with_user(
    session: AsyncSession, sub_id: int
) -> Subscription | None:
    stmt = (
        select(Subscription)
        .options(joinedload(Subscription.user))
        .where(Subscription.id == sub_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# ── Ноды и инбаунды ─────────────────────────────────────────────────────────


async def all_nodes(session: AsyncSession) -> list[Node]:
    stmt = (
        select(Node)
        .options(joinedload(Node.inbounds))
        .order_by(Node.sort_order, Node.id)
    )
    return list((await session.execute(stmt)).unique().scalars())


async def node_by_id(session: AsyncSession, node_id: int) -> Node | None:
    return (
        await session.execute(select(Node).where(Node.id == node_id))
    ).scalar_one_or_none()


async def node_by_panel_url(
    session: AsyncSession, panel_base_url: str
) -> Node | None:
    return (
        await session.execute(
            select(Node).where(Node.panel_base_url == panel_base_url)
        )
    ).scalar_one_or_none()


async def active_inbound_ids(session: AsyncSession) -> list[int]:
    """Инбаунды, которые сейчас должны быть у каждой активной подписки."""
    stmt = (
        select(Inbound.id)
        .join(Node, Node.id == Inbound.node_id)
        .where(Inbound.is_active.is_(True), Node.is_active.is_(True))
    )
    return list((await session.execute(stmt)).scalars())


async def upsert_inbound(
    session: AsyncSession,
    node_id: int,
    panel_inbound_id: int,
    remark: str,
    protocol: str,
    port: int,
    stream_meta: dict,
) -> Inbound:
    await session.execute(
        insert(Inbound)
        .values(
            node_id=node_id,
            panel_inbound_id=panel_inbound_id,
            remark=remark,
            protocol=protocol,
            port=port,
            stream_meta=stream_meta,
            synced_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=[Inbound.node_id, Inbound.panel_inbound_id],
            set_={
                "remark": remark,
                "protocol": protocol,
                "port": port,
                "stream_meta": stream_meta,
                "synced_at": utcnow(),
            },
        )
    )
    # То же правило, что в get_or_create_user: RETURNING не даёт ORM-объект,
    # который вызывающий код потом меняет (is_active по итогам разбора).
    inbound = (
        await session.execute(
            select(Inbound).where(
                Inbound.node_id == node_id,
                Inbound.panel_inbound_id == panel_inbound_id,
            )
        )
    ).scalar_one()
    return inbound


# ── Клиенты подписок ────────────────────────────────────────────────────────


def _renderable() -> Select:
    """Клиенты, которые реально можно отдать в подписке."""
    return (
        select(SubscriptionClient)
        .options(
            joinedload(SubscriptionClient.inbound).joinedload(Inbound.node)
        )
        .join(Inbound, Inbound.id == SubscriptionClient.inbound_id)
        .join(Node, Node.id == Inbound.node_id)
        .where(
            SubscriptionClient.state == ClientState.ACTIVE.value,
            Inbound.is_active.is_(True),
            Node.is_active.is_(True),
        )
        .order_by(Node.sort_order, Node.id, Inbound.id)
    )


async def clients_for_render(
    session: AsyncSession, sub_id: int
) -> list[SubscriptionClient]:
    stmt = _renderable().where(SubscriptionClient.subscription_id == sub_id)
    return list((await session.execute(stmt)).unique().scalars())


async def clients_of_subscription(
    session: AsyncSession, sub_id: int
) -> list[SubscriptionClient]:
    stmt = (
        select(SubscriptionClient)
        .options(joinedload(SubscriptionClient.inbound).joinedload(Inbound.node))
        .where(SubscriptionClient.subscription_id == sub_id)
    )
    return list((await session.execute(stmt)).unique().scalars())


async def enqueue_client(
    session: AsyncSession, sub_id: int, inbound_id: int, remote_email: str
) -> None:
    """Поставить клиента в очередь на создание. Идемпотентно."""
    stmt = (
        insert(SubscriptionClient)
        .values(
            subscription_id=sub_id,
            inbound_id=inbound_id,
            remote_email=remote_email,
            state=ClientState.PENDING.value,
        )
        .on_conflict_do_nothing(
            index_elements=[
                SubscriptionClient.subscription_id,
                SubscriptionClient.inbound_id,
            ]
        )
    )
    await session.execute(stmt)


async def mark_clients(
    session: AsyncSession, sub_id: int, state: ClientState
) -> None:
    """Перевести все живые клиенты подписки в целевое состояние."""
    await session.execute(
        update(SubscriptionClient)
        .where(
            SubscriptionClient.subscription_id == sub_id,
            SubscriptionClient.state.notin_(
                [ClientState.REMOVED.value, ClientState.FAILED.value]
            ),
        )
        .values(state=state.value, attempts=0, next_attempt_at=None)
    )


# Состояния, требующие похода в панель.
WORK_STATES = (
    ClientState.PENDING.value,
    ClientState.DISABLING.value,
    ClientState.REMOVING.value,
)


async def take_work_batch(
    session: AsyncSession, limit: int = 50
) -> list[SubscriptionClient]:
    """Забрать порцию клиентов, которых надо привести в порядок на панели.

    Две фазы намеренно: блокировку берём отдельным плоским запросом, потому
    что FOR UPDATE вместе с outer join (его добавляет joinedload) Postgres
    не принимает. SKIP LOCKED — чтобы два экземпляра бота не дрались за
    одну строку.
    """
    now = utcnow()
    picker = (
        select(SubscriptionClient.id)
        .where(
            SubscriptionClient.state.in_(WORK_STATES),
            (SubscriptionClient.next_attempt_at.is_(None))
            | (SubscriptionClient.next_attempt_at <= now),
        )
        .order_by(SubscriptionClient.id)
        .limit(limit)
    )
    if not IS_SQLITE:
        # В SQLite блокировок строк нет вообще, но там и один процесс.
        picker = picker.with_for_update(skip_locked=True)

    ids = list((await session.execute(picker)).scalars())
    if not ids:
        return []

    stmt = (
        select(SubscriptionClient)
        .options(
            joinedload(SubscriptionClient.inbound).joinedload(Inbound.node),
            joinedload(SubscriptionClient.subscription),
        )
        .where(SubscriptionClient.id.in_(ids))
        .order_by(SubscriptionClient.id)
    )
    return list((await session.execute(stmt)).unique().scalars())


async def provisioning_errors(
    session: AsyncSession, limit: int = 3
) -> list[SubscriptionClient]:
    stmt = (
        select(SubscriptionClient)
        .where(
            SubscriptionClient.state.in_(WORK_STATES + (ClientState.FAILED.value,)),
            SubscriptionClient.last_error.is_not(None),
        )
        .order_by(SubscriptionClient.updated_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars())


async def record_failure(
    session: AsyncSession, client: SubscriptionClient, error: str
) -> None:
    """Отложить повтор с экспоненциальным backoff; после 8 попыток — failed."""
    client.attempts += 1
    client.last_error = error[:500]
    if client.attempts >= 8:
        client.state = ClientState.FAILED.value
        client.next_attempt_at = None
    else:
        delay = min(2 ** client.attempts, 900)  # до 15 минут
        client.next_attempt_at = utcnow() + timedelta(seconds=delay)


# ── Платежи ─────────────────────────────────────────────────────────────────


async def register_payment(
    session: AsyncSession,
    user_id: int,
    provider: str,
    provider_payment_id: str,
    plan_code: str,
    months: int,
    amount: int,
    currency: str,
) -> int | None:
    """Зафиксировать платёж. None — если этот платёж уже был обработан.

    Единственная защита от двойного зачисления: уникальный
    provider_payment_id. Повторный webhook просто не вставит строку,
    вернётся None, и начисления не произойдёт.
    """
    stmt = (
        insert(Payment)
        .values(
            user_id=user_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            plan_code=plan_code,
            months=months,
            amount=amount,
            currency=currency,
            status=PaymentStatus.PAID.value,
        )
        .on_conflict_do_nothing(index_elements=[Payment.provider_payment_id])
        .returning(Payment.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def attach_payment_subscription(
    session: AsyncSession, payment_id: int, sub_id: int
) -> None:
    await session.execute(
        update(Payment).where(Payment.id == payment_id).values(subscription_id=sub_id)
    )


# ── Статистика ──────────────────────────────────────────────────────────────


async def stats(session: AsyncSession) -> dict:
    now = utcnow()
    total_users = (await session.execute(select(func.count(User.id)))).scalar_one()
    active = (
        await session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )
    ).scalar_one()
    trials = (
        await session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.is_trial.is_(True),
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at > now,
            )
        )
    ).scalar_one()
    expiring = (
        await session.execute(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubStatus.ACTIVE.value,
                Subscription.expires_at.between(now, now + timedelta(days=3)),
            )
        )
    ).scalar_one()
    revenue_30d = (
        await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0)).where(
                Payment.status == PaymentStatus.PAID.value,
                Payment.created_at >= now - timedelta(days=30),
            )
        )
    ).scalar_one()
    stuck = (
        await session.execute(
            select(func.count(SubscriptionClient.id)).where(
                SubscriptionClient.state == ClientState.FAILED.value
            )
        )
    ).scalar_one()
    return {
        "total_users": total_users,
        "active": active,
        "trials": trials,
        "expiring_3d": expiring,
        "revenue_30d": revenue_30d,
        "stuck": stuck,
    }


async def all_user_tg_ids(session: AsyncSession) -> list[int]:
    stmt = select(User.tg_id).where(User.is_banned.is_(False))
    return list((await session.execute(stmt)).scalars())
