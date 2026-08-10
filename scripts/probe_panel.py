"""Разведка боевой панели. ТОЛЬКО ЧТЕНИЕ — ничего не создаёт и не меняет.

Запускать до первого деплоя, чтобы узнать реальные параметры инбаундов
вместо тех, что были захардкожены в add_client.py.

    python -m scripts.probe_panel https://IP:PORT/secret_path USER PASS

Что проверяем глазами в выводе:
  * какие panel_inbound_id существуют (в старом скрипте фигурировал 5);
  * реальные sni / sid / pbk — совпадают ли с хардкодом
    www.cloudflare.com / 17;
  * блок "проблемы" пуст ли;
  * ссылка, собранная для СУЩЕСТВУЮЩЕГО клиента, — вставь её в v2rayTun.
    Работает → генератор корректен, хардкод больше не нужен.
"""

from __future__ import annotations

import asyncio
import json
import sys

from app.panel.client import ThreeXUIClient
from app.panel.links import build_vless_link, parse_inbound


def _clients_of(raw: dict) -> list[dict]:
    settings = raw.get("settings")
    data = json.loads(settings) if isinstance(settings, str) else (settings or {})
    return data.get("clients", [])


async def main() -> None:
    if len(sys.argv) != 4:
        print(__doc__)
        raise SystemExit(2)

    base_url, user, password = sys.argv[1:4]
    host = base_url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]

    panel = ThreeXUIClient(base_url, user, password)
    try:
        await panel.login()
        print("login: ok\n")

        inbounds = await panel.list_inbounds()
        print(f"инбаундов: {len(inbounds)}\n")

        for raw in inbounds:
            meta = parse_inbound(raw)
            clients = _clients_of(raw)
            enabled = raw.get("enable")

            print("=" * 70)
            print(
                f"id={raw.get('id')}  port={raw.get('port')}  "
                f"protocol={raw.get('protocol')}  enable={enabled}"
            )
            print(f"remark={raw.get('remark')!r}  клиентов={len(clients)}")
            print(f"network={meta['network']}  security={meta['security']}")

            for key in ("sni", "pbk", "sid", "fp", "spx", "path", "host", "alpn", "flow"):
                if key in meta and meta[key] != "":
                    print(f"  {key} = {meta[key]}")

            if meta["problems"]:
                print("  ПРОБЛЕМЫ: " + "; ".join(meta["problems"]))

            if clients:
                sample = clients[0]
                link = build_vless_link(
                    host=host,
                    port=int(raw["port"]),
                    uuid=str(sample.get("id")),
                    stream_meta=meta,
                    label=f"probe-{raw.get('remark') or raw.get('id')}",
                )
                print(f"\n  клиент для примера: email={sample.get('email')!r} "
                      f"limitIp={sample.get('limitIp')} "
                      f"expiryTime={sample.get('expiryTime')} "
                      f"subId={sample.get('subId')!r}")
                print(f"  ссылка (проверь в клиенте):\n  {link}")
            print()

        print("=" * 70)
        print("Проверка API точечной записи (без записи):")
        try:
            await panel.get_client_traffics("__nonexistent_probe__")
            print("  getClientTraffics отвечает — точечные ручки доступны")
        except Exception as exc:  # noqa: BLE001
            print(f"  getClientTraffics: {exc}")
    finally:
        await panel.aclose()


if __name__ == "__main__":
    asyncio.run(main())
