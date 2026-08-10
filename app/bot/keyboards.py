"""Клавиатуры и префиксы callback_data."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.config import PLANS, settings

# Префиксы callback_data.
CB_BUY = "buy"
CB_PLAN = "plan"
CB_TRIAL = "trial"
CB_SUB = "sub"
CB_HELP = "help"
CB_HOME = "home"
CB_ROTATE = "rotate"
CB_ROTATE_OK = "rotate_ok"
CB_PAY_STARS = "pstars"
CB_PAY_MANUAL = "pman"
CB_PAY_OK = "payok"
CB_PAY_NO = "payno"


def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🔑 Моя подписка"), KeyboardButton(text="💳 Купить")],
        [KeyboardButton(text="📱 Как подключить")],
    ]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def start_inline(*, trial_available: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if trial_available:
        rows.append([
            InlineKeyboardButton(
                text=f"🎁 Попробовать {settings.trial_days} дн. бесплатно",
                callback_data=CB_TRIAL,
            )
        ])
    rows.append([InlineKeyboardButton(text="💳 Тарифы", callback_data=CB_BUY)])
    rows.append([
        InlineKeyboardButton(text="📱 Как подключить", callback_data=CB_HELP)
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plans_kb() -> InlineKeyboardMarkup:
    rows = []
    for plan in PLANS:
        per_month = plan.price // plan.months
        label = f"{plan.title} — {plan.price} ⭐"
        if plan.months > 1:
            label += f"  ({per_month}/мес)"
        rows.append([
            InlineKeyboardButton(
                text=label, callback_data=f"{CB_PLAN}:{plan.code}"
            )
        ])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=CB_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_method_kb(plan_code: str) -> InlineKeyboardMarkup:
    """Способ оплаты. Пока звёзды и перевод — остальное добавится классом."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Telegram Stars",
                    callback_data=f"{CB_PAY_STARS}:{plan_code}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="💸 Перевод (подтвердит админ)",
                    callback_data=f"{CB_PAY_MANUAL}:{plan_code}",
                )
            ],
            [InlineKeyboardButton(text="← Тарифы", callback_data=CB_BUY)],
        ]
    )


def subscription_kb(*, has_sub: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📱 Как подключить", callback_data=CB_HELP)],
        [
            InlineKeyboardButton(
                text="💳 Продлить" if has_sub else "💳 Тарифы",
                callback_data=CB_BUY,
            )
        ],
    ]
    if has_sub:
        rows.append([
            InlineKeyboardButton(
                text="🔄 Сбросить ссылку", callback_data=CB_ROTATE
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def rotate_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, сбросить", callback_data=CB_ROTATE_OK
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=CB_SUB)],
        ]
    )


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="← В меню", callback_data=CB_HOME)]
        ]
    )


def manual_review_kb(ref: str) -> InlineKeyboardMarkup:
    """Кнопки для админа под заявкой на оплату переводом.

    В ref упакованы получатель, тариф и идентификатор заявки — состояние
    нигде не хранится, поэтому кнопка переживает перезапуск бота.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Зачесть", callback_data=f"{CB_PAY_OK}:{ref}"
                ),
                InlineKeyboardButton(
                    text="✖️ Отклонить", callback_data=f"{CB_PAY_NO}:{ref}"
                ),
            ]
        ]
    )
