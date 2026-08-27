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

from telegram import Bot, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import NetworkError

from bot.models import STATUS_NEW
from bot.validators import ValidationError, parse_amount
from config import settings
from services import runtime_settings as rs
from services import storage, tg_retry
from services.notifier import open_button, resolved_finance_ids
from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Сколько повторять недоставленное напоминание, секунд. Полчаса: дольше —
# и напоминание «оплатите сегодня» приходит, когда день уже прошёл.
RETRY_WINDOW = 1800.0


class ReminderNotDelivered(Exception):
    """Напоминание собрали, но не смогли отправить: канал молчит."""

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


def digest_link(bot: Bot, chat_id: int, param: str) -> InlineKeyboardMarkup | None:
    """Кнопка «Открыть в приложении» под СВОДКОЙ — сразу с нужным фильтром.

    Сводка перечисляет несколько заявок, поэтому ведёт не на одну из них, а
    на выборку: `overdue` включает в панели фильтр «⚠️ Просрочены»,
    `due_<с>_<по>` — окно плановых дат. Своей выборки не заводим: у панели
    ровно эти фильтры уже есть, они видны в полях, и «Сбросить» работает
    как обычно.

    Список НОМЕРОВ в ссылку не кладём намеренно. Сводка собрана в свой час,
    а открывают её позже: «просроченные на сейчас» — более честный ответ,
    чем вчерашний перечень, и он не растёт вместе с числом заявок (в
    startapp помещается 512 символов, это два десятка номеров).
    """
    btn = open_button(bot, param, chat_id)
    return InlineKeyboardMarkup([[btn]]) if btn else None


async def _send(
    bot: Bot, chat_ids: list[int], text: str, param: str = ""
) -> tuple[int, bool]:
    """(сколько доставлено, была ли СЕТЕВАЯ потеря).

    Сеть и смысл разделены намеренно. «Канал молчит» — повод попробовать
    позже: сообщение актуально, адресат на месте. «Чата нет» или «бот
    заблокирован» — повод забыть: повторяй хоть до вечера, ничего не
    изменится, а день окажется съеден бесполезными попытками.
    """
    delivered, lost = 0, False
    for chat_id in chat_ids:
        try:
            keyboard = digest_link(bot, chat_id, param) if param else None
            await tg_retry.send_with_retry(
                lambda cid=chat_id, kb=keyboard: bot.send_message(
                    chat_id=cid, text=text, parse_mode=ParseMode.HTML,
                    reply_markup=kb,
                ),
                what=f"Напоминание в чат {chat_id}",
            )
            delivered += 1
        except NetworkError:
            lost = True
            log.warning("Напоминание в чат %s: канал молчит, повторю", chat_id)
        except Exception:  # noqa: BLE001 — один недоступный не срывает остальных
            log.warning("Не удалось отправить напоминание в чат %s", chat_id)
    return delivered, lost


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
    # Выходные: платежей нет, напоминать не о чем — но ручной прогон делаем.
    if cfg["weekdays_only"] and today.weekday() >= 5 and not force:
        return 0, 0
    due, overdue = split_by_deadline(rows, today, cfg["days_before"])
    sent_due = sent_overdue = 0
    # Была ли отправка, которая не удалась. Отличать это от «отправлять было
    # нечего» обязательно: иначе планировщик пометит день закрытым и съест
    # напоминание на сутки — ровно то, от чего уже защищались на чтении
    # реестра (strict=True), но не защитились на самой отправке.
    _lost = False
    if due and cfg["due_enabled"] and user_id in resolved_finance_ids():
        # Окно то же, по которому выбраны строки: от сегодня до горизонта.
        window = f"due_{today:%Y-%m-%d}_{today + timedelta(days=cfg['days_before']):%Y-%m-%d}"
        ok, lost = await _send(
            bot, [user_id], build_due_message(due, cfg["days_before"]), window
        )
        sent_due = len(due) if ok else 0
        _lost = _lost or lost
    wants_overdue = cfg["overdue_enabled"] and rs.reminders_config()["overdue_enabled"]
    if overdue and wants_overdue and user_id in overdue_recipients(
            rs.reminders_config()["overdue_to"]):
        ok, lost = await _send(
            bot, [user_id], build_overdue_message(overdue), "overdue"
        )
        sent_overdue = len(overdue) if ok else 0
        _lost = _lost or lost
    if _lost:
        raise ReminderNotDelivered(user_id)
    return sent_due, sent_overdue


async def run_reminders(bot: Bot, today: date | None = None) -> tuple[int, int]:
    """Рассылает напоминания всем получателям сразу — кнопка «Проверить сейчас».

    Возвращает (к оплате скоро, просрочено) по ОБЩИМ настройкам: это сводка
    для админа, а каждому уходит его собственная выборка.
    """
    cfg = rs.reminders_config()
    today = today or datetime.now(ZoneInfo(settings.timezone)).date()
    rows = await storage.recent_requests(limit=SCAN_LIMIT, strict=True)
    for user_id in recipients():
        try:
            await send_to(bot, user_id, rows, today, force=True)
        except ReminderNotDelivered:
            # Ручной прогон повторов не планирует: админ видит результат
            # сразу и нажмёт ещё раз. Главное — не оборвать остальных.
            log.warning("Ручной прогон: напоминание для %s не ушло", user_id)
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
    # Кому не смогли доставить: пробуем снова на каждом тике, но не вечно —
    # к концу окна напоминание уже неактуально, а спам вреднее молчания.
    pending: dict[int, datetime] = {}
    while True:
        await asyncio.sleep(TICK_INTERVAL)
        if not rs.reminders_config()["enabled"]:
            continue
        now = datetime.now(ZoneInfo(settings.timezone))
        today = now.date()
        for uid, since in list(pending.items()):
            if (now - since).total_seconds() > RETRY_WINDOW:
                log.error("Напоминание для %s так и не ушло за %.0f мин", uid, RETRY_WINDOW / 60)
                pending.pop(uid, None)
                sent_on[uid] = today          # больше не пытаемся сегодня
        due_now = [
            uid for uid in recipients()
            if (rs.personal_reminders(uid)["time"] == _hhmm(now) or uid in pending)
            and sent_on.get(uid) != today
        ]
        if not due_now:
            continue
        try:
            # strict: на недоступном реестре нельзя помечать день отправленным,
            # иначе одна сетевая заминка съедала напоминания на все сутки.
            rows = await storage.recent_requests(limit=SCAN_LIMIT, strict=True)
        except storage.RegistryUnavailable:
            log.warning("Напоминания: реестр недоступен, повторю на следующем тике")
            continue
        for user_id in due_now:
            try:
                await send_to(bot, user_id, rows, today)
            except ReminderNotDelivered:
                # День НЕ помечаем: получатель остаётся в окне повтора и
                # получит своё, как только канал оживёт.
                pending[user_id] = now
                log.warning("Напоминание для %s не ушло, повторю", user_id)
                continue
            except Exception:  # noqa: BLE001 — один сбой не срывает остальных
                log.exception("Сбой напоминания для %s", user_id)
            sent_on[user_id] = today
            pending.pop(user_id, None)
