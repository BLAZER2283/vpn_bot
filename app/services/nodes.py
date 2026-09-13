"""Управление нодами: регистрация, синхронизация инбаундов, backfill.

Backfill — это то, из-за чего вся схема так устроена. Добавили сервер →
всем активным подпискам поставили задачу создать на нём клиента → сервер
появился у всех, ничего не перевыдавая.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo
from app.db.models import Inbound, Node, SubStatus, Subscription
from app.db.repo import utcnow
from app.panel import pool
from app.panel.links import parse_inbound
from app.services.subscription import client_email

log = logging.getLogger(__name__)


async def sync_inbounds(session: AsyncSession, node: Node) -> list[Inbound]:
    """Подтянуть инбаунды ноды из панели и обновить параметры ссылок.

    Вызывается и при добавлении ноды, и по расписанию: если на сервере
    поменяли REALITY-ключи или путь WS, ссылки обновятся сами.
    """
    panel = await pool.get_client(node)
    raws = await panel.list_inbounds()

    result: list[Inbound] = []
    for raw in raws:
        if raw.get("protocol") != "vless":
            log.info(
                "нода %s: инбаунд %s протокола %s пропущен",
                node.name, raw.get("id"), raw.get("protocol"),
            )
            continue

        meta = parse_inbound(raw)
        inbound = await repo.upsert_inbound(
            session,
            node_id=node.id,
            panel_inbound_id=int(raw["id"]),
            remark=str(raw.get("remark") or ""),
            protocol=str(raw.get("protocol") or "vless"),
            port=int(raw["port"]),
            stream_meta=meta,
        )

        # Инбаунд, выключенный в панели или с битым REALITY, отдавать нельзя:
        # ссылка с пустым pbk молча не подключается, и человек решит,
        # что виноват сервис.
        healthy = bool(raw.get("enable", True)) and not meta["problems"]
        inbound.is_active = healthy
        if not healthy:
            log.warning(
                "нода %s инбаунд %s исключён: %s",
                node.name,
                raw.get("id"),
                "; ".join(meta["problems"]) or "выключен в панели",
            )
        result.append(inbound)

    return result


async def backfill_node(session: AsyncSession, node: Node) -> int:
    """Поставить всем живым подпискам задачу выдаться на инбаундах этой ноды."""
    inbound_ids = [
        i.id
        for i in (
            await session.execute(
                select(Inbound).where(
                    Inbound.node_id == node.id, Inbound.is_active.is_(True)
                )
            )
        ).scalars()
    ]
    if not inbound_ids:
        return 0

    subs = list(
        (
            await session.execute(
                select(Subscription).where(
                    Subscription.status == SubStatus.ACTIVE.value,
                    Subscription.expires_at > utcnow(),
                )
            )
        ).scalars()
    )

    for sub in subs:
        for inbound_id in inbound_ids:
            await repo.enqueue_client(
                session, sub.id, inbound_id, client_email(sub.id)
            )

    log.info(
        "backfill ноды %s: %d подписок × %d инбаундов",
        node.name, len(subs), len(inbound_ids),
    )
    return len(subs) * len(inbound_ids)


async def add_node(
    session: AsyncSession,
    *,
    name: str,
    panel_base_url: str,
    panel_user: str,
    panel_pass: str,
    public_host: str,
    flag: str = "",
) -> tuple[Node, int, int]:
    """Зарегистрировать ноду, синхронизировать инбаунды, раздать всем.

    Возвращает (нода, сколько инбаундов, сколько задач на выдачу).
    """
    normalized_url = panel_base_url.rstrip("/")
    node = await repo.node_by_panel_url(session, normalized_url)
    if node is None:
        node = Node(
            name=name,
            flag=flag,
            panel_base_url=normalized_url,
            panel_user=panel_user,
            panel_pass=panel_pass,
            public_host=public_host,
        )
        session.add(node)
        await session.flush()
    else:
        node.name = name
        node.flag = flag
        node.panel_user = panel_user
        node.panel_pass = panel_pass
        node.public_host = public_host
        node.is_active = True

    inbounds = await sync_inbounds(session, node)
    await session.flush()
    tasks = await backfill_node(session, node)
    return node, len(inbounds), tasks


async def set_node_active(
    session: AsyncSession, node: Node, active: bool
) -> None:
    """Включить/выключить ноду.

    Выключение мгновенно убирает её из выдачи подписок, но клиентов на панели
    не трогает: если это временная мера, включение вернёт всё как было.
    """
    node.is_active = active
