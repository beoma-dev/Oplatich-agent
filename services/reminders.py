"""Напоминания по срокам оплаты.

Плановая дата раньше никем не читалась: заявка со сроком «завтра» могла тихо
висеть в «Новая», а «Отложена» вообще была тупиком. Раз в сутки бот считает
две вещи и рассылает их:

  • финансистам — что предстоит оплатить завтра (суммы по валютам, список);
  • администраторам — что просрочено (срок прошёл, а заявка не оплачена).

Расписание и параметры — блок «Напоминания финансистам» в админ-панели ⚙️
(значения по умолчанию берутся из REMINDERS_ENABLED / REMINDER_TIME в .env);
правки применяются без рестарта. Цикл переживает любые сбои: напоминание
вторично по отношению к приёму заявок.
"""
from __future__ import annotations

import asyncio
import html
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode

from bot.models import STATUS_NEW
from bot.validators import ValidationError, parse_amount
from config import settings
from services import runtime_settings as rs
from services import storage
from services.notifier import resolved_finance_ids
from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Как часто перечитываем расписание (и ловим смену суток).
# Тикаем раз в минуту: у каждого получателя своё время, и попасть
# нужно в его минуту, а не в общее окно.
TICK_INTERVAL = 30.0
# Сколько заявок просматриваем: напоминания смотрят «хвост» реестра.
SCAN_LIMIT = 500

# Статусы, по которым заявка ещё ждёт оплаты.
PENDING_STATUSES = {STATUS_NEW, "Отложена"}

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d")


def parse_planned(value: str) -> date | None:
    """Плановая дата из строки реестра (None — пусто или не разобрать)."""
    raw = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _amount(row: dict[str, str]) -> Decimal:
    """Сумма из строки реестра.

    Разбор — канонический parse_amount, а не самодельный: Google отдаёт суммы
    отформатированными («125 000,50»), причём разделитель тысяч — НЕРАЗРЫВНЫЙ
    пробел, который обычный .replace(" ", "") не убирает. Из-за этого сумма
    в сводке молча превращалась в ноль.
    """
    try:
        return parse_amount(str(row.get("Сумма", "")))
    except ValidationError:
        return Decimal(0)


