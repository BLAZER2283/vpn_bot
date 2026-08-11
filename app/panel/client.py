"""Клиент API панели 3x-ui.

Отличия от разового скрипта add_client.py:
  * не перезаписывает инбаунд целиком (`update/{id}`) — это гонка, при двух
    одновременных выдачах один клиент затирает другого. Используем точечные
    addClient / updateClient;
  * после записи всегда перечитывает клиента. У 3x-ui есть баг: addClient
    отвечает пустым телом, клиент при этом не создан. Без верификации мы бы
    считали такую подписку выданной;
  * при протухшей сессии сам логинится заново и повторяет запрос.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.panel.errors import PanelAuthError, PanelRejected, PanelUnavailable

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass(slots=True)
class PanelClientSpec:
    """Клиент так, как его понимает панель."""

    uuid: str
    email: str
    sub_id: str
    flow: str = ""
    limit_ip: int = 0
    total_gb: int = 0
    expiry_ms: int = 0
    enable: bool = True
    tg_id: str = ""

    def to_panel(self) -> dict[str, Any]:
        return {
            "id": self.uuid,
            "email": self.email,
            "subId": self.sub_id,
            "flow": self.flow,
            "limitIp": self.limit_ip,
            "totalGB": self.total_gb,
            "expiryTime": self.expiry_ms,
            "enable": self.enable,
            "tgId": self.tg_id,
            "reset": 0,
        }


class ThreeXUIClient:
    """Одна сессия на одну панель. Не потокобезопасен между процессами."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        verify_tls: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._http = httpx.AsyncClient(
            verify=verify_tls, timeout=TIMEOUT, follow_redirects=False
        )
        self._lock = asyncio.Lock()
        self._logged_in = False
        self._csrf = ""

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── транспорт ───────────────────────────────────────────────────────────

    async def _fetch_csrf(self) -> str:
        """Снять CSRF-токен со стартовой страницы панели.

        Свежие сборки 3x-ui без него отвечают на /login 403. Именно так это
        делал рабочий add_client.py — шаг обязательный, не оптимизация.
        """
        try:
            # Стартовая страница обычно редиректит на /login — токен лежит
            # уже там, поэтому переход разрешаем точечно.
            resp = await self._http.get(f"{self._base}/", follow_redirects=True)
        except httpx.HTTPError as exc:
            raise PanelUnavailable(f"csrf: {exc}") from exc

        match = re.search(
            r'csrf-token"\s+content="([^"]+)"', resp.text
        ) or re.search(r'name="csrf"\s+value="([^"]+)"', resp.text)
        return match.group(1) if match else ""

    async def login(self) -> None:
        async with self._lock:
            # Токен и сессионная кука приезжают с этой же страницы.
            self._csrf = await self._fetch_csrf()

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
            }
            if self._csrf:
                headers["X-CSRF-Token"] = self._csrf

            try:
                resp = await self._http.post(
                    f"{self._base}/login",
                    data={"username": self._username, "password": self._password},
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                raise PanelUnavailable(f"login: {exc}") from exc

            if resp.status_code == 403:
                raise PanelRejected(
                    "login: 403 — панель отклонила запрос. Проверь, что адрес "
                    "указан вместе с секретным путём, и что IP не заблокирован"
                )
            if resp.status_code == 404:
                raise PanelRejected(
                    "login: 404 — по этому адресу панели нет. Нужен базовый URL "
                    "вида https://IP:ПОРТ/секретный_путь"
                )
            if resp.status_code != 200:
                raise PanelUnavailable(f"login: HTTP {resp.status_code}")

            try:
                body = resp.json()
            except ValueError:
                raise PanelUnavailable("login: не JSON в ответе") from None
            if not body.get("success"):
                raise PanelRejected(f"login: {body.get('msg', 'отказ')}")

            self._logged_in = True

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        _retry: bool = True,
    ) -> Any:
        if not self._logged_in:
            await self.login()

        url = f"{self._base}{path}"
        headers = {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self._csrf:
            headers["X-CSRF-Token"] = self._csrf

        try:
            resp = await self._http.request(
                method,
                url,
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise PanelUnavailable(f"{path}: {exc}") from exc

        # Панель на протухшей сессии редиректит на /login или отдаёт HTML.
        looks_like_login = (
            resp.status_code in (301, 302, 307, 401, 403)
            or "text/html" in resp.headers.get("content-type", "")
        )
        if looks_like_login:
            self._logged_in = False
            if _retry:
                await self.login()
                return await self._call(
                    method, path, json_body=json_body, _retry=False
                )
            raise PanelAuthError(f"{path}: сессия не поднялась")

        if resp.status_code >= 500:
            raise PanelUnavailable(f"{path}: HTTP {resp.status_code}")

        if not resp.content:
            # Известное поведение 3x-ui на addClient: пустое тело.
            # Считаем неопределённостью, а не успехом.
            raise PanelUnavailable(f"{path}: пустой ответ")

        try:
            body = resp.json()
        except ValueError:
            raise PanelUnavailable(f"{path}: не JSON в ответе") from None

        if not body.get("success"):
            raise PanelRejected(f"{path}: {body.get('msg', 'отказ без причины')}")
        return body.get("obj")

    # ── инбаунды ────────────────────────────────────────────────────────────

    async def list_inbounds(self) -> list[dict]:
        obj = await self._call("GET", "/panel/api/inbounds/list")
        return list(obj or [])

    async def get_inbound(self, inbound_id: int) -> dict:
        return await self._call("GET", f"/panel/api/inbounds/get/{inbound_id}")

    # ── клиенты ─────────────────────────────────────────────────────────────

    @staticmethod
    def _settings(spec: PanelClientSpec) -> dict:
        # settings — именно JSON-СТРОКА. Объект панель не разбирает и отвечает
        # "Fail: unexpected end of JSON input".
        return {"clients": [spec.to_panel()]}

    async def add_client(self, inbound_id: int, spec: PanelClientSpec) -> None:
        await self._call(
            "POST",
            "/panel/api/inbounds/addClient",
            json_body={
                "id": inbound_id,
                "settings": json.dumps(self._settings(spec)),
            },
        )

    async def update_client(
        self, inbound_id: int, spec: PanelClientSpec, target_uuid: str | None = None
    ) -> None:
        """Обновление шлёт ПОЛНЫЙ объект: пропущенные поля панель обнулит.

        target_uuid — какого клиента заменяем. Совпадает со spec.uuid всегда,
        кроме ротации ключа: там в URL нужен старый UUID, а новый едет в теле.
        """
        await self._call(
            "POST",
            f"/panel/api/inbounds/updateClient/{target_uuid or spec.uuid}",
            json_body={
                "id": inbound_id,
                "settings": json.dumps(self._settings(spec)),
            },
        )

    async def delete_client(self, inbound_id: int, uuid: str) -> None:
        await self._call(
            "POST", f"/panel/api/inbounds/{inbound_id}/delClient/{uuid}"
        )

    async def get_client_traffics(self, email: str) -> dict | None:
        try:
            return await self._call(
                "GET", f"/panel/api/inbounds/getClientTraffics/{email}"
            )
        except PanelRejected:
            return None

    async def client_exists(self, inbound_id: int, email: str) -> bool:
        """Есть ли клиент в панели на самом деле.

        Сначала дёшево — getClientTraffics. Для только что созданного клиента
        он у части сборок отдаёт null, поэтому подстраховываемся чтением
        самого инбаунда.
        """
        traffic = await self.get_client_traffics(email)
        if traffic:
            return True

        inbound = await self.get_inbound(inbound_id)
        raw = inbound.get("settings")
        settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
        return any(c.get("email") == email for c in settings.get("clients", []))

    async def upsert_client(
        self,
        inbound_id: int,
        spec: PanelClientSpec,
        known_uuid: str | None = None,
    ) -> None:
        """Создать или обновить клиента и убедиться, что он действительно есть.

        known_uuid — UUID, под которым клиент уже лежит на панели (если лежит).
        Верификация после записи обязательна: без неё баг с пустым ответом
        addClient даёт «успешно выданную» подписку, которая не подключается.
        """
        try:
            await self.add_client(inbound_id, spec)
        except PanelRejected as exc:
            if not exc.is_duplicate:
                raise
            await self.update_client(inbound_id, spec, target_uuid=known_uuid)

        if not await self.client_exists(inbound_id, spec.email):
            raise PanelUnavailable(
                f"клиент {spec.email} не найден после записи — повторим"
            )

    async def ping(self) -> bool:
        try:
            await self.list_inbounds()
            return True
        except Exception:  # noqa: BLE001 — health-проверка не должна падать
            return False
