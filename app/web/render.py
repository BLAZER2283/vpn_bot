"""Сборка тела подписки.

Именно этот модуль делает «одна ссылка = все серверы»: собственный
sub-эндпоинт агрегирует ноды, чего встроенный в 3x-ui сделать не может —
он знает только про свою панель.
"""

from __future__ import annotations

import base64

from app.db.models import Subscription, SubscriptionClient
from app.panel.links import build_vless_link


def render_links(
    clients: list[SubscriptionClient], sub: Subscription
) -> list[str]:
    links: list[str] = []
    for client in clients:
        inbound = client.inbound
        node = inbound.node
        label = f"{node.flag} {node.name}".strip()
        if inbound.remark and len(clients) > 1:
            # Когда у ноды несколько инбаундов, надо различать их в клиенте.
            same_node = sum(1 for c in clients if c.inbound.node_id == node.id)
            if same_node > 1:
                label = f"{label} · {inbound.remark}"

        links.append(
            build_vless_link(
                host=node.public_host,
                port=inbound.port,
                uuid=client.remote_uuid or sub.client_uuid,
                stream_meta=inbound.stream_meta,
                label=label,
            )
        )
    return links


def render_body(links: list[str]) -> str:
    """Стандартный формат: ссылки через \\n, всё вместе в base64."""
    return base64.b64encode("\n".join(links).encode()).decode()


def userinfo_header(sub: Subscription) -> str:
    """Заголовок, из которого клиент рисует срок и остаток трафика.

    Трафик безлимитный, поэтому нули: клиенты трактуют total=0 как «без
    ограничений» и просто не показывают полосу.
    """
    expire = int(sub.expires_at.timestamp())
    return f"upload=0; download=0; total=0; expire={expire}"
