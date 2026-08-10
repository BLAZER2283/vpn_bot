"""Переиспользование сессий панелей.

Логин в 3x-ui не бесплатный, а провиженинг ходит в одни и те же панели
десятки раз в минуту. Держим по одному клиенту на ноду.
"""

from __future__ import annotations

import asyncio

from app.db.models import Node
from app.panel.client import ThreeXUIClient

_clients: dict[int, ThreeXUIClient] = {}
_lock = asyncio.Lock()


async def get_client(node: Node) -> ThreeXUIClient:
    async with _lock:
        client = _clients.get(node.id)
        if client is None:
            client = ThreeXUIClient(
                node.panel_base_url, node.panel_user, node.panel_pass
            )
            _clients[node.id] = client
        return client


async def drop_client(node_id: int) -> None:
    """Сбросить сессию — например, после смены пароля панели."""
    async with _lock:
        client = _clients.pop(node_id, None)
    if client is not None:
        await client.aclose()


async def close_all() -> None:
    async with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        await client.aclose()
