"""Самопроверка сборки vless-ссылок. Без БД, сети и панели.

    python -m scripts.selfcheck

Проверяет разбор инбаунда и генерацию ссылки на зафиксированных примерах —
ровно тех местах, где старый add_client.py ошибался. Стоит прогнать после
любой правки app/panel/links.py.
"""

from __future__ import annotations

import json
import sys
from urllib.parse import parse_qs, urlparse

from app.panel.links import build_vless_link, parse_inbound

REALITY_INBOUND = {
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
                "publicKey": "pC-TWuetK8fDSNpMRMgOxrWaEn24hiaXBcdeTVPjm0I",
                "fingerprint": "chrome",
                "spiderX": "/",
            },
        },
    }),
}

# Тот же инбаунд, но publicKey лежит верхним уровнем — известное расхождение
# между сборками панели.
REALITY_LEGACY = {
    "id": 6,
    "port": 443,
    "protocol": "vless",
    "streamSettings": json.dumps({
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "serverNames": ["www.google.com"],
            "shortIds": ["ab"],
            "publicKey": "LEGACYKEY",
            "settings": {},
        },
    }),
}

WS_INBOUND = {
    "id": 7,
    "port": 8443,
    "protocol": "vless",
    "remark": "wss",
    "streamSettings": json.dumps({
        "network": "ws",
        "security": "tls",
        "tlsSettings": {
            "serverName": "vpn.example.com",
            "alpn": ["http/1.1"],
            "settings": {"fingerprint": "chrome"},
        },
        "wsSettings": {"path": "/vless", "headers": {"Host": "vpn.example.com"}},
    }),
}

BROKEN_REALITY = {
    "id": 8,
    "port": 443,
    "protocol": "vless",
    "streamSettings": json.dumps({
        "network": "tcp",
        "security": "reality",
        "realitySettings": {"serverNames": [], "shortIds": [], "settings": {}},
    }),
}

UUID = "983ecd09-6257-411d-a58c-fa7473ca3c0c"

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"  ok   {message}")
    else:
        print(f"  FAIL {message}")
        failures.append(message)


def query_of(link: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}


def main() -> int:
    print("REALITY:")
    meta = parse_inbound(REALITY_INBOUND)
    link = build_vless_link(
        host="1.2.3.4", port=443, uuid=UUID, stream_meta=meta, label="NL"
    )
    q = query_of(link)
    check(not meta["problems"], "проблем не найдено")
    check("pbk" in q, "параметр называется pbk, а не pk")
    check(q.get("sni") == "www.cloudflare.com", "sni взят из serverNames")
    check(q.get("sid") == "17", "sid взят из shortIds")
    check(q.get("flow") == "xtls-rprx-vision", "flow на TCP выставлен")
    check(link.startswith(f"vless://{UUID}@1.2.3.4:443?"), "хост и порт из ноды")
    check(link.endswith("#NL"), "метка на месте")

    print("\nREALITY (publicKey верхним уровнем):")
    meta = parse_inbound(REALITY_LEGACY)
    check(meta.get("pbk") == "LEGACYKEY", "publicKey найден по запасному пути")
    check(not meta["problems"], "инбаунд считается рабочим")

    print("\nWS + TLS:")
    meta = parse_inbound(WS_INBOUND)
    q = query_of(
        build_vless_link(
            host="vpn.example.com", port=8443, uuid=UUID,
            stream_meta=meta, label="NL wss",
        )
    )
    check("flow" not in q, "flow НЕ ставится на ws (иначе конфиг не работает)")
    check(q.get("type") == "ws", "type=ws")
    check(q.get("path") == "/vless", "path из wsSettings")
    check(q.get("host") == "vpn.example.com", "host из заголовков ws")
    check(q.get("security") == "tls", "security=tls")

    print("\nБитый REALITY:")
    meta = parse_inbound(BROKEN_REALITY)
    check(bool(meta["problems"]), "инбаунд помечен проблемным и не пойдёт в выдачу")

    print()
    if failures:
        print(f"ПРОВАЛЕНО проверок: {len(failures)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
