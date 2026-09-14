"""Локальная проверка сквозного пути. Без Telegram, без панели, без Docker.

    python -m scripts.localtest

Что делает: поднимает SQLite в памяти, заводит фейковую ноду с двумя
инбаундами (REALITY + WSS), выдаёт триал, «создаёт» клиентов на подставной
панели, поднимает subscription-endpoint и забирает подписку HTTP-запросом.
Потом проверяет продление, истечение и добавление второй ноды.

Это то, что нельзя проверить руками, не имея бота и сервера под рукой:
корректность base64-тела, заголовков, и главное — что добавление ноды
не меняет ссылку.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys

# Настройки читаются при импорте app.config, поэтому задаём их до импортов.
os.environ.setdefault("BOT_TOKEN", "0:localtest")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./localtest.db")
os.environ.setdefault("SUB_PORT", "8099")
os.environ.setdefault("SUB_BASE_URL", "http://127.0.0.1:8099")
os.environ.setdefault("ADMIN_IDS", "1")

import aiohttp  # noqa: E402

from app.config import PLANS_BY_CODE  # noqa: E402
from app.db import repo  # noqa: E402
from app.db.base import create_all, engine, session_factory  # noqa: E402
from app.db.models import ClientState, Inbound, Node  # noqa: E402
from app.db.repo import utcnow  # noqa: E402
from app.panel import pool  # noqa: E402
from app.payments import service as pay_service  # noqa: E402
from app.services import nodes as node_service  # noqa: E402
from app.services import provisioning  # noqa: E402
from app.services import subscription as sub_service  # noqa: E402
from app.web.server import start_web  # noqa: E402

failures: list[str] = []


def check(ok: bool, message: str) -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {message}")
    if not ok:
        failures.append(message)


# ── Подставная панель ───────────────────────────────────────────────────────


class FakePanel:
    """Ведёт себя как ThreeXUIClient, но держит клиентов в памяти.

    Специально повторяет неприятную особенность настоящей панели: клиентов
    хранит по email, а не по UUID.
    """

    def __init__(self, host: str) -> None:
        self.host = host
        self.clients: dict[tuple[int, str], dict] = {}
        self.fail_next = False

    async def list_inbounds(self) -> list[dict]:
        import json

        return [
            {
                "id": 5,
                "port": 443,
                "protocol": "vless",
                "remark": "reality",
                "enable": True,
                "streamSettings": json.dumps({
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "serverNames": ["www.cloudflare.com"],
                        "shortIds": ["17"],
                        "settings": {
                            "publicKey": "TESTPBK",
                            "fingerprint": "chrome",
                            "spiderX": "/",
                        },
                    },
                }),
            },
            {
                "id": 6,
                "port": 8443,
                "protocol": "vless",
                "remark": "wss",
                "enable": True,
                "streamSettings": json.dumps({
                    "network": "ws",
                    "security": "tls",
                    "tlsSettings": {
                        "serverName": self.host,
                        "alpn": ["http/1.1"],
                        "settings": {"fingerprint": "chrome"},
                    },
                    "wsSettings": {
                        "path": "/vless",
                        "headers": {"Host": self.host},
                    },
                }),
            },
        ]

    async def upsert_client(self, inbound_id, spec, known_uuid=None) -> None:
        if self.fail_next:
            from app.panel.errors import PanelUnavailable

            self.fail_next = False
            raise PanelUnavailable("тестовый сбой панели")
        self.clients[(inbound_id, spec.email)] = {
            "uuid": spec.uuid,
            "enable": spec.enable,
            "limitIp": spec.limit_ip,
            "subId": spec.sub_id,
            "flow": spec.flow,
        }

    async def delete_client(self, inbound_id, uuid, email=None) -> None:
        for key, value in list(self.clients.items()):
            if key[0] == inbound_id and value["uuid"] == uuid:
                del self.clients[key]

    async def aclose(self) -> None:
        pass


panels: dict[int, FakePanel] = {}


async def fake_get_client(node: Node) -> FakePanel:
    panel = panels.get(node.id)
    if panel is None:
        panel = FakePanel(node.public_host)
        panels[node.id] = panel
    return panel


# ── Сценарий ────────────────────────────────────────────────────────────────


async def fetch(url: str) -> tuple[int, str, dict]:
    async with aiohttp.ClientSession() as http:
        async with http.get(url) as resp:
            return resp.status, await resp.text(), dict(resp.headers)


async def main() -> int:
    # Подменяем фабрику клиентов панели на фейковую.
    pool.get_client = fake_get_client  # type: ignore[assignment]

    db_path = "./localtest.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    await create_all()

    runner = await start_web()
    try:
        print("\n1. Первый сервер")
        async with session_factory() as session:
            node, inbounds, tasks = await node_service.add_node(
                session,
                name="Тест-1",
                panel_base_url="https://10.0.0.1:45563/secret",
                panel_user="admin",
                panel_pass="pass",
                public_host="10.0.0.1",
                flag="🇳🇱",
            )
            await session.commit()
            node1_id = node.id
        check(inbounds == 2, f"найдено инбаундов: {inbounds} (ожидалось 2)")
        check(tasks == 0, "активных подписок ещё нет, задач 0")

        print("\n2. Пользователь и триал")
        async with session_factory() as session:
            user = await repo.get_or_create_user(session, 555, "tester")
            sub = await sub_service.start_trial(session, user)
            await node_service.assign_node_to_subscription(session, sub, node)
            await session.commit()
            sub_id, token = sub.id, sub.sub_token
        check(sub is not None, "триал выдан")

        async with session_factory() as session:
            ready, total = await provisioning.ready_count(session, sub_id)
        check(total == 2 and ready == 0, f"в очереди 2 инбаунда, готово {ready}")

        print("\n3. Подписка до провиженинга")
        status, body, _ = await fetch(f"http://127.0.0.1:8099/s/{token}")
        check(status == 200, "ссылка уже валидна (деньги не ждут панель)")
        check(body == "", "но пока пустая — клиенты не созданы")

        print("\n4. Провиженинг")
        processed = await provisioning.drain()
        check(processed == 2, f"обработано записей: {processed}")

        status, body, headers = await fetch(f"http://127.0.0.1:8099/s/{token}")
        decoded = base64.b64decode(body).decode()
        links = decoded.splitlines()
        check(len(links) == 2, f"в подписке ссылок: {len(links)}")
        check(all(l.startswith("vless://") for l in links), "все ссылки vless://")
        check("pbk=TESTPBK" in decoded, "REALITY отдан с pbk (не pk)")
        check("flow=xtls-rprx-vision" in links[0], "flow есть на TCP-инбаунде")
        check("flow=" not in links[1], "flow отсутствует на ws-инбаунде")
        check(
            "Subscription-Userinfo" in headers
            and "expire=" in headers["Subscription-Userinfo"],
            "заголовок Subscription-Userinfo на месте",
        )
        check(
            headers.get("Profile-Update-Interval") == "12",
            "Profile-Update-Interval выставлен",
        )
        check(
            panels[node1_id].clients[(5, f"sub{sub_id}")]["limitIp"] == 6,
            "лимит устройств доехал до панели",
        )
        check(
            panels[node1_id].clients[(5, f"sub{sub_id}")]["subId"]
            == f"s{sub_id}",
            "в панель ушёл s<id>, а не секретный токен",
        )
        check(token not in str(panels[node1_id].clients), "токен не утёк в панель")

        print("\n5. Второй сервер — ключ НЕ перевыдаётся")
        async with session_factory() as session:
            node2, inbounds2, tasks2 = await node_service.add_node(
                session,
                name="Тест-2",
                panel_base_url="https://10.0.0.2:45563/secret",
                panel_user="admin",
                panel_pass="pass",
                public_host="10.0.0.2",
                flag="🇩🇪",
            )
            tasks2 = await node_service.assign_node_to_subscription(
                session, sub, node2
            )
            await session.commit()
        check(tasks2 == 2, f"задач на выдачу существующей подписке: {tasks2}")

        await provisioning.drain()
        status, body, _ = await fetch(f"http://127.0.0.1:8099/s/{token}")
        links = base64.b64decode(body).decode().splitlines()
        check(len(links) == 4, f"стало ссылок: {len(links)} (было 2)")
        check(
            any("@10.0.0.2:" in l for l in links),
            "новый сервер появился в подписке",
        )

        async with session_factory() as session:
            fresh = await repo.subscription_by_token(session, token)
        check(fresh is not None, "токен тот же — перевыдачи не потребовалось")

        print("\n6. Частичный отказ панели")
        panels[node1_id].fail_next = True
        async with session_factory() as session:
            user2 = await repo.get_or_create_user(session, 777, "second")
            sub2 = await sub_service.create_subscription(
                session, user2, days=1
            )
            await session.commit()
            sub2_id, token2 = sub2.id, sub2.sub_token

        await provisioning.drain(max_batches=1)
        status, body, _ = await fetch(f"http://127.0.0.1:8099/s/{token2}")
        links2 = base64.b64decode(body).decode().splitlines()
        check(status == 200, "подписка работает несмотря на сбой панели")
        check(
            0 < len(links2) < 4,
            f"выдано частично: {len(links2)} из 4 — остальное в ретраях",
        )

        async with session_factory() as session:
            ready, total = await provisioning.ready_count(session, sub2_id)
        check(ready < total, f"бот знает про недовыдачу: {ready}/{total}")

        print("\n7. Оплата и продление")
        plan = PLANS_BY_CODE["m1"]
        async with session_factory() as session:
            user = await repo.get_user_by_tg(session, 555)
            before = (await repo.latest_subscription(session, user.id)).expires_at
            sub3, created = await pay_service.credit(
                session,
                user=user,
                plan=plan,
                provider="stars",
                provider_payment_id="charge-1",
            )
            await session.commit()
            after, token3 = sub3.expires_at, sub3.sub_token
        check(not created, "продлена существующая подписка, а не создана новая")
        check(after > before, f"срок вырос: {before:%d.%m} → {after:%d.%m}")
        check(token3 == token, "ссылка после продления НЕ изменилась")

        print("\n8. Повторный платёж (двойной вебхук)")
        async with session_factory() as session:
            user = await repo.get_user_by_tg(session, 555)
            try:
                await pay_service.credit(
                    session,
                    user=user,
                    plan=plan,
                    provider="stars",
                    provider_payment_id="charge-1",
                )
                check(False, "повторный платёж должен был отклониться")
            except pay_service.AlreadyProcessed:
                check(True, "повторный платёж отклонён, срок не удвоился")

        print("\n9. Истечение")
        async with session_factory() as session:
            sub_obj = await repo.subscription_by_token(session, token)
            sub_obj.expires_at = utcnow() - __import__("datetime").timedelta(days=1)
            await sub_service.expire_subscription(session, sub_obj)
            await session.commit()

        await provisioning.drain()
        status, _, _ = await fetch(f"http://127.0.0.1:8099/s/{token}")
        check(status == 404, "истёкшая подписка отдаёт 404")

        disabled = [
            c for c in panels[node1_id].clients.values() if not c["enable"]
        ]
        check(bool(disabled), "клиент на панели выключен, а не удалён")

        print("\n10. Продление после истечения")
        async with session_factory() as session:
            user = await repo.get_user_by_tg(session, 555)
            sub4, _ = await pay_service.credit(
                session,
                user=user,
                plan=plan,
                provider="stars",
                provider_payment_id="charge-2",
            )
            await session.commit()
            token4 = sub4.sub_token

        await provisioning.drain()
        status, body, _ = await fetch(f"http://127.0.0.1:8099/s/{token4}")
        check(token4 == token, "и снова та же ссылка")
        check(status == 200, "доступ вернулся")
        check(
            len(base64.b64decode(body).decode().splitlines()) == 4,
            "все 4 сервера снова в подписке",
        )

        print("\n11. Неизвестный токен")
        status, _, _ = await fetch("http://127.0.0.1:8099/s/nonexistent")
        check(status == 404, "неизвестный токен — 404 без подсказок")

        status, _, _ = await fetch("http://127.0.0.1:8099/healthz")
        check(status == 200, "healthz отвечает")

    finally:
        await runner.cleanup()
        await engine.dispose()

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("Все проверки пройдены. Сквозной путь работает.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
