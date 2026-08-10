"""Диагностика подключения к Telegram.

    python -m scripts.netcheck

Отвечает на один вопрос: почему бот не достучался до api.telegram.org и
что именно чинить. Проверяет DNS, TCP, TLS и сам токен — по отдельности,
потому что лечатся они по-разному.
"""

from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys

HOST = "api.telegram.org"
PORT = 443


def check_dns() -> list[str]:
    print("1. DNS")
    try:
        infos = socket.getaddrinfo(HOST, PORT, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        print(f"   ПРОВАЛ: имя не разрешается ({exc})")
        print("   → DNS подменён или заблокирован. Поставь 1.1.1.1 / 8.8.8.8")
        return []

    ips = sorted({info[4][0] for info in infos})
    print(f"   ok: {', '.join(ips)}")

    # Подмена на локальный/нулевой адрес — типичный признак DNS-фильтрации.
    bad = [ip for ip in ips if ip.startswith(("127.", "0.")) or ip == "::1"]
    if bad:
        print(f"   ВНИМАНИЕ: подозрительный адрес {bad} — похоже на подмену DNS")
    return ips


def check_tcp(ips: list[str]) -> str | None:
    print("\n2. TCP :443")
    for ip in ips:
        sock = socket.socket(
            socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM
        )
        sock.settimeout(7)
        try:
            sock.connect((ip, PORT))
            print(f"   ok: {ip} принял соединение")
            return ip
        except (TimeoutError, socket.timeout):
            print(f"   таймаут: {ip}")
        except OSError as exc:
            print(f"   отказ: {ip} ({exc.strerror or exc})")
        finally:
            sock.close()

    print("   ПРОВАЛ: ни один адрес не отвечает")
    print("   → Соединение режется провайдером. Нужен прокси или VPN")
    suggest_local_proxy()
    return None


# Локальные прокси, которые поднимают популярные клиенты. Порт → чей он.
KNOWN_PROXIES: list[tuple[int, str, str]] = [
    (10809, "v2rayN", "http"),
    (10808, "v2rayN", "socks5"),
    (2334, "Hiddify", "http"),
    (12334, "Hiddify", "socks5"),
    (7890, "Clash", "http"),
    (7891, "Clash", "socks5"),
    (1080, "SOCKS-клиент", "socks5"),
    (20172, "NekoRay", "socks5"),
    (9150, "Tor Browser", "socks5"),
]


def suggest_local_proxy() -> None:
    """Найти уже запущенный на машине прокси и подсказать готовую строку."""
    print("\n   Ищу локальный прокси…")
    found: list[tuple[int, str, str]] = []
    for port, name, scheme in KNOWN_PROXIES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        try:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                found.append((port, name, scheme))
        finally:
            sock.close()

    if not found:
        print("   не найден — ни один известный клиент не слушает локально")
        print("\n   Что делать:")
        print("   1. Подключи Windows к своему VLESS через v2rayN или Hiddify")
        print("      (ссылку возьми в панели 3x-ui), потом запусти проверку снова")
        print("   2. Либо разверни бота на VPS — там Telegram доступен напрямую")
        return

    print("   найдено:")
    for port, name, scheme in found:
        print(f"     :{port} — похоже на {name}")

    port, name, scheme = found[0]
    print(f"\n   Пропиши в .env и запусти бота снова:")
    print(f"     TELEGRAM_PROXY={scheme}://127.0.0.1:{port}")
    if scheme == "socks5":
        print("   (для socks5 нужен пакет aiohttp-socks — он уже в requirements)")


def check_tls(ip: str) -> bool:
    print("\n3. TLS")
    ctx = ssl.create_default_context()
    sock = socket.socket(
        socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM
    )
    sock.settimeout(10)
    try:
        sock.connect((ip, PORT))
        with ctx.wrap_socket(sock, server_hostname=HOST) as tls:
            cert = tls.getpeercert()
            issuer = dict(x[0] for x in cert.get("issuer", ()))
            print(f"   ok: {tls.version()}, выдан {issuer.get('organizationName', '?')}")
            return True
    except ssl.SSLError as exc:
        print(f"   ПРОВАЛ: {exc}")
        print("   → Сертификат подменяют (DPI/антивирус) либо рвут handshake")
        return False
    except (TimeoutError, socket.timeout):
        # TCP прошёл, а TLS завис — классическая подпись DPI по SNI.
        print("   ПРОВАЛ: таймаут на рукопожатии")
        print("   → Похоже на DPI: TCP пускают, TLS по имени режут. Нужен VPN")
        return False
    except OSError as exc:
        print(f"   ПРОВАЛ: {exc}")
        return False
    finally:
        sock.close()


async def check_api() -> bool:
    print("\n4. Telegram API")
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        from pathlib import Path

        env = Path(__file__).resolve().parent.parent / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip()
                    break

    if not token or token.startswith("123456:"):
        print("   пропуск: BOT_TOKEN не задан")
        return False

    proxy = os.environ.get("TELEGRAM_PROXY", "")
    try:
        import aiohttp
    except ImportError:
        print("   пропуск: aiohttp не установлен")
        return False

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        # aiohttp сам понимает http-прокси через proxy=, а для socks5
        # нужен отдельный коннектор из aiohttp-socks.
        if proxy and proxy.startswith("socks"):
            from aiohttp_socks import ProxyConnector

            connector = ProxyConnector.from_url(proxy)
            async with aiohttp.ClientSession(
                timeout=timeout, connector=connector
            ) as http:
                async with http.get(f"https://{HOST}/bot{token}/getMe") as resp:
                    data = await resp.json()
        else:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.get(
                    f"https://{HOST}/bot{token}/getMe", proxy=proxy or None
                ) as resp:
                    data = await resp.json()

        if data.get("ok"):
            me = data["result"]
            print(f"   ok: бот @{me.get('username')} ({me.get('first_name')})")
            return True
        print(f"   ПРОВАЛ: {data.get('description')}")
        print("   → Токен неверен или отозван. Возьми новый у @BotFather")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"   ПРОВАЛ: {type(exc).__name__}: {exc}")
        print("   → Если задан прокси, вероятно, он не поднимает соединение")
        return False


async def main() -> int:
    print(f"Проверка доступа к {HOST}\n")
    proxy = os.environ.get("TELEGRAM_PROXY", "")
    if proxy:
        print(f"TELEGRAM_PROXY = {proxy}\n")

    ips = check_dns()
    if not ips:
        return 1

    ip = check_tcp(ips)
    if ip is None:
        return 1

    if not check_tls(ip):
        return 1

    ok = await check_api()

    print()
    if ok:
        print("Всё в порядке — бот должен запуститься.")
        return 0

    print("Сеть до Telegram работает, проблема в токене (см. пункт 4).")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
