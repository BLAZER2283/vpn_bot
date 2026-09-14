"""Админские команды.

Всё через команды, а не кнопки: операций мало, они редкие, а текстовый
ввод не требует состояний FSM и переживает перезапуск бота.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS_BY_CODE, settings
from app.db import repo
from app.db.models import User
from app.services import nodes as node_service
from app.services import notify, provisioning
from app.services import subscription as sub_service

log = logging.getLogger(__name__)
router = Router()

# Все хендлеры этого роутера — только для админов.
router.message.filter(F.from_user.id.in_(settings.admins))

HELP = """<b>Админ-команды</b>

/stats — сводка
/nodes — список серверов
/addnode — добавить сервер
/delnode &lt;id&gt; — удалить сервер
/syncnodes — перечитать инбаунды со всех панелей
/drain — прогнать очередь выдачи сейчас
/grantnode &lt;tg_id&gt; &lt;node_id&gt; — выдать ноду пользователю
/revokenode &lt;tg_id&gt; &lt;node_id&gt; — убрать ноду у пользователя
/delnode &lt;node_id&gt; — удалить ноду из панели бота

Пользовательские команды устройств: /devices, /adddevice, /deldevice
/give &lt;tg_id&gt; &lt;plan|дни&gt; — начислить вручную
/whois &lt;tg_id&gt; — что у пользователя
/say &lt;текст&gt; — рассылка всем
"""


@router.message(Command("admin", "help"))
async def cmd_admin(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    s = await repo.stats(session)
    await message.answer(
        "<b>Статистика</b>\n\n"
        f"Пользователей: {s['total_users']}\n"
        f"Активных подписок: {s['active']} (из них триалов: {s['trials']})\n"
        f"Истекают за 3 дня: {s['expiring_3d']}\n"
        f"Оплат за 30 дней: {s['revenue_30d']} {settings.currency}\n"
        f"Зависших выдач: {s['stuck']}"
        + ("\n\n⚠️ Есть зависшие — посмотри /nodes" if s["stuck"] else "")
    )


@router.message(Command("nodes"))
async def cmd_nodes(message: Message, session: AsyncSession) -> None:
    nodes = await repo.all_nodes(session)
    if not nodes:
        await message.answer("Серверов нет. Добавить: /addnode")
        return

    lines = ["<b>Серверы</b>", ""]
    for node in nodes:
        mark = "🟢" if node.is_active else "⚪️"
        active_inbounds = [i for i in node.inbounds if i.is_active]
        lines.append(
            f"{mark} <b>{node.flag} {node.name}</b> (id {node.id})\n"
            f"   {node.public_host}\n"
            f"   инбаундов: {len(active_inbounds)} из {len(node.inbounds)}"
        )
        for inbound in node.inbounds:
            if not inbound.is_active:
                problems = inbound.stream_meta.get("problems") or ["выключен"]
                lines.append(
                    f"   ⚠️ инбаунд {inbound.panel_inbound_id}: "
                    f"{'; '.join(problems)}"
                )
    await message.answer("\n".join(lines))


@router.message(Command("addnode"))
async def cmd_addnode(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    """/addnode имя|url|логин|пароль|хост[|флаг]

    Разделитель — вертикальная черта: в паролях панелей часто есть пробелы
    и двоеточия, а вот | практически никогда.
    """
    if not command.args:
        await message.answer(
            "Формат:\n<code>/addnode Нидерланды|"
            "https://1.2.3.4:45563/secret|admin|pass|vpn.example.com|🇳🇱</code>\n\n"
            "Хост — то, что попадёт в ссылку. Лучше домен, а не IP: "
            "при смене адреса сервера ключи не придётся перевыдавать."
        )
        return

    parts = [p.strip() for p in command.args.split("|")]
    if len(parts) < 5:
        await message.answer("Нужно минимум 5 полей через |. Смотри /addnode")
        return

    name, url, user, password, host = parts[:5]
    flag = parts[5] if len(parts) > 5 else ""

    await message.answer("Подключаюсь к панели…")
    try:
        node, inbounds, tasks = await node_service.add_node(
            session,
            name=name,
            panel_base_url=url,
            panel_user=user,
            panel_pass=password,
            public_host=host,
            flag=flag,
        )
    except Exception as exc:  # noqa: BLE001 — показать причину админу
        # Откат обязателен: запись ноды уже во flush, и без него middleware
        # закоммитит её — в базе останется сервер без инбаундов.
        await session.rollback()
        await message.answer(f"Не получилось: <code>{exc}</code>")
        return

    if inbounds == 0:
        await session.rollback()
        await message.answer(
            "Панель ответила, но рабочих VLESS-инбаундов не нашлось.\n\n"
            "Проверь в панели, что инбаунд включён, протокол vless, "
            "а у REALITY заполнен публичный ключ. Сервер не добавлен."
        )
        return

    await message.answer(
        f"Сервер <b>{node.name}</b> добавлен (id {node.id}).\n"
        f"Инбаундов найдено: {inbounds}\n"
        f"Задач на выдачу: {tasks}\n\n"
        "Клиенты создаются в фоне — у пользователей сервер появится "
        "при следующем обновлении подписки. Прогнать сейчас: /drain"
    )


@router.message(Command("delnode"))
async def cmd_delnode(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    """/delnode <id> — удалить сервер из базы.

    Клиентов на самой панели не трогает: команда нужна, чтобы убрать
    ошибочные записи. Ссылки этой ноды исчезают из подписок сразу.
    """
    if not command.args or not command.args.strip().isdigit():
        await message.answer(
            "Формат: <code>/delnode 3</code>\nID смотри в /nodes"
        )
        return

    node = await repo.node_by_id(session, int(command.args.strip()))
    if node is None:
        await message.answer("Такого сервера нет")
        return

    name = f"{node.flag} {node.name} (id {node.id})"
    await session.delete(node)
    await message.answer(
        f"Удалён {name}.\n\n"
        "Инбаунды и выданные на них клиенты убраны из базы. "
        "На панели клиенты остались — при необходимости почисти вручную."
    )


@router.message(Command("syncnodes"))
async def cmd_syncnodes(message: Message, session: AsyncSession) -> None:
    nodes = await repo.all_nodes(session)
    report = []
    for node in nodes:
        try:
            inbounds = await node_service.sync_inbounds(session, node)
            ok = sum(1 for i in inbounds if i.is_active)
            report.append(f"🟢 {node.name}: {ok}/{len(inbounds)}")
        except Exception as exc:  # noqa: BLE001
            report.append(f"🔴 {node.name}: {exc}")
    await message.answer("\n".join(report) or "Серверов нет")


@router.message(Command("drain"))
async def cmd_drain(message: Message, session: AsyncSession) -> None:
    # Своя транзакция внутри — вне сессии этого апдейта.
    processed = await provisioning.drain()
    errors = await repo.provisioning_errors(session)
    if not errors:
        await message.answer(f"Обработано записей: {processed}\nОшибок нет.")
        return

    lines = [f"Обработано записей: {processed}", "", "Последние ошибки:"]
    lines.extend(
        f"• client {client.id}: {client.last_error}"
        for client in errors
    )
    await message.answer("\n".join(lines))


@router.message(Command("grantnode"))
async def cmd_grantnode(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not all(arg.isdigit() for arg in args):
        await message.answer("Формат: <code>/grantnode tg_id node_id</code>")
        return
    user = await repo.get_user_by_tg(session, int(args[0]))
    node = await repo.node_by_id(session, int(args[1]))
    sub = await repo.latest_subscription(session, user.id) if user else None
    if user is None or node is None or sub is None:
        await message.answer("Пользователь, сервер или подписка не найдены")
        return
    tasks = await node_service.assign_node_to_subscription(session, sub, node)
    await message.answer(f"Нода {node.name} назначена. Задач на выдачу: {tasks}")


@router.message(Command("revokenode"))
async def cmd_revokenode(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not all(arg.isdigit() for arg in args):
        await message.answer("Формат: <code>/revokenode tg_id node_id</code>")
        return
    user = await repo.get_user_by_tg(session, int(args[0]))
    node = await repo.node_by_id(session, int(args[1]))
    sub = await repo.latest_subscription(session, user.id) if user else None
    if user is None or node is None or sub is None:
        await message.answer("Пользователь, сервер или подписка не найдены")
        return
    await node_service.unassign_node_from_subscription(session, sub, node)
    await message.answer(f"Нода {node.name} убирается у пользователя через очередь.")


@router.message(Command("give"))
async def cmd_give(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    """/give <tg_id> <plan_code|N дней>"""
    args = (command.args or "").split()
    if len(args) != 2:
        await message.answer(
            "Формат: <code>/give 123456789 m1</code> или "
            "<code>/give 123456789 7d</code>"
        )
        return

    tg_id_raw, what = args
    if not tg_id_raw.isdigit():
        await message.answer("tg_id должен быть числом")
        return

    target = await repo.get_user_by_tg(session, int(tg_id_raw))
    if target is None:
        await message.answer("Пользователь ещё не запускал бота")
        return

    if what.endswith("d") and what[:-1].isdigit():
        days = int(what[:-1])
        sub, _ = await sub_service.grant_days(session, target, days)
        await message.answer(f"Начислено {days} дн. до {sub.expires_at:%d.%m.%Y}")
    else:
        plan = PLANS_BY_CODE.get(what)
        if plan is None:
            await message.answer(
                f"Неизвестный тариф. Доступны: {', '.join(PLANS_BY_CODE)}"
            )
            return
        sub, _ = await sub_service.grant_months(session, target, plan.months)
        await message.answer(
            f"Начислено {plan.title} до {sub.expires_at:%d.%m.%Y}"
        )

    await session.flush()
    await notify.send_safe(
        message.bot,
        target.tg_id,
        f"Подписка продлена до {sub.expires_at:%d.%m.%Y}. "
        "Ссылка та же — ничего перенастраивать не нужно.",
    )


@router.message(Command("whois"))
async def cmd_whois(
    message: Message, command: CommandObject, session: AsyncSession
) -> None:
    if not command.args or not command.args.strip().isdigit():
        await message.answer("Формат: <code>/whois 123456789</code>")
        return

    target = await repo.get_user_by_tg(session, int(command.args.strip()))
    if target is None:
        await message.answer("Не найден")
        return

    sub = await repo.latest_subscription(session, target.id)
    lines = [
        f"<b>{target.tg_id}</b> @{target.username or '—'}",
        f"триал использован: {'да' if target.trial_used else 'нет'}",
    ]
    if sub is None:
        lines.append("подписок нет")
    else:
        ready, total = await provisioning.ready_count(session, sub.id)
        lines += [
            f"статус: {sub.status}"
            f"{' (триал)' if sub.is_trial else ''}",
            f"до: {sub.expires_at:%d.%m.%Y %H:%M} UTC",
            f"серверов готово: {ready}/{total}",
        ]
        clients = await repo.clients_of_subscription(session, sub.id)
        for client in clients:
            if client.last_error:
                lines.append(
                    f"⚠️ {client.inbound.node.name}: {client.state}, "
                    f"попыток {client.attempts}\n   {client.last_error[:200]}"
                )
    await message.answer("\n".join(lines))


@router.message(Command("say"))
async def cmd_say(message: Message, command: CommandObject) -> None:
    if not command.args:
        await message.answer("Формат: <code>/say текст рассылки</code>")
        return
    await message.answer("Рассылаю…")
    sent, total = await notify.broadcast(message.bot, command.args)
    await message.answer(f"Доставлено {sent} из {total}")
