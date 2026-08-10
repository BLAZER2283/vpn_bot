"""Абстракция платёжных провайдеров.

Подключить ЮKassa или крипту = добавить файл с классом, реализующим этот
протокол, и строку в PROVIDERS. Всё остальное — тарифы, продление,
идемпотентность — общее и переписывать не придётся.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.config import Plan


@dataclass(slots=True)
class Invoice:
    """Как показать пользователю счёт.

    Провайдер заполняет либо send_invoice_kwargs (нативный счёт Telegram),
    либо url/text (внешняя оплата или ручное подтверждение).
    """

    provider: str
    provider_payment_id: str
    send_invoice_kwargs: dict | None = None
    url: str = ""
    text: str = ""
    extra: dict = field(default_factory=dict)


class PaymentProvider(Protocol):
    """Контракт провайдера."""

    code: str
    title: str

    def build_invoice(self, plan: Plan, user_tg_id: int) -> Invoice:
        """Подготовить счёт. Ничего не пишет в БД — только формирует данные."""
        ...
