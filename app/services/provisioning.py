"""Воркер провиженинга: приводит панели к тому состоянию, что записано в БД.

БД — источник истины. Строка subscription_clients говорит, каким клиент
должен быть; воркер добивается этого на панели и только после подтверждения
переводит строку в терминальное состояние.

Частичная выдача (2 ноды из 3) — нормальный режим, а не ошибка: подписка
отдаёт то, что уже готово, остальное доедет ретраями.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo
from app.db.base import session_factory
from app.db.models import ClientState, SubscriptionClient
from app.panel import pool
from app.panel.client import PanelClientSpec
from app.panel.errors import PanelRejected, PanelUnavailable

log = logging.getLogger(__name__)

BATCH = 50


def _spec(client: SubscriptionClient, *, enable: bool) -> PanelClientSpec:
    sub = client.subscription
    inbound = client.inbound

    # expiryTime на панели — вторая линия обороны на случай, если джоба
    # отключения не сработает. По умолчанию выключено: требует, чтобы часы
    # на нодах не разъезжались с часами бота.
    expiry_ms = 0
    if settings.panel_set_expiry and sub.expires_at is not None:
        expiry_ms = int(sub.expires_at.timestamp() * 1000)

    return PanelClientSpec(
        uuid=sub.client_uuid,
        email=client.remote_email,
        # В панель кладём безобидный идентификатор, а не sub_token:
        # секретный токен не должен расползаться по чужим серверам.
        sub_id=f"s{sub.id}",
        flow=inbound.stream_meta.get("flow", ""),
        limit_ip=sub.device_limit,
        total_gb=0,
        expiry_ms=expiry_ms,
        enable=enable,
        tg_id="",
    )


async def _apply(session: AsyncSession, client: SubscriptionClient) -> None:
    """Один шаг: применить желаемое состояние одной строки."""
    node = client.inbound.node
    panel = await pool.get_client(node)
    inbound_id = client.inbound.panel_inbound_id
    target = client.state

    if target == ClientState.PENDING.value:
        # После ротации ключа старого клиента надо снести: он остался бы
        # рабочим, а смысл ротации — именно убить утёкшую ссылку.
        stale = client.remote_uuid
        if stale and stale != client.subscription.client_uuid:
            try:
                await panel.delete_client(inbound_id, stale)
            except PanelRejected:
                pass  # уже нет — не беда

        await panel.upsert_client(
            inbound_id, _spec(client, enable=True), known_uuid=stale
        )
        client.remote_uuid = client.subscription.client_uuid
        client.state = ClientState.ACTIVE.value

    elif target == ClientState.DISABLING.value:
        await panel.upsert_client(
            inbound_id,
            _spec(client, enable=False),
            known_uuid=client.remote_uuid,
        )
        client.remote_uuid = client.subscription.client_uuid
        client.state = ClientState.DISABLED.value

    elif target == ClientState.REMOVING.value:
        uuid = client.remote_uuid or client.subscription.client_uuid
        try:
            await panel.delete_client(inbound_id, uuid)
        except PanelRejected as exc:
            # Нет клиента — цель достигнута.
            log.info("delete %s: %s", client.remote_email, exc)
        client.state = ClientState.REMOVED.value

    else:  # pragma: no cover — в выборку такие состояния не попадают
        return

    client.attempts = 0
    client.last_error = None
    client.next_attempt_at = None


async def run_once() -> int:
    """Обработать одну порцию. Возвращает число разобранных строк."""
    processed = 0
    async with session_factory() as session:
        clients = await repo.take_work_batch(session, BATCH)
        for client in clients:
            try:
                await _apply(session, client)
            except (PanelUnavailable, PanelRejected) as exc:
                await repo.record_failure(session, client, str(exc))
                log.warning(
                    "провиженинг %s на ноде %s: %s",
                    client.remote_email,
                    client.inbound.node.name,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — воркер не должен падать
                await repo.record_failure(session, client, repr(exc))
                log.exception("неожиданная ошибка провиженинга")
            processed += 1
        await session.commit()
    return processed


async def drain(max_batches: int = 20) -> int:
    """Разгребать до пустой очереди. Ограничение — чтобы не крутиться вечно."""
    total = 0
    for _ in range(max_batches):
        done = await run_once()
        total += done
        if done < BATCH:
            break
    return total


async def ready_count(session: AsyncSession, sub_id: int) -> tuple[int, int]:
    """Сколько серверов уже отдаётся и сколько ожидается всего."""
    clients = await repo.clients_of_subscription(session, sub_id)
    live = [
        c for c in clients
        if c.inbound.is_active and c.inbound.node.is_active
    ]
    ready = sum(1 for c in live if c.state == ClientState.ACTIVE.value)
    return ready, len(live)