def split_by_deadline(
    rows: list[dict[str, str]], today: date, days_before: int = 1
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Делит неоплаченные заявки на «скоро платить» и «просрочены».

    days_before — горизонт предупреждения: 1 даёт классическое «завтра»,
    3 — всё, что предстоит в ближайшие три дня, 0 — только сегодняшние.
    """
    horizon = today + timedelta(days=max(0, days_before))
    due, overdue = [], []
    for row in rows:
        if row.get("Статус оплаты", "") not in PENDING_STATUSES:
            continue
        planned = parse_planned(row.get("Плановая дата оплаты", ""))
        if planned is None:
            continue
        if planned < today:
            overdue.append(row)
        elif today <= planned <= horizon and not (days_before > 0 and planned == today):
            # Сегодняшние при days_before>0 не дёргаем: их уже анонсировали.
            due.append(row)
    return due, overdue


def _totals_line(rows: list[dict[str, str]]) -> str:
    """«125 000.50 RUB + 300.00 USD» — сумма к оплате по валютам."""
    totals: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        totals[row.get("Валюта", "") or "—"] += _amount(row)
    return " + ".join(
        f"{value:,.2f} {currency}".replace(",", " ")
        for currency, value in sorted(totals.items())
    )


def _list_lines(rows: list[dict[str, str]], limit: int = 10) -> str:
    e = html.escape
    lines = []
    for row in rows[:limit]:
        urgent = "🔴 " if row.get("Срочность", "") == "Срочно" else ""
        lines.append(
            f"• {urgent}{e(row.get('Контрагент', '—'))} — "
            f"<b>{e(str(row.get('Сумма', '')))} {e(row.get('Валюта', ''))}</b> "
            f"({e(row.get('Сотрудник по заявке', '—'))})"
        )
    if len(rows) > limit:
        lines.append(f"…и ещё {len(rows) - limit}")
    return "\n".join(lines)


def build_due_message(rows: list[dict[str, str]], days_before: int = 1) -> str:
    when = {0: "Сегодня", 1: "Завтра"}.get(
        days_before, f"В ближайшие {days_before} дн."
    )
    return (
        f"⏰ <b>{when} к оплате: {len(rows)}</b>\n"
        f"Сумма: <b>{_totals_line(rows)}</b>\n\n" + _list_lines(rows)
    )


def build_overdue_message(rows: list[dict[str, str]]) -> str:
    e = html.escape
    lines = []
    for row in rows[:10]:
        lines.append(
            f"• {e(row.get('Плановая дата оплаты', '—'))} — "
            f"{e(row.get('Контрагент', '—'))}, "
            f"<b>{e(str(row.get('Сумма', '')))} {e(row.get('Валюта', ''))}</b> "
            f"({e(row.get('Статус оплаты', '—'))})"
        )
    if len(rows) > 10:
        lines.append(f"…и ещё {len(rows) - 10}")
    return (
        f"🚨 <b>Просрочено: {len(rows)}</b>\n"
        f"Сумма: <b>{_totals_line(rows)}</b>\n\n" + "\n".join(lines)
    )


async def _send(bot: Bot, chat_ids: list[int], text: str) -> int:
    delivered = 0
    for chat_id in chat_ids:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            delivered += 1
        except Exception:  # noqa: BLE001 — один недоступный не срывает остальных
            log.warning("Не удалось отправить напоминание в чат %s", chat_id)
    return delivered


def overdue_recipients(target: str) -> list[int]:
    """Кому уходит сводка по просрочке — по настройке из админ-панели."""
    financiers = resolved_finance_ids()
    admins = list(effective_admin_ids())
    if target == "financiers":
        return financiers
    if target == "both":
        return financiers + [a for a in admins if a not in financiers]
    return admins


def recipients() -> list[int]:
    """Кому вообще может уйти напоминание: финансисты плюс адресаты просрочки."""
    cfg = rs.reminders_config()
    out = list(resolved_finance_ids())
    for uid in overdue_recipients(cfg["overdue_to"]):
        if uid not in out:
            out.append(uid)
    return out


async def send_to(bot: Bot, user_id: int, rows: list[dict[str, str]],
                  today: date, *, force: bool = False) -> tuple[int, int]:
    """Напоминание одному получателю — по ЕГО настройкам.

    Окно «за сколько дней» у каждого своё, поэтому и выборка своя: у одного
    «завтра к оплате», у другого «на неделю вперёд».

    force — ручной прогон «Проверить сейчас»: он обходит общий выключатель
    расписания, но НЕ личный отказ получателя.
    """
    cfg = rs.personal_reminders(user_id)
    if cfg["muted"] or (not cfg["enabled"] and not force):
        return 0, 0
    due, overdue = split_by_deadline(rows, today, cfg["days_before"])
    sent_due = sent_overdue = 0
    if due and user_id in resolved_finance_ids():
        sent_due = len(due) if await _send(
            bot, [user_id], build_due_message(due, cfg["days_before"])) else 0
    wants_overdue = cfg["overdue_enabled"] and rs.reminders_config()["overdue_enabled"]
    if overdue and wants_overdue and user_id in overdue_recipients(
            rs.reminders_config()["overdue_to"]):
        sent_overdue = len(overdue) if await _send(
            bot, [user_id], build_overdue_message(overdue)) else 0
    return sent_due, sent_overdue


async def run_reminders(bot: Bot, today: date | None = None) -> tuple[int, int]:
    """Рассылает напоминания всем получателям сразу — кнопка «Проверить сейчас».

    Возвращает (к оплате скоро, просрочено) по ОБЩИМ настройкам: это сводка
    для админа, а каждому уходит его собственная выборка.
    """
    cfg = rs.reminders_config()
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    rows = await storage.recent_requests(limit=SCAN_LIMIT)
    for user_id in recipients():
        await send_to(bot, user_id, rows, today, force=True)
    due, overdue = split_by_deadline(rows, today, cfg["days_before"])
    log.info("Напоминания: к оплате %s, просрочено %s", len(due), len(overdue))
    return len(due), len(overdue)


def _hhmm(now: datetime) -> str:
    return now.strftime("%H:%M")


async def reminder_loop(bot: Bot) -> None:
    """Фоновая задача: каждому получателю — в его собственное время.

    Раньше время было одно на всех и цикл спал до него. Теперь у каждого
    финансиста своё расписание, поэтому тикаем раз в минуту и смотрим, кому
    сейчас пора. Настройки перечитываются на каждом тике — правки из панели
    применяются без рестарта.
    """
    log.info("Планировщик напоминаний запущен: %s", rs.reminders_config())
    # Кому и в какой день уже отправили: защита от повтора внутри минуты.
    sent_on: dict[int, date] = {}
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        if not rs.reminders_config()["enabled"]:
            continue
        now = datetime.now(ZoneInfo(settings.timezone))
        today = now.date()
        due_now = [
            uid for uid in recipients()
            if rs.personal_reminders(uid)["time"] == _hhmm(now) and sent_on.get(uid) != today
        ]
        if not due_now:
            continue
        try:
            rows = await storage.recent_requests(limit=SCAN_LIMIT)
            for user_id in due_now:
                await send_to(bot, user_id, rows, today)
                sent_on[user_id] = today
        except Exception:  # noqa: BLE001 — цикл должен пережить любой сбой
            log.exception("Сбой напоминаний о сроках")
