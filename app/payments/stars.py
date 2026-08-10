"""Оплата звёздами Telegram (XTR).

Единственный способ принимать деньги без юрлица и внешней регистрации,
поэтому он и включён по умолчанию. provider_payment_id берём из
telegram_payment_charge_id — он уникален и стабилен, на нём и держится
защита от повторного зачисления.
"""

from __future__ import annotations

from aiogram.types import LabeledPrice

from app.config import Plan
from app.payments.base import Invoice

CODE = "stars"


class StarsProvider:
    code = CODE
    title = "Telegram Stars ⭐"

    def build_invoice(self, plan: Plan, user_tg_id: int) -> Invoice:
        return Invoice(
            provider=self.code,
            # Настоящий id придёт от Telegram после оплаты; до этого момента
            # платёж в БД не пишется вообще — записывать нечего.
            provider_payment_id="",
            send_invoice_kwargs={
                "title": f"VPN — {plan.title}",
                "description": (
                    f"Доступ ко всем серверам на {plan.title.lower()}. "
                    "Одна ссылка, безлимитный трафик."
                ),
                # payload вернётся в successful_payment — по нему узнаем тариф.
                "payload": f"plan:{plan.code}",
                "currency": "XTR",
                # Для звёзд provider_token пустой — это не ошибка.
                "provider_token": "",
                "prices": [LabeledPrice(label=plan.title, amount=plan.price)],
            },
        )


provider = StarsProvider()
