"""HTTP-сервер подписок.

Отдаёт ровно две вещи: подписку по токену и healthz. Панели в момент
запроса не опрашиваются — ответ строится из БД, поэтому лежащая панель
не мешает клиентам обновлять конфиг.
"""

from __future__ import annotations

import base64
import logging
import time
from collections import defaultdict, deque

from aiohttp import web

from app.config import settings
from app.db import repo
from app.db.base import session_factory
from app.services.subscription import is_live
from app.web.render import render_body, render_links, userinfo_header

log = logging.getLogger(__name__)

# Клиенты обновляют подписку раз в несколько часов. Всё, что чаще, —
# либо кривой клиент, либо перебор токенов.
RATE_LIMIT = 30
RATE_WINDOW = 300.0

_hits: dict[str, deque[float]] = defaultdict(deque)


def _rate_limited(key: str) -> bool:
    now = time.monotonic()
    window = _hits[key]
    while window and now - window[0] > RATE_WINDOW:
        window.popleft()
    if len(window) >= RATE_LIMIT:
        return True
    window.append(now)
    return False


def _client_ip(request: web.Request) -> str:
    # Заголовок клиента контролирует только доверенный reverse proxy.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.remote or "?"


async def handle_subscription(request: web.Request) -> web.Response:
    token = request.match_info["token"]

    if _rate_limited(_client_ip(request)):
        return web.Response(status=429, text="", headers={"Retry-After": "60"})

    async with session_factory() as session:
        device = await repo.device_by_token(session, token)
        sub = device.subscription if device else None
        # Одинаковый 404 для несуществующего и погашенного токена: перебор
        # не должен различать «нет такого» и «есть, но неактивен».
        if sub is None or not is_live(sub):
            return web.Response(status=404, text="")

        clients = await repo.clients_for_render(session, sub.id, device.id)
        links = render_links(clients, sub)

    headers = {
        "Subscription-Userinfo": userinfo_header(sub),
        "Profile-Update-Interval": str(settings.sub_update_interval_hours),
        "Profile-Title": "base64:"
        + base64.b64encode(settings.sub_title.encode()).decode(),
        "Cache-Control": "no-store",
    }
    if settings.support_url:
        headers["Support-Url"] = settings.support_url

    return web.Response(
        text=render_body(links),
        content_type="text/plain",
        headers=headers,
    )


async def handle_health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/s/{token}", handle_subscription)
    app.router.add_get("/healthz", handle_health)
    return app


async def start_web() -> web.AppRunner:
    runner = web.AppRunner(build_app(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", settings.sub_port)
    await site.start()
    log.info("subscription-endpoint слушает :%s", settings.sub_port)
    return runner
