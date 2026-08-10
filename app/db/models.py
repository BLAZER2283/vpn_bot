"""Модели.

Главная идея схемы — в `SubscriptionClient`. Подписка у пользователя одна,
а её материализаций на панелях столько, сколько активных инбаундов в системе.
Добавили сервер → всем активным подпискам добавилась строка в pending →
воркер провиженинга создал клиента на новой панели → сервер появился у всех
без перевыдачи ссылок.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import JsonDict, UtcDateTime


class SubStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    BANNED = "banned"


class ClientState(str, enum.Enum):
    """Состояние клиента на конкретной панели."""

    PENDING = "pending"    # надо создать
    ACTIVE = "active"      # создан и включён
    DISABLING = "disabling"  # надо выключить
    DISABLED = "disabled"  # выключен (подписка истекла)
    REMOVING = "removing"  # надо удалить
    REMOVED = "removed"    # удалён
    FAILED = "failed"      # панель отвергла, нужен разбор руками


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELED = "canceled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")


class Node(Base):
    """Сервер с панелью 3x-ui."""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    flag: Mapped[str] = mapped_column(String(8), default="")

    # Полный базовый URL панели вместе с секретным путём,
    # например https://1.2.3.4:45563/iCVCKaB3GmKMCgBvnR
    panel_base_url: Mapped[str] = mapped_column(String(512))
    panel_user: Mapped[str] = mapped_column(String(128))
    panel_pass: Mapped[str] = mapped_column(String(256))

    # Адрес, который попадёт в vless-ссылку. Часто совпадает с хостом панели,
    # но может отличаться (домен вместо IP, CDN и т.п.).
    public_host: Mapped[str] = mapped_column(String(255))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )

    inbounds: Mapped[list["Inbound"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )


class Inbound(Base):
    """Инбаунд внутри панели. Один инбаунд = одна ссылка в подписке."""

    __tablename__ = "inbounds"
    __table_args__ = (UniqueConstraint("node_id", "panel_inbound_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"))
    panel_inbound_id: Mapped[int] = mapped_column(Integer)

    remark: Mapped[str] = mapped_column(String(128), default="")
    protocol: Mapped[str] = mapped_column(String(32), default="vless")
    port: Mapped[int] = mapped_column(Integer)

    # Всё, что нужно для сборки vless-ссылки: security, type, pbk, sid, sni,
    # fp, flow, path, host, alpn. Снимается с панели, не пишется руками.
    stream_meta: Mapped[dict] = mapped_column(JsonDict, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime)

    node: Mapped[Node] = relationship(back_populates="inbounds")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    # То, что пользователь вставляет в клиент. Ротируется по кнопке.
    sub_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Один UUID на все ноды: панели независимы, конфликта нет,
    # зато поддержка проще — по одному UUID видно клиента везде.
    client_uuid: Mapped[str] = mapped_column(String(36))

    status: Mapped[str] = mapped_column(String(16), default=SubStatus.ACTIVE.value)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    device_limit: Mapped[int] = mapped_column(Integer, default=6)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)

    # Чтобы не слать одно и то же напоминание дважды.
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="subscriptions")
    clients: Mapped[list["SubscriptionClient"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class SubscriptionClient(Base):
    """Клиент подписки на конкретном инбаунде конкретной ноды."""

    __tablename__ = "subscription_clients"
    __table_args__ = (UniqueConstraint("subscription_id", "inbound_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    inbound_id: Mapped[int] = mapped_column(ForeignKey("inbounds.id", ondelete="CASCADE"))

    state: Mapped[str] = mapped_column(
        String(16), default=ClientState.PENDING.value, index=True
    )
    # email — уникальный идентификатор клиента внутри панели.
    remote_email: Mapped[str] = mapped_column(String(128))
    # UUID, который реально применён на панели. Отличается от
    # Subscription.client_uuid только после ротации ключа — тогда старого
    # клиента надо снести, иначе он останется рабочим.
    remote_uuid: Mapped[str | None] = mapped_column(String(36))

    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # Не трогать раньше этого времени (экспоненциальный backoff).
    next_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now()
    )

    subscription: Mapped[Subscription] = relationship(back_populates="clients")
    inbound: Mapped[Inbound] = relationship()


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL")
    )

    provider: Mapped[str] = mapped_column(String(32))
    # Защита от двойного зачисления: повторный webhook или повторный
    # successful_payment упрётся в этот индекс.
    provider_payment_id: Mapped[str] = mapped_column(String(128), unique=True)

    plan_code: Mapped[str] = mapped_column(String(16))
    months: Mapped[int] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8))

    status: Mapped[str] = mapped_column(String(16), default=PaymentStatus.PAID.value)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now()
    )
