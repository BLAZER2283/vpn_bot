"""Все тексты в одном месте — править их не залезая в логику."""

from __future__ import annotations

from datetime import datetime

from app.config import settings
from app.db.repo import utcnow

GREETING = (
    "<b>{title}</b>\n\n"
    "Один ключ — все серверы. Ставишь ссылку в приложение один раз, "
    "дальше серверы добавляются и обновляются сами.\n\n"
    "Трафик без ограничений, до {devices} устройств."
)

NO_SUBSCRIPTION = (
    "Подписки пока нет.\n\n"
    "Можно взять бесплатно на {days} дн. и проверить скорость, "
    "или сразу оформить тариф."
)

TRIAL_USED = "Пробный период уже использован — доступен любой из тарифов."

SETUP_GUIDE = (
    "<b>Как подключиться</b>\n\n"
    "1. Установи приложение:\n"
    "   • Android — <a href='https://play.google.com/store/apps/details?id=com.v2raytun.android'>v2rayTun</a> "
    "или v2rayNG\n"
    "   • iPhone — <a href='https://apps.apple.com/app/streisand/id6450534064'>Streisand</a> "
    "или v2rayTun\n"
    "   • Windows — <a href='https://github.com/hiddify/hiddify-next/releases'>Hiddify</a>\n"
    "   • macOS — Streisand или Hiddify\n\n"
    "2. Скопируй свою ссылку из «Моя подписка».\n\n"
    "3. В приложении: <b>добавить из буфера обмена</b> "
    "(или «Add profile from clipboard»).\n\n"
    "4. Выбери сервер и включи.\n\n"
    "Серверов в подписке несколько — переключайся между ними, "
    "если какой-то работает медленно. Новые появятся сами."
)

PAY_INTRO = (
    "Выбери срок. Чем дольше — тем дешевле месяц.\n\n"
    "Подписка продлевается сразу после оплаты, ссылка остаётся прежней."
)


def plural_days(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "день"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def fmt_left(expires_at: datetime) -> str:
    delta = expires_at - utcnow()
    if delta.total_seconds() <= 0:
        return "истекла"
    days = delta.days
    if days >= 1:
        return f"{days} {plural_days(days)}"
    hours = int(delta.total_seconds() // 3600)
    return f"{hours} ч" if hours else "меньше часа"


def subscription_card(
    *, expires_at: datetime, is_trial: bool, sub_url: str,
    ready: int, total: int, devices: int,
) -> str:
    head = "Пробная подписка" if is_trial else "Подписка активна"
    lines = [
        f"<b>{head}</b>",
        "",
        f"Осталось: <b>{fmt_left(expires_at)}</b>",
        f"До: {expires_at:%d.%m.%Y %H:%M} UTC",
        f"Устройств: до {devices}",
    ]

    if total == 0:
        lines.append("Серверы: настраиваются")
    elif ready == total:
        lines.append(f"Серверов: {total}")
    else:
        # Частичная выдача — нормальный режим, а не сбой. Говорим прямо,
        # чтобы человек не решил, что заплатил зря.
        lines.append(
            f"Серверов: {ready} из {total} — остальные подключатся сами"
        )

    lines += [
        "",
        "<b>Твоя ссылка:</b>",
        f"<code>{sub_url}</code>",
        "",
        "Вставь её в приложение один раз. Продление и новые серверы "
        "подтянутся автоматически — ссылка не меняется.",
    ]
    return "\n".join(lines)


def expired_card(expires_at: datetime) -> str:
    return (
        "<b>Подписка закончилась</b>\n\n"
        f"Истекла {expires_at:%d.%m.%Y}. Ссылка сохранена — после продления "
        "всё заработает само, перенастраивать приложение не нужно."
    )


def greeting() -> str:
    return GREETING.format(title=settings.sub_title, devices=settings.device_limit)


def no_subscription() -> str:
    return NO_SUBSCRIPTION.format(days=settings.trial_days)
