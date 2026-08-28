"""Сводка по заявкам для админа: кто пользуется, где застревает, что с документами.

Считалка (`build`) — ЧИСТАЯ функция от строк реестра, отметок об оплате и
справочника пользователей. Так её можно проверить на выдуманных данных, не
поднимая ни Google, ни SQLite, — а сбор данных остаётся тонкой обёрткой.

Три вопроса, на которые отвечает сводка, и ничего сверх них:
  «внедрение идёт?»   — сколько людей подаёт, кто получил доступ и молчит;
  «где застревает?»   — сколько ждёт дольше нормы, что просрочено, за сколько
                        дней в среднем платят;
  «что с бумагами?»   — сколько заявок без документов и сколько оплаченных
                        без закрывающих.

Суммы НИКОГДА не складываются между валютами: 100 ₽ и 100 $ — не 200.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from bot.models import REQUEST_STATUSES, STATUS_NEW
from bot.validators import ValidationError, parse_amount
from config import settings
from services import audit, storage
from services import runtime_settings as rs
from services.reminders import PENDING_STATUSES, SCAN_LIMIT, split_by_deadline
from services.user_directory import all_users

log = logging.getLogger(__name__)

STATUS_PAID = REQUEST_STATUSES["PAID"][1]
CLOSING_HEADER = "Закрывающие документы"
# Сколько дней ожидания считаем нормой. Три рабочих дня — то, за что никто
# не ругается; всё, что дольше, стоит увидеть в отдельном числе.
SLOW_AFTER_DAYS = 3
TOP_LIMIT = 5


def _amount(row: dict[str, str]) -> Decimal:
    try:
        return parse_amount(str(row.get("Сумма", "")))
    except ValidationError:
        return Decimal(0)


def _totals(rows: list[dict[str, str]]) -> dict[str, str]:
    """{валюта: сумма} строками — JSON не знает Decimal, а float округляет."""
    acc: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        acc[row.get("Валюта", "") or "—"] += _amount(row)
    return {cur: f"{val:.2f}" for cur, val in sorted(acc.items())}


def _submitted(row: dict[str, str]) -> date | None:
    raw = str(row.get("Дата внесения в реестр", "")).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def build(
    rows: list[dict[str, str]],
    paid_at: dict[str, float],
    known_users: list[tuple[int, str]],
    allowed_ids: set[int],
    today: date,
    days: int,
) -> dict:
    """Сводка. rows — строки реестра, paid_at — {ID заявки: время оплаты}."""
    since = date.fromordinal(today.toordinal() - max(1, days) + 1)
    period = [r for r in rows if (_submitted(r) or date.min) >= since]

    # --- Люди -------------------------------------------------------------
    by_author: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in period:
        name = row.get("Сотрудник по заявке", "") or "—"
        by_author[name].append(row)
    top = sorted(by_author.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP_LIMIT]

    ever = {r.get("Telegram ID", "") for r in rows if r.get("Telegram ID")}
    # Доступ выдали, а человек ни разу не подал: либо не знает, либо не нужен.
    idle = sorted(
        name for uid, name in known_users
        if uid in allowed_ids and str(uid) not in ever
    )

    # --- Сроки ------------------------------------------------------------
    _, overdue = split_by_deadline(rows, today, 0)
    waiting_long = []
    for row in rows:
        if row.get("Статус оплаты", "") not in PENDING_STATUSES:
            continue
        made = _submitted(row)
        if made is not None and (today - made).days >= SLOW_AFTER_DAYS:
            waiting_long.append(row)

    lags = []
    for row in rows:
        stamp = paid_at.get(row.get("ID заявки", ""))
        made = _submitted(row)
        if stamp is None or made is None:
            continue
        paid_day = datetime.fromtimestamp(stamp, ZoneInfo(settings.timezone)).date()
        lags.append(max(0, (paid_day - made).days))

    by_article: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in period:
        by_article[row.get("Статья", "") or "—"].append(row)
    articles = sorted(by_article.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TOP_LIMIT]

    # --- Документы --------------------------------------------------------
    no_docs = [
        r for r in rows
        if not str(r.get("Ссылка на счет", "")).strip()
        and not str(r.get("Реквизиты", "")).strip()
    ]
    paid_rows = [r for r in rows if r.get("Статус оплаты", "") == STATUS_PAID]
    no_closing = [
        r for r in paid_rows if not str(r.get(CLOSING_HEADER, "")).strip()
    ]

    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        statuses[row.get("Статус оплаты", "") or STATUS_NEW] += 1

    return {
        "days": days,
        "people": {
            "authors_period": len(by_author),
            "authors_ever": len(ever),
            "top": [
                {"name": name, "count": len(items), "sums": _totals(items)}
                for name, items in top
            ],
            "idle": idle,
        },
        "flow": {
            "period_count": len(period),
            "period_sums": _totals(period),
            "total_count": len(rows),
            "statuses": dict(sorted(statuses.items())),
            "median_days": _median(lags),
            "paid_measured": len(lags),
            "waiting_long": len(waiting_long),
            "waiting_after_days": SLOW_AFTER_DAYS,
            "overdue_now": len(overdue),
            "overdue_sums": _totals(overdue),
            "articles": [
                {"name": name, "count": len(items), "sums": _totals(items)}
                for name, items in articles
            ],
        },
        "docs": {
            "no_docs": len(no_docs),
            "paid_total": len(paid_rows),
            "paid_without_closing": len(no_closing),
        },
    }


async def summary(days: int = 30) -> dict:
    """Сводка по живым данным. Ошибка чтения аудита не отменяет остального:
    без него пропадёт только медиана «подача → оплата»."""
    rows = await storage.recent_requests(limit=SCAN_LIMIT)
    try:
        paid_at = await audit.paid_times()
    except Exception:  # noqa: BLE001 — метрика вторична
        log.exception("Аналитика: не удалось прочитать отметки об оплате")
        paid_at = {}
    allowed = set(rs.effective_allowed_ids())
    today = datetime.now(ZoneInfo(settings.timezone)).date()
    return build(rows, paid_at, all_users(), allowed, today, days)
