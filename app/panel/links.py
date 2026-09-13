"""Разбор инбаунда панели и сборка vless-ссылок.

Всё, что раньше было захардкожено в add_client.py, здесь вычисляется из
ответа панели. Это принципиально: после ротации REALITY-ключей или смены
пути WS ссылки продолжат работать сами.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote, urlencode


def _loads(value: Any) -> dict:
    """Панель отдаёт settings/streamSettings строками, но не всегда."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def parse_inbound(raw: dict) -> dict:
    """Из сырого инбаунда достать всё нужное для ссылки.

    Возвращает stream_meta — то, что ляжет в БД и потом в query-строку.
    Ключ "problems" непустой → инбаунд нельзя отдавать клиентам.
    """
    stream = _loads(raw.get("streamSettings"))
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")

    meta: dict[str, Any] = {"network": network, "security": security}
    problems: list[str] = []

    if security == "reality":
        rs = stream.get("realitySettings") or {}
        inner = rs.get("settings") or {}

        names = rs.get("serverNames") or []
        meta["sni"] = names[0] if names else ""

        # Известный баг 3x-ui: publicKey то внутри settings, то верхним уровнем.
        meta["pbk"] = inner.get("publicKey") or rs.get("publicKey") or ""

        sids = rs.get("shortIds") or []
        meta["sid"] = sids[0] if sids else ""

        meta["fp"] = inner.get("fingerprint") or "chrome"
        meta["spx"] = inner.get("spiderX") or "/"

        if not meta["pbk"]:
            problems.append("reality: пустой publicKey")
        if not meta["sni"]:
            problems.append("reality: пустой serverNames")

    elif security == "tls":
        ts = stream.get("tlsSettings") or {}
        inner = ts.get("settings") or {}
        meta["sni"] = ts.get("serverName") or ""
        meta["fp"] = inner.get("fingerprint") or "chrome"
        alpn = ts.get("alpn") or []
        if alpn:
            meta["alpn"] = ",".join(alpn)
        if inner.get("allowInsecure"):
            meta["allowInsecure"] = "1"

    if network == "ws":
        ws = stream.get("wsSettings") or {}
        meta["path"] = ws.get("path") or "/"
        host = (ws.get("headers") or {}).get("Host") or ws.get("host") or ""
        if host:
            meta["host"] = host
    elif network == "grpc":
        gs = stream.get("grpcSettings") or {}
        meta["serviceName"] = gs.get("serviceName") or ""
        if gs.get("multiMode"):
            meta["mode"] = "multi"
    elif network in ("httpupgrade", "xhttp"):
        hs = stream.get(f"{network}Settings") or {}
        meta["path"] = hs.get("path") or "/"
        if hs.get("host"):
            meta["host"] = hs["host"]

    # flow=xtls-rprx-vision работает только на голом TCP. На ws/grpc он ломает
    # соединение — в add_client.py это вешалось на клиента безусловно.
    meta["flow"] = (
        "xtls-rprx-vision"
        if network == "tcp" and security in ("reality", "tls")
        else ""
    )

    meta["problems"] = problems
    return meta


def build_vless_link(
    *, host: str, port: int, uuid: str, stream_meta: dict, label: str
) -> str:
    """Собрать vless://-ссылку. host берётся из ноды, не из инбаунда."""
    meta = dict(stream_meta)
    meta.pop("problems", None)
    if meta.get("security") == "tls" and not meta.get("sni"):
        meta["sni"] = host
    if meta.get("network") == "ws" and not meta.get("host"):
        meta["host"] = host
    flow = meta.pop("flow", "")

    query: dict[str, str] = {
        "type": meta.pop("network", "tcp"),
        "security": meta.pop("security", "none"),
        "encryption": "none",
    }
    if flow:
        query["flow"] = flow
    query.update({k: str(v) for k, v in meta.items() if v not in (None, "")})

    qs = urlencode(query, quote_via=quote, safe="")
    return f"vless://{uuid}@{host}:{port}?{qs}#{quote(label)}"
