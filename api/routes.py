"""HTTP-маршруты Mini App: приём заявки из формы.

Использует те же валидаторы и финализацию, что и чат-форма бота, — оба канала
дают одинаковые записи в реестре и одинаковые уведомления.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from api.auth import validate_init_data
from bot.access import is_allowed, is_bot_admin, is_financier
from bot.models import (
    CURRENCIES,
    REQUEST_ID_RE,
    REQUEST_STATUSES,
    InvoiceRequest,
    Urgency,
    new_request_id,
)
from bot.my_requests import LIST_LIMIT as MY_LIST_LIMIT
from bot.scheduling import auto_planned_date
from bot.validators import (
    MAX_EXTRA_FILES,
    MAX_FILE_SIZE_BYTES,
    ValidationError,
    looks_broken,
    looks_like_gibberish,
    parse_amount,
    parse_planned_date,
    parse_registry_filter_date,
    validate_file,
    validate_line_field,
    validate_text_field,
)
from config import settings
from services import (
    alerts,
    audit,
    backup,
    dedup,
    invoice_check,
    invoice_extract,
    notifier,
    registry_check,
    request_meta,
    restore,
    storage,
)
from services import health as health_pulse
from services import runtime_settings as rs
from services.access_requests import request_access
from services.deletion import delete_request as delete_request_service
from services.intake import finalize_submission
from services.local_storage import build_extra_filename, build_invoice_filename
from services.notifier import closing_docs_notify, overdue_nudge
from services.reminders import PENDING_STATUSES
from services.reminders import SCAN_LIMIT as REMINDER_SCAN_LIMIT
from services.reminders import send_to as send_reminder_to
from services.status_change import apply_status
from services.user_directory import all_users, resolve, username_for
from services.withdraw import withdraw_request

log = logging.getLogger(__name__)

router = APIRouter()

# Простой rate limit подачи заявок: не более N за окно на пользователя.
_RATE_MAX = 5
_RATE_WINDOW = 60.0
_rate: dict[int, list[float]] = {}


def _rate_limited(user_id: int) -> bool:
    """True — лимит превышен; иначе учитывает попытку."""
    now = time.monotonic()
    stamps = [t for t in _rate.get(user_id, []) if now - t < _RATE_WINDOW]
    if len(stamps) >= _RATE_MAX:
        return True
    stamps.append(now)
    _rate[user_id] = stamps
    return False


# Отдельный, более щедрый лимит для предпроверки файла (OCR — дорогая).
_CHECK_RATE_MAX = 10
_check_rate: dict[int, list[float]] = {}


def _check_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    stamps = [t for t in _check_rate.get(user_id, []) if now - t < _RATE_WINDOW]
    if len(stamps) >= _CHECK_RATE_MAX:
        return True
    stamps.append(now)
    _check_rate[user_id] = stamps
    return False


@router.get("/health")
async def health() -> JSONResponse:
    """Здоровье сервиса — точка для внешнего мониторинга (UptimeRobot).

    200 — процесс жив и Telegram отвечал недавно; 503 — процесс жив, но
    Telegram недоступен (обычно умер прокси); таймаут/502 от реверс-прокси —
    умер контейнер или сервер. Пульс ведёт services/health.probe_loop.
    """
    alive = health_pulse.telegram_alive()
    age = health_pulse.last_ok_age()
    return JSONResponse(
        {"ok": alive, "telegram_last_ok_age_s": None if age is None else int(age)},
        status_code=200 if alive else 503,
    )


@router.get("/access")
async def access_state(request: Request) -> dict:
    """Есть ли у открывшего форму доступ к подаче и не висит ли его заявка.

    Подпись initData обязательна, whitelist — нет: смысл ручки в том, чтобы
    ответить тому, у кого доступа как раз и нет.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    # Заодно отдаём роли: приложение опрашивает эту ручку и по ней же
    # показывает или убирает панель финансиста и админские вкладки, когда
    # права поменяли в чате. is_bot_admin кэширует ответ Telegram на 5 минут,
    # так что опрос его не дёргает.
    return {
        "allowed": is_allowed(user["id"]),
        "financier": is_financier(user["id"]),
        "admin": await is_bot_admin(request.app.state.bot, user["id"]),
        "pending": rs.access_request_pending(user["id"]),
        "has_admins": bool(rs.effective_admin_ids()),
        # Пустая строка на боевом — плашки нет; на стенде «СТЕНД».
        "env_label": settings.env_label.strip()[:24],
        # Технические работы: плашку видят все, кто открыл форму.
        "maintenance": rs.maintenance_config(),
        # Живая инструкция: пока обкатывается на стенде.
        "animated_help": settings.animated_help,
    }


@router.post("/access/request")
async def access_request(request: Request) -> dict:
    """Просьба открыть доступ: уходит всем админам с кнопками решения."""
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if is_allowed(user["id"]):
        return {"ok": False, "message": "Доступ уже открыт."}
    if _my_rate_limited(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите минуту.")
    message = await request_access(
        request.app.state.bot,
        user["id"],
        user.get("username") or "",
        " ".join(filter(None, [user.get("first_name"), user.get("last_name")])),
    )
    return {"ok": True, "message": message}


@router.get("/counterparties")
async def counterparties(request: Request) -> dict:
    """Справочник контрагентов — подсказки-чипсы в форме.

    Вместе с именем отдаём последние известные реквизиты этого контрагента:
    чипс подставляет и то, и другое, чтобы реквизиты не набирались руками
    заново (частый источник опечаток в платёжке).
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not is_allowed(user["id"]):
        raise HTTPException(status_code=403, detail="Нет доступа.")
    return {"items": await storage.counterparty_book(limit=6)}


# ---------------------------------------------------------------------------
# «Мои заявки»
# ---------------------------------------------------------------------------
# Отдельный, щедрый лимит для чтения/отзыва своих заявок.
_MY_RATE_MAX = 20
_my_rate: dict[int, list[float]] = {}


def _my_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    stamps = [t for t in _my_rate.get(user_id, []) if now - t < _RATE_WINDOW]
    if len(stamps) >= _MY_RATE_MAX:
        return True
    stamps.append(now)
    _my_rate[user_id] = stamps
    return False


def _is_overdue(row: dict[str, str]) -> bool:
    """Срок оплаты прошёл, а заявка всё ещё ждёт.

    Просрочка — не статус в реестре, а вычисляемое состояние, и считается
    оно на сервере: `PENDING_STATUSES` и разбор даты живут здесь, а копия
    той же логики в JS стала бы четвёртым местом, которое разъедется.
    Раньше признак существовал только внутри фильтра «Просрочены», поэтому
    в списке заявка выглядела обычной «Новой» — узнать о сроке можно было,
    лишь заранее заподозрив и переключив фильтр.
    """
    if row.get("Статус оплаты", "") not in PENDING_STATUSES:
        return False
    planned = _parse_registry_date(row.get("Плановая дата оплаты", ""))
    if planned is None:
        return False
    return planned < datetime.now(ZoneInfo(settings.timezone)).date()


def _as_item(row: dict[str, str], reason: str) -> dict:
    """Строка реестра → элемент списка «Мои заявки».

    Путь/ссылку на файл наружу не отдаём — приложению достаточно признака
    «счёт был приложен».
    """
    return {
        "id": row.get("ID заявки", ""),
        "status": row.get("Статус оплаты", ""),
        "sender": row.get("Сотрудник по заявке", ""),
        "counterparty": row.get("Контрагент", ""),
        "amount": row.get("Сумма", ""),
        "currency": row.get("Валюта", ""),
        "article": row.get("Статья", ""),
        "comment": row.get("Комментарий", ""),
        "urgency": row.get("Срочность", ""),
        "planned_date": row.get("Плановая дата оплаты", ""),
        "created_at": row.get("Дата внесения в реестр", ""),
        "has_invoice": bool(row.get("Ссылка на счет", "")),
        # Три случая, а не два: «нет счёта» ещё не значит «есть реквизиты».
        "payment_source": (
            "invoice" if row.get("Ссылка на счет", "")
            else ("requisites" if row.get("Реквизиты", "") else "none")
        ),
        "requisites": row.get("Реквизиты", ""),
        "work_deadline": row.get("Срок исполнения работ по договору", ""),
        "overdue": _is_overdue(row),
        # Сколько закрывающих уже приложено — для подписи на кнопке.
        "closing_count": len(
            [x for x in (row.get(CLOSING_HEADER, "") or "").splitlines() if x.strip()]
        ),
        "reason": reason,
    }


async def _authorized_user(request: Request) -> dict:
    """Подпись initData + whitelist — как у подачи заявки."""
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not is_allowed(user["id"]):
        raise HTTPException(status_code=403, detail="Нет доступа.")
    if _my_rate_limited(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите минуту.")
    return user


@router.get("/my-requests")
async def my_requests(request: Request, request_id: str = "") -> dict:
    """Последние заявки автора (или одна конкретная — для повтора по ссылке).

    Выдаются ТОЛЬКО свои заявки: выборка идёт по проверенному id из initData,
    подставить чужой идентификатор нельзя.
    """
    user = await _authorized_user(request)
    try:
        rows = await storage.recent_by_author(user["id"], limit=MY_LIST_LIMIT, strict=True)
    except storage.RegistryUnavailable as exc:
        # Пустой список здесь читается как «заявок нет» — а это неправда.
        raise HTTPException(
            status_code=503,
            detail="Реестр сейчас недоступен — попробуйте ещё раз через минуту.",
        ) from exc
    if request_id:
        if not REQUEST_ID_RE.fullmatch(request_id):
            raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")
        rows = [r for r in rows if r.get("ID заявки", "") == request_id]
        if not rows:
            # Заявка могла выпасть за пределы окна — добираем точечно.
            row = await storage.get_request(request_id)
            if row is not None and row.get("Telegram ID", "") == str(user["id"]):
                rows = [row]
    reasons = await request_meta.reasons_for([r.get("ID заявки", "") for r in rows])
    return {
        "items": [_as_item(row, reasons.get(row.get("ID заявки", ""), "")) for row in rows]
    }


# ---------------------------------------------------------------------------
# Панель финансиста: все заявки с фильтрами
# ---------------------------------------------------------------------------
# Сколько последних заявок просматривает панель (фильтры применяются к ним).
FINANCE_SCAN_LIMIT = 500
# Сколько отдаём наружу после фильтрации.
FINANCE_PAGE_LIMIT = 100


def _parse_registry_date(value: str) -> date | None:
    """«2026-08-04 22:43» или «04.08.2026» → date (None — не разобрать)."""
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(raw[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    return None


# Значение фильтра «Просрочено»: не встречается среди статусов реестра.
OVERDUE_FILTER = "__overdue__"


def _matches(
    row: dict[str, str],
    *,
    status: str,
    urgency: str,
    query: str,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    if status == OVERDUE_FILTER:
        if not _is_overdue(row):
            return False
    elif status and row.get("Статус оплаты", "") != status:
        return False
    if urgency and row.get("Срочность", "") != urgency:
        return False
    if query:
        haystack = " ".join((
            row.get("Контрагент", ""), row.get("Сотрудник по заявке", ""),
            row.get("Статья", ""), row.get("Комментарий", ""), row.get("ID заявки", ""),
            row.get("Срок исполнения работ по договору", ""),
        )).lower()
        if query not in haystack:
            return False
    if date_from or date_to:
        # Фильтруем по ПЛАНОВОЙ дате оплаты: финансист планирует платежи.
        planned = _parse_registry_date(row.get("Плановая дата оплаты", ""))
        if planned is None:
            return False
        if date_from and planned < date_from:
            return False
        if date_to and planned > date_to:
            return False
    return True


@router.get("/finance/requests")
async def finance_requests(
    request: Request,
    status: str = "",
    urgency: str = "",
    query: str = "",
    date_from: str = "",
    date_to: str = "",
) -> dict:
    """Все заявки с фильтрами — только для финансистов.

    total_found считается по ВСЕЙ отфильтрованной выборке, а не по отданной
    странице: панель честно показывает, что список обрезан.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    # Строго по списку финансистов: админ, убравший себя оттуда, панель
    # теряет — иначе «убрал себя, а кнопка осталась». Нужен доступ —
    # добавьте себя финансистом (⚙️ → Финансисты).
    if not is_financier(user["id"]):
        await audit.log_event(
            audit.FINANCE_DENIED, user["id"], user.get("username"), "панель заявок"
        )
        raise HTTPException(status_code=403, detail="Панель доступна финансистам.")
    if _my_rate_limited(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите минуту.")

    try:
        from_date = parse_registry_filter_date(date_from)
        to_date = parse_registry_filter_date(date_to)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rows = await storage.recent_requests(limit=FINANCE_SCAN_LIMIT)
    found = [
        row for row in rows
        if _matches(
            row,
            status=status.strip(),
            urgency=urgency.strip(),
            query=query.strip().lower(),
            date_from=from_date,
            date_to=to_date,
        )
    ]

    page = found[:FINANCE_PAGE_LIMIT]
    reasons = await request_meta.reasons_for([r.get("ID заявки", "") for r in page])
    return {
        "items": [_as_item(row, reasons.get(row.get("ID заявки", ""), "")) for row in page],
        "total_found": len(found),
        "shown": len(page),
        "scanned": len(rows),
        "scan_limit": FINANCE_SCAN_LIMIT,
    }


@router.post("/finance/status")
async def finance_status(request: Request) -> dict:
    """Смена статуса из панели: {"request_id", "key": PAID|DEFERRED|REJECTED,
    "reason": "…"}.

    Тот же сценарий, что у кнопок на карточке в чате (services.status_change):
    реестр, карточки всех финансистов, причина, аудит, уведомление автору.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not is_financier(user["id"]):
        await audit.log_event(
            audit.STATUS_DENIED, user["id"], user.get("username"), "панель заявок"
        )
        raise HTTPException(status_code=403, detail="Статусы меняют финансисты.")
    if _my_rate_limited(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите минуту.")

    body = await request.json()
    request_id = str(body.get("request_id", "")).strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")
    key = str(body.get("key", "")).strip()
    if key not in REQUEST_STATUSES:
        raise HTTPException(status_code=422, detail="Неизвестный статус.")
    reason = str(body.get("reason", "")).strip()[:300]

    username = user.get("username")
    ok, message = await apply_status(
        request.app.state.bot,
        request_id,
        key,
        actor_id=user["id"],
        actor_name=f"@{username}" if username else str(user["id"]),
        reason=reason or None,
    )
    return {"ok": ok, "message": message}


@router.get("/finance/access")
async def finance_access(request: Request) -> dict:
    """Показывать ли кнопку панели: ровно то же условие, что и у выборки."""
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    return {"ok": is_financier(user["id"])}


@router.post("/requests/delete")
async def delete_request_route(request: Request) -> dict:
    """Удаление заявки: {"request_id": "INV-…"}.

    Права разбирает services.deletion: админ — любую, автор — только свою
    отозванную. Удаление необратимо и всегда пишется в аудит.
    """
    user = await _authorized_user(request)
    body = await request.json()
    request_id = str(body.get("request_id", "")).strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")

    username = user.get("username")
    ok, message = await delete_request_service(
        request.app.state.bot,
        request_id,
        actor_id=user["id"],
        actor_name=f"@{username}" if username else str(user["id"]),
        is_admin=await is_bot_admin(request.app.state.bot, user["id"]),
    )
    return {"ok": ok, "message": message}


@router.post("/my/withdraw")
async def my_withdraw(request: Request) -> dict:
    """Отзыв своей заявки: {"request_id": "INV-…"}."""
    user = await _authorized_user(request)
    body = await request.json()
    request_id = str(body.get("request_id", "")).strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")

    username = user.get("username")
    ok, message = await withdraw_request(
        request.app.state.bot,
        request_id,
        actor_id=user["id"],
        actor_name=f"@{username}" if username else str(user["id"]),
    )
    return {"ok": ok, "message": message}


# Как часто автор может напомнить по ОДНОЙ заявке. Шесть часов, а не сутки:
# внутри рабочего дня уместно поторопить утром и ещё раз к вечеру, но не
# чаще — иначе кнопка превращается в способ забрасывать бухгалтерию.
NUDGE_INTERVAL_SECONDS = 6 * 3600


@router.post("/my/nudge")
async def my_nudge(request: Request) -> dict:
    """Напоминание финансистам о просрочке по своей заявке: {"request_id": …}.

    Автор видит, что срок прошёл, а денег нет, — и до сих пор мог только
    писать финансисту лично, мимо бота. Теперь просьба уходит всем
    получателям, с кнопками статуса, и попадает в аудит.

    «Просрочена» проверяется на сервере: признак вычисляемый, и доверять
    тому, что прислало приложение, нельзя — иначе напоминание можно было
    бы слать по любой заявке.
    """
    user = await _authorized_user(request)
    body = await request.json()
    request_id = str(body.get("request_id", "")).strip()
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")

    row = await storage.get_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена в реестре.")
    if row.get("Telegram ID", "") != str(user["id"]) and not await is_bot_admin(
        request.app.state.bot, user["id"]
    ):
        await audit.log_event(
            audit.WITHDRAW_DENIED, user["id"], user.get("username"),
            f"{request_id}: чужая заявка, напоминание о просрочке",
        )
        raise HTTPException(status_code=403, detail="Напомнить можно только по своей заявке.")
    if not _is_overdue(row):
        raise HTTPException(
            status_code=422,
            detail="Срок оплаты ещё не прошёл — напоминать пока не о чем.",
        )

    left = await request_meta.claim_nudge(request_id, NUDGE_INTERVAL_SECONDS)
    if left > 0:
        hours = max(1, round(left / 3600))
        raise HTTPException(
            status_code=429,
            detail=f"По этой заявке уже напомнили. Следующее — через {hours} ч.",
        )

    planned = _parse_registry_date(row.get("Плановая дата оплаты", ""))
    days = (datetime.now(ZoneInfo(settings.timezone)).date() - planned).days
    delivered = await overdue_nudge(
        request.app.state.bot, request_id, row, user.get("username"), days
    )
    if not delivered:
        # Никому не дошло — отметку снимаем, иначе человек шесть часов
        # думал бы, что напоминание ушло.
        await request_meta.forget_nudge(request_id)
        raise HTTPException(
            status_code=502, detail="Не удалось доставить напоминание. Попробуйте позже."
        )
    await audit.log_event(
        audit.REQUEST_SUBMITTED, user["id"], user.get("username"),
        f"{request_id}: напоминание о просрочке ({days} дн.), получателей {delivered}",
    )
    return {
        "ok": True,
        "message": f"Напомнили: получателей — {delivered}.",
    }


CLOSING_HEADER = "Закрывающие документы"


@router.post("/my/closing-docs")
async def my_closing_docs(
    request: Request,
    request_id: str = Form(...),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    """Закрывающие документы к УЖЕ поданной заявке: акт, УПД, накладная.

    Приходят после оплаты, иногда через месяц, поэтому дописываются в
    существующую строку реестра, а не в новую заявку. Прикладывает АВТОР
    (у него они и есть) — или админ; чужую заявку так не дополнить, иначе
    к платежу можно было бы подшить чей угодно документ.

    Дописываем к тому, что уже есть: документы носят частями, и вторая
    загрузка не должна стирать первую.
    """
    user = await _authorized_user(request)
    if not REQUEST_ID_RE.fullmatch(request_id.strip()):
        raise HTTPException(status_code=422, detail="Некорректный идентификатор заявки.")
    request_id = request_id.strip()

    row = await storage.get_request(request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Заявка не найдена в реестре.")
    is_admin = await is_bot_admin(request.app.state.bot, user["id"])
    if row.get("Telegram ID", "") != str(user["id"]) and not is_admin:
        await audit.log_event(
            audit.DELETE_DENIED, user["id"], user.get("username"),
            f"{request_id}: чужая заявка, закрывающие документы",
        )
        raise HTTPException(
            status_code=403, detail="Дополнить можно только свою заявку."
        )

    picked = [f for f in (files or []) if f is not None and f.filename]
    if not picked:
        raise HTTPException(status_code=422, detail="Прикрепите хотя бы один документ.")
    already = [x for x in (row.get(CLOSING_HEADER, "") or "").splitlines() if x.strip()]
    if len(already) + len(picked) > MAX_EXTRA_FILES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Закрывающих документов не больше {MAX_EXTRA_FILES}: "
                f"уже приложено {len(already)}."
            ),
        )

    saved: list[str] = []
    for position, item in enumerate(picked, start=len(already) + 1):
        blob = await _read_limited(item, MAX_FILE_SIZE_BYTES)
        if len(blob) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=422, detail=f"«{item.filename}» больше 20 МБ.")
        try:
            validate_file(item.content_type, len(blob))
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"«{item.filename}»: {exc}"
            ) from exc
        name = build_extra_filename(item.filename, f"{request_id}-закр", position)
        saved.append(await storage.save_invoice(blob, name))

    updated = await storage.set_request_field(
        request_id, CLOSING_HEADER, "\n".join(already + saved)
    )
    if updated is None:
        raise HTTPException(status_code=500, detail="Не удалось записать в реестр.")

    await audit.log_event(
        audit.REQUEST_SUBMITTED, user["id"], user.get("username"),
        f"{request_id}: закрывающих документов +{len(saved)}",
    )
    await closing_docs_notify(
        request.app.state.bot, request_id, row, saved, user.get("username")
    )
    return {
        "ok": True,
        "count": len(already) + len(saved),
        "message": f"Готово: документов у заявки — {len(already) + len(saved)}.",
    }


# ---------------------------------------------------------------------------
# Админ-панель Mini App
# ---------------------------------------------------------------------------
async def _require_admin(request: Request) -> dict:
    """Админ = ADMIN_IDS из .env или администратор доверенного канала/группы."""
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not await is_bot_admin(request.app.state.bot, user["id"]):
        await audit.log_event(
            audit.ADMIN_DENIED, user["id"], user.get("username"), request.url.path
        )
        raise HTTPException(status_code=403, detail="Только для администраторов бота.")
    return user


@router.get("/admin/settings")
async def admin_settings(request: Request) -> dict:
    """Текущие настройки для админ-панели."""
    await _require_admin(request)
    # Показываем ЭФФЕКТИВНЫЙ список: отключённые из панели записи .env
    # пропадают отсюда так же, как и удалённые динамические.
    dynamic = set(rs.dynamic_finance())
    financiers = [
        {"entry": x, "source": "dynamic" if x in dynamic else "env"}
        for x in rs.effective_finance_recipients()
    ]
    disabled = set(rs.disabled_allowed())
    allowed = [
        {"id": i, "source": "env", "username": username_for(i)}
        for i in settings.allowed_user_ids
        if i not in disabled
    ] + [
        {"id": i, "source": "dynamic", "username": username_for(i)}
        for i in rs.dynamic_allowed()
    ]
    dyn_admins = set(rs.dynamic_admins())
    admins = [
        {"id": i, "source": "dynamic" if i in dyn_admins else "env",
         "username": username_for(i)}
        for i in rs.effective_admin_ids()
    ]
    registry_url, drive_url = _registry_links()
    return {
        "autofill": rs.autofill_enabled(),
        "financiers": financiers,
        "allowed": allowed,
        "org_name": settings.org_name,
        "backup": {**rs.backup_config(), "archive": backup.describe()},
        # Плашка работ — тем же ответом: отдельный запрос ради двух полей
        # не нужен, а читать состояние методом POST было бы просто неверно.
        "maintenance": rs.maintenance_config(),
        "reminders": rs.reminders_config(),
        "registry_url": registry_url,
        "drive_url": drive_url,
        "admins": admins,
        # Здоровье бота: настройки уведомлений, живое состояние связи и
        # журнал последних сбоев — панель открывается и тогда, когда
        # Telegram недоступен, так что это единственный надёжный экран.
        "alerts": rs.alerts_config(),
        "alert_kinds": [
            {"key": key, "title": title, "critical": critical}
            for key, title, _default, critical in rs.ALERT_KINDS
        ],
        "health": health_pulse.link_state(),
        "incidents": rs.recent_incidents(6),
        "incidents_day": rs.incidents_since(time.time() - 86400),
        # Расхождение реестра и зеркала иначе не видно ниоткуда: сбой записи
        # в зеркало намеренно не отменяет заявку и живёт только в логах.
        "registry": await _registry_state(),
    }


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


@router.get("/admin/users")
async def admin_users(request: Request) -> dict:
    """Кто уже пользуется ботом и у кого открыта подача заявок.

    Собирается из справочника (всех, кто хоть раз обращался к боту) плюс id
    из whitelist, которые справочнику ещё не попадались. Права админа здесь
    определяются только по ADMIN_IDS: администраторы доверенных чатов
    вычисляются запросом в Telegram на каждого, и перечислить их списком
    нельзя — об этом сказано в подсказке карточки.
    """
    await _require_admin(request)
    known = await asyncio.to_thread(all_users)
    allowed = set(await asyncio.to_thread(rs.effective_allowed_ids))
    dynamic = set(rs.dynamic_allowed())
    admins = set(await asyncio.to_thread(rs.effective_admin_ids))
    dyn_admins = set(rs.dynamic_admins())
    fin_ids = await asyncio.to_thread(_finance_ids)

    names = dict(known)
    users = []
    for uid in sorted({*names, *allowed, *admins, *fin_ids}):
        # Отрицательный id — это чат (FINANCE_CHAT_IDS принимает и группы), а
        # не человек; сам бот в справочник больше не попадает, но старые
        # записи могли остаться — их тоже убираем.
        if uid <= 0 or uid == settings.bot_id:
            continue
        users.append({
            "id": uid,
            "username": names.get(uid),
            "admin": uid in admins,
            "admin_source": ("dynamic" if uid in dyn_admins else "env") if uid in admins else None,
            "financier": uid in fin_ids,
            "access": ("dynamic" if uid in dynamic else "env") if uid in allowed else None,
        })
    # Пустой whitelist = подача закрыта всем, кроме админов (fail-closed).
    return {"users": users, "whitelist_empty": not allowed}


def _finance_ids() -> set[int]:
    """id финансистов; импорт локальный — notifier тянет пол-сервисного слоя."""
    from services.notifier import resolved_finance_ids

    return set(resolved_finance_ids())


@router.post("/admin/restore")
async def admin_restore(
    request: Request,
    file: UploadFile = File(...),
    action: str = Form("inspect"),
) -> dict:
    """Восстановление из загруженного архива: сначала «покажи», потом «ставь».

    Операция пишет поверх живых данных, поэтому она admin-only, двухшаговая
    и обязательно оставляет за собой страховочную копию. Разбор архива —
    в services/restore, здесь только права, размер и аудит.
    """
    user = await _require_admin(request)
    blob = await _read_limited(file, restore.MAX_ARCHIVE_BYTES)
    if len(blob) > restore.MAX_ARCHIVE_BYTES:
        raise HTTPException(status_code=413, detail="Архив слишком большой.")

    try:
        if action == "inspect":
            summary = await asyncio.to_thread(restore.inspect_sync, blob)
            return {"ok": True, "summary": summary}
        if action == "apply":
            summary = await asyncio.to_thread(restore.apply_sync, blob)
            await asyncio.to_thread(restore.reload_caches)
            await audit.log_event(
                audit.RESTORE_APPLIED,
                user["id"],
                user.get("username"),
                f"архив от {summary['made_at']}: заявок {summary['requests']}, "
                f"файлов {summary['files']}; копия до — {summary['safety_backup']}",
            )
            await alerts.alert_admins(
                request.app.state.bot,
                "Данные восстановлены из архива",
                f"@{user.get('username') or user['id']} поставил архив от "
                f"{summary['made_at']}. Копия прежнего состояния: "
                f"{summary['safety_backup']}.",
                signature="restore-applied",
                kind="storage",
                hint="Если это не вы — состояние до восстановления лежит в data/backups/.",
            )
            return {
                "ok": True,
                "summary": summary,
                "message": (
                    f"Восстановлено из архива от {summary['made_at']}. "
                    f"Копия прежнего состояния: {summary['safety_backup']}."
                ),
            }
    except restore.RestoreError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    raise HTTPException(status_code=422, detail="Некорректный запрос.")


@router.post("/admin/backup")
async def admin_backup(request: Request) -> dict:
    """Управление бэкапом: {"action": "save", enabled, time, keep} | {"action": "run"}."""
    user = await _require_admin(request)
    body = await request.json()
    action = body.get("action")

    if action == "run":
        try:
            path, delivered = await backup.run_backup(request.app.state.bot)
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой бэкапа из админ-панели")
            raise HTTPException(status_code=500, detail="Не удалось собрать бэкап.") from exc
        message = (
            "Бэкап собран и отправлен в чат с ботом."
            if delivered
            else f"Бэкап собран ({path.name}), но отправить файлом не удалось."
        )
        return {"ok": bool(delivered), "message": message}

    if action == "save":
        time_value = str(body.get("time", "")).strip()
        if time_value and not _TIME_RE.fullmatch(time_value):
            raise HTTPException(status_code=422, detail="Время — в формате ЧЧ:ММ.")
        keep_raw = str(body.get("keep", "")).strip()
        keep_value: int | None = None
        if keep_raw:
            if not keep_raw.isdigit() or not 1 <= int(keep_raw) <= 60:
                raise HTTPException(
                    status_code=422, detail="Число копий — целое от 1 до 60."
                )
            keep_value = int(keep_raw)
        enabled = bool(body.get("enabled"))

        cfg = await asyncio.to_thread(
            rs.set_backup_config,
            enabled=enabled,
            time=time_value or None,
            keep=keep_value,
        )
        await audit.log_event(
            audit.BACKUP_SETTINGS,
            user["id"],
            user.get("username"),
            f"enabled={cfg['enabled']} time={cfg['time']} keep={cfg['keep']}",
        )
        state = "включён" if cfg["enabled"] else "выключен"
        return {
            "ok": True,
            "message": f"Бэкап {state}: ежедневно в {cfg['time']}, хранить {cfg['keep']}.",
            "backup": cfg,
        }

    raise HTTPException(status_code=422, detail="Некорректный запрос.")


async def _registry_state() -> dict:
    """Сверка реестра с зеркалом для панели: результат плюс готовая строка."""
    result = await registry_check.check()
    return {**result, "text": registry_check.describe(result)}


# Клиентские ошибки: одна и та же ошибка от одного человека — не чаще раза
# в это окно. Ключ включает текст, а не только id: сломанная страница сыплет
# одним исключением на каждое нажатие, но две РАЗНЫЕ поломки у одного
# человека — это две новости, и вторую терять нельзя. От спама одинаковой
# ошибкой сразу у многих защищает уже троттлинг по сигнатуре в alerts.
_CLIENT_ERROR_WINDOW = 300.0
_CLIENT_ERROR_MAX_KEYS = 500
_client_error_seen: dict[tuple[int, str], float] = {}


@router.post("/client-error")
async def client_error(request: Request) -> dict:
    """Сообщение фронтенда о своём падении: {"message": ..., "where": ...}.

    Серверная часть теперь докладывает админам обо всём, а исключение в
    браузере оставляло человека перед застывшей формой молча. Доступно
    любому с подписью Telegram: ошибка интересна именно у того, кто не
    смог подать заявку.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    body = await request.json()
    message = str(body.get("message", ""))[:200].strip()
    where = str(body.get("where", ""))[:120].strip()
    if not message:
        raise HTTPException(status_code=422, detail="Пустое сообщение об ошибке.")

    now = time.monotonic()
    key = (user["id"], message[:60])
    last = _client_error_seen.get(key)
    if last is not None and now - last < _CLIENT_ERROR_WINDOW:
        return {"ok": True, "throttled": True}
    if len(_client_error_seen) > _CLIENT_ERROR_MAX_KEYS:
        stale = [k for k, t in _client_error_seen.items() if now - t > _CLIENT_ERROR_WINDOW]
        for k in stale:
            del _client_error_seen[k]
    _client_error_seen[key] = now

    who = user.get("username") or user.get("first_name") or user["id"]
    await alerts.alert_admins(
        request.app.state.bot,
        "Ошибка в форме у пользователя",
        f"@{who} (id {user['id']}): {message}" + (f" — {where}" if where else ""),
        signature=f"client-error-{message[:60]}",
        kind="error",
        hint="Ошибка в браузере у пользователя, а не на сервере.",
    )
    return {"ok": True}


@router.post("/admin/alerts")
async def admin_alerts(request: Request) -> dict:
    """Уведомления о сбоях: {"action": "save"|"test"|"status", ...}.

    save   — режим, категории и порог по связи;
    test   — проверочное сообщение ТОЛЬКО нажавшему: канал «бот → админ»
             живёт отдельно от всего остального и проверяется отдельно;
    status — свежее состояние связи и журнал без перезагрузки всей панели.
    """
    user = await _require_admin(request)
    body = await request.json()
    action = body.get("action")

    if action == "status":
        return {
            "ok": True,
            "health": health_pulse.link_state(),
            "incidents": rs.recent_incidents(6),
            "incidents_day": rs.incidents_since(time.time() - 86400),
            "registry": await _registry_state(),
        }

    if action == "test":
        sent = await alerts.send_test_alert(request.app.state.bot, user["id"])
        if not sent:
            # Обычно это значит, что человек не открывал чат с ботом.
            raise HTTPException(
                status_code=502,
                detail="Не удалось доставить: напишите боту в личку /start и повторите.",
            )
        return {"ok": True, "message": "Проверочное уведомление отправлено вам в чат."}

    if action == "save":
        grace_raw = str(body.get("link_grace_min", "")).strip()
        grace: int | None = None
        if grace_raw:
            if not grace_raw.isdigit() or not (
                rs.LINK_GRACE_MIN <= int(grace_raw) <= rs.LINK_GRACE_MAX
            ):
                raise HTTPException(
                    status_code=422,
                    detail=f"Порог по связи — целое от {rs.LINK_GRACE_MIN} "
                           f"до {rs.LINK_GRACE_MAX} минут.",
                )
            grace = int(grace_raw)
        raw_kinds = body.get("kinds")
        kinds = (
            {k: bool(v) for k, v in raw_kinds.items() if k in rs.ALERT_KEYS}
            if isinstance(raw_kinds, dict)
            else None
        )
        cfg = await asyncio.to_thread(
            rs.set_alerts_config,
            enabled=bool(body.get("enabled")),
            kinds=kinds,
            link_grace_min=grace,
        )
        off = [k for k, v in cfg["kinds"].items() if not v]
        await audit.log_event(
            audit.ALERT_SETTINGS,
            user["id"],
            user.get("username"),
            f"enabled={cfg['enabled']} grace={cfg['link_grace_min']} off={','.join(off) or '-'}",
        )
        message = (
            f"Уведомления о сбоях: {'все включённые категории' if cfg['enabled'] else 'только критичные'}"
            f", порог по связи {cfg['link_grace_min']} мин."
        )
        return {"ok": True, "message": message, "alerts": cfg}

    raise HTTPException(status_code=422, detail="Некорректный запрос.")


def _registry_links() -> tuple[str | None, str | None]:
    """Ссылки на реестр и папку счетов. Источник один — services/storage."""
    return storage.registry_links()


@router.get("/registry/links")
async def registry_links(request: Request) -> dict:
    """Ссылки на реестр и папку счетов — финансистам и админам.

    Финансист работает с этим реестром каждый день, но полная админ-панель
    ему не положена: там состав админов, whitelist и бэкапы. Поэтому ссылки
    отдаём отдельной ручкой, а не открываем ему `/admin/settings`.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    uid = user["id"]
    if not (is_financier(uid) or await is_bot_admin(request.app.state.bot, uid)):
        raise HTTPException(status_code=403, detail="Только для финансистов и админов.")
    registry, drive = _registry_links()
    return {"registry_url": registry, "drive_url": drive}


@router.get("/autofill/me")
async def my_autofill(request: Request) -> dict:
    """Личный выключатель чтения счёта. Доступен ЛЮБОМУ, у кого есть доступ.

    Бета есть бета: тот, кому распознавание мешает, отключает его себе сам,
    не обращаясь к админу. Общая настройка остаётся значением по умолчанию
    и главным выключателем — выключенная, она гасит функцию у всех.
    """
    user = await _authorized_user(request)
    return {
        "enabled": rs.personal_autofill(user["id"]),
        "available": rs.autofill_enabled(),
    }


@router.post("/autofill/me")
async def set_my_autofill(request: Request, enabled: str = Form(...)) -> dict:
    user = await _authorized_user(request)
    value = enabled.strip().lower()
    if value not in ("1", "0", "default"):
        raise HTTPException(status_code=422, detail="Некорректное значение.")
    await asyncio.to_thread(
        rs.set_personal_autofill, user["id"], None if value == "default" else value == "1"
    )
    return {
        "enabled": rs.personal_autofill(user["id"]),
        "available": rs.autofill_enabled(),
    }


@router.get("/reminders/me")
async def my_reminders(request: Request) -> dict:
    """Личные настройки напоминаний того, кто открыл форму.

    Доступ — получателям: финансистам и админам. Остальным напоминать нечего.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    uid = user["id"]
    if not (is_financier(uid) or await is_bot_admin(request.app.state.bot, uid)):
        raise HTTPException(status_code=403, detail="Только для финансистов и админов.")
    cfg = rs.personal_reminders(uid)
    cfg["defaults"] = rs.reminders_config()
    cfg["financier"] = is_financier(uid)
    # Едет вместе с напоминаниями, хотя настройка не о них: это один экран
    # «мои настройки получателя» и одна кнопка «Сохранить». Заводить второй
    # запрос ради одного поля — лишний круг по нестабильному каналу.
    cfg["card_urgency"] = rs.personal_card_urgency(uid)
    return cfg


@router.post("/reminders/me")
async def save_my_reminders(request: Request) -> dict:
    """Сохранить свои настройки или вернуться к общим ({"action": "reset"})."""
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    uid = user["id"]
    if not (is_financier(uid) or await is_bot_admin(request.app.state.bot, uid)):
        raise HTTPException(status_code=403, detail="Только для финансистов и админов.")
    body = await request.json()
    if body.get("action") == "reset":
        cfg = await asyncio.to_thread(rs.clear_personal_reminders, uid)
        return {"ok": True, "message": "Вернул настройки по умолчанию.", "reminders": cfg}

    if body.get("action") == "test":
        # Прогон на себе: приходит только тому, кто нажал, и не ждёт расписания.
        try:
            rows = await storage.recent_requests(limit=REMINDER_SCAN_LIMIT, strict=True)
        except storage.RegistryUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail="Реестр сейчас недоступен — попробуйте ещё раз через минуту.",
            ) from exc
        today = datetime.now(ZoneInfo(settings.timezone)).date()
        due, overdue = await send_reminder_to(
            request.app.state.bot, uid, rows, today, force=True
        )
        if not due and not overdue:
            message = "Напоминать не о чем: ни ближайших, ни просроченных заявок."
        else:
            message = f"Прислал вам: к оплате {due}, просрочено {overdue}."
        return {"ok": True, "message": message}

    time_value = str(body.get("time", "")).strip()
    if time_value and not _TIME_RE.fullmatch(time_value):
        raise HTTPException(status_code=422, detail="Время — в формате ЧЧ:ММ.")
    days_raw = str(body.get("days_before", "")).strip()
    days = None
    if days_raw:
        if not days_raw.isdigit() or not 0 <= int(days_raw) <= 14:
            raise HTTPException(status_code=422, detail="«За сколько дней» — число 0…14.")
        days = int(days_raw)
    card_urgency = str(body.get("card_urgency", "")).strip()
    if card_urgency:
        if card_urgency not in rs.CARD_URGENCY_CHOICES:
            raise HTTPException(status_code=422, detail="Некорректный фильтр срочности.")
        await asyncio.to_thread(rs.set_personal_card_urgency, uid, card_urgency)
    cfg = await asyncio.to_thread(
        rs.set_personal_reminders,
        uid,
        enabled=bool(body.get("enabled", True)),
        time=time_value or None,
        days_before=days,
        due_enabled=bool(body.get("due_enabled", True)),
        overdue_enabled=bool(body.get("overdue_enabled", True)),
        weekdays_only=bool(body.get("weekdays_only", False)),
    )
    cfg["card_urgency"] = rs.personal_card_urgency(uid)
    # Предупреждение, а не запрет: если ВСЕ получатели просят только срочные,
    # обычные заявки не придут никому. Заявка остаётся в реестре и видна
    # в панели, но пуш-уведомления о ней исчезают — и никакой алерт об этом
    # не скажет. Человек должен узнать это в момент, когда сам это включает.
    message = "Настройки сохранены."
    if cfg["card_urgency"] == rs.CARD_URGENCY_URGENT:
        others = [i for i in notifier.resolved_finance_ids() if i != uid]
        if not any(
            rs.personal_card_urgency(i) == rs.CARD_URGENCY_ALL for i in others
        ):
            message += (
                " Внимание: теперь ни один получатель не получает обычные "
                "заявки — только срочные. Они по-прежнему в реестре и в панели."
            )
    return {"ok": True, "message": message, "reminders": cfg}


@router.post("/admin/maintenance")
async def admin_maintenance(request: Request) -> dict:
    """Плашка «технические работы»: {"enabled": bool, "text": "…"}.

    Подачу НЕ блокирует: заявка всё равно уходит в реестр, а плашка
    предупреждает, что ответ может задержаться. Молча не принять
    заполненную форму было бы хуже, чем принять её во время работ.
    """
    user = await _require_admin(request)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    text = body.get("text")
    cfg = await asyncio.to_thread(
        rs.set_maintenance,
        enabled=enabled,
        text=None if text is None else str(text),
    )
    await audit.log_event(
        audit.MAINTENANCE,
        user["id"],
        user.get("username"),
        f"технические работы: {'включены' if enabled else 'сняты'}",
    )
    return {
        "ok": True,
        "message": "Плашка включена." if enabled else "Плашка снята.",
        "maintenance": cfg,
    }


@router.post("/admin/autofill")
async def admin_autofill(request: Request) -> dict:
    """Бета-тумблер автозаполнения: {"enabled": true|false}."""
    user = await _require_admin(request)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    value = await asyncio.to_thread(rs.set_autofill, enabled)
    await audit.log_event(
        audit.BETA_SETTINGS, user["id"], user.get("username"), f"autofill={value}"
    )
    return {
        "ok": True,
        "autofill": value,
        "message": ("Автозаполнение включено." if value
                    else "Автозаполнение выключено — форма заполняется вручную."),
    }


@router.post("/admin/financiers")
async def admin_financiers(request: Request) -> dict:
    """Добавить/убрать финансиста: {"action": "add"|"remove", "entry": "@user"|"123"}."""
    await _require_admin(request)
    body = await request.json()
    action, entry = body.get("action"), str(body.get("entry", "")).strip()
    if action not in ("add", "remove") or not entry:
        raise HTTPException(status_code=422, detail="Некорректный запрос.")
    if action == "add":
        if not rs.valid_financier_entry(entry):
            raise HTTPException(
                status_code=422,
                detail="Нужен числовой id или @username (5–32 символа: буквы, цифры, «_»).",
            )
        changed = await asyncio.to_thread(rs.add_financier, entry)
        message = "Финансист добавлен." if changed else "Уже в списке."
    else:
        changed = await asyncio.to_thread(rs.remove_financier, entry)
        message = "Финансист убран." if changed else "Такого финансиста нет в списке."
    return {"ok": changed, "message": message}


@router.post("/admin/allowed")
async def admin_allowed(request: Request) -> dict:
    """Открыть/закрыть доступ: {"action": "add"|"remove", "entry": "@user"|"123"}."""
    await _require_admin(request)
    body = await request.json()
    action, entry = body.get("action"), str(body.get("entry", "")).strip()
    if action not in ("add", "remove") or not entry:
        raise HTTPException(status_code=422, detail="Некорректный запрос.")

    uid = int(entry) if entry.lstrip("-").isdigit() else resolve(entry)
    if uid is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Не знаю id пользователя {entry}: пусть он напишет боту /start, "
                "или укажите числовой id (команда /myid)."
            ),
        )
    if action == "add":
        changed = await asyncio.to_thread(rs.add_allowed, uid)
        message = f"Доступ открыт (id {uid})." if changed else "Уже в whitelist."
    else:
        changed = await asyncio.to_thread(rs.remove_allowed, uid)
        message = (
            f"Доступ закрыт (id {uid})." if changed
            else "Нет в динамическом whitelist (записи из .env правятся на сервере)."
        )
    return {"ok": changed, "message": message}


@router.post("/admin/admins")
async def admin_admins(request: Request) -> dict:
    """Назначить/снять админа: {"action": "add"|"remove", "entry": "@user"|"123"}.

    Последнего админа снять нельзя: иначе настройками бота станет некому
    управлять, а вернуть права можно будет только правкой .env на сервере.
    """
    actor = await _require_admin(request)
    body = await request.json()
    action, entry = body.get("action"), str(body.get("entry", "")).strip()
    if action not in ("add", "remove") or not entry:
        raise HTTPException(status_code=422, detail="Некорректный запрос.")

    uid = int(entry) if entry.lstrip("-").isdigit() else resolve(entry)
    if uid is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Не знаю id пользователя {entry}: пусть он напишет боту /start, "
                "или укажите числовой id (команда /myid)."
            ),
        )
    if action == "add":
        changed = await asyncio.to_thread(rs.add_admin, uid)
        message = f"Назначен админом (id {uid})." if changed else "Уже админ."
    else:
        if uid in settings.admin_ids:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Этот админ задан в .env на сервере — снять права можно "
                    "только там. Так владелец не теряет контроль над ботом."
                ),
            )
        if len(await asyncio.to_thread(rs.effective_admin_ids)) <= 1:
            raise HTTPException(
                status_code=409,
                detail="Это последний админ — снять права будет некому вернуть.",
            )
        changed = await asyncio.to_thread(rs.remove_admin, uid)
        message = f"Права админа сняты (id {uid})." if changed else "Этот человек не админ."
    # Смена состава админов — событие для журнала безопасности.
    await audit.log_event(
        audit.ADMIN_ROLE, actor["id"], actor.get("username"), f"{action} {uid}"
    )
    return {"ok": changed, "message": message}


async def _read_limited(file: UploadFile, limit: int) -> bytes:
    """Читает вложение, но не больше limit+1 байта.

    Раньше файл читался целиком и ТОЛЬКО потом сверялся с лимитом: защита
    стояла после того, как данные уже в памяти. Практический потолок задавал
    Caddy (max_size 25MB), но полагаться на обратный прокси в вопросе
    собственной памяти неправильно. Читаем кусками и обрываемся на первом
    превышении — вернувшийся объём на единицу больше лимита и есть признак
    «слишком большой».
    """
    chunks: list[bytes] = []
    read = 0
    while read <= limit:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        read += len(chunk)
    return b"".join(chunks)[: limit + 1]


@router.post("/check-file")
async def check_file(
    request: Request,
    amount: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    """Мгновенная предпроверка вложения при прикреплении в форме.

    Показывает пользователю «не похоже на счёт» ещё ДО отправки заявки.
    Авторитетная проверка всё равно выполняется при submit.
    """
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not is_allowed(user["id"]):
        raise HTTPException(status_code=403, detail="Нет доступа.")
    if _check_rate_limited(user["id"]):
        raise HTTPException(status_code=429, detail="Слишком часто — подождите минуту.")

    content = await _read_limited(file, MAX_FILE_SIZE_BYTES)
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(status_code=422, detail="Файл больше 20 МБ.")

    expected = None
    if amount.strip():
        try:
            expected = parse_amount(amount)
        except ValidationError:
            expected = None  # сумма ещё не введена/битая — проверяем без бонуса

    warning, text = await asyncio.to_thread(
        invoice_check.inspect_invoice_file, content, file.filename or "", expected
    )
    # Бета: разбираем УЖЕ распознанный текст — второго прогона OCR нет.
    # Форма ничего не подставляет сама, только показывает предложение.
    autofill: dict = {}
    if rs.personal_autofill(user["id"]) and text:
        autofill = await asyncio.to_thread(invoice_extract.extract_fields, text)
    return {"warning": warning, "autofill": autofill}


@router.post("/invoice")
async def submit_invoice(
    request: Request,
    amount: str = Form(...),
    currency: str = Form(...),
    counterparty: str = Form(...),
    article: str = Form(...),
    planned_date: str = Form(...),
    work_deadline: str = Form(""),
    comment: str = Form(""),
    urgency: str = Form(...),
    has_invoice: str = Form(...),
    requisites: str = Form(""),
    return_chat: str = Form(""),
    force: str = Form("0"),
    confirm_text: str = Form("0"),
    file: UploadFile | None = File(None),
    extra_files: list[UploadFile] = File(default=[]),
):
    # --- Аутентификация и доступ -------------------------------------------
    user = validate_init_data(
        request.headers.get("X-Telegram-Init-Data", ""), settings.telegram_bot_token
    )
    if not is_allowed(user["id"]):
        await audit.log_event(
            audit.ACCESS_DENIED, user["id"], user.get("username"), "mini app"
        )
        raise HTTPException(
            status_code=403,
            detail="У вас нет доступа к подаче заявок. Обратитесь к администратору.",
        )
    if _rate_limited(user["id"]):
        await audit.log_event(
            audit.RATE_LIMITED, user["id"], user.get("username"), "mini app"
        )
        raise HTTPException(
            status_code=429,
            detail="Слишком много заявок подряд — подождите минуту и попробуйте снова.",
        )

    # --- Валидация полей (та же, что в чат-форме) ---------------------------
    try:
        amount_value = parse_amount(amount)
        counterparty_value = validate_text_field(
            counterparty, field_name="Контрагент", max_len=200
        )
        article_value = validate_text_field(article, field_name="Статья", max_len=100)
        # "auto" — дату считает сервер по срочности (в TIMEZONE, не в браузере).
        planned_value = (
            None if planned_date.strip() in ("", "auto")
            else parse_planned_date(planned_date)
        )
        # Срок исполнения — свободный текст («текущий месяц», «поставка
        # в декабре»), поэтому датой не разбирается: только длина и пробелы.
        # Поле ОБЯЗАТЕЛЬНОЕ: пустое значение отклоняем.
        work_deadline_value = validate_line_field(
            work_deadline,
            field_name="Срок исполнения работ по договору",
            max_len=200,
            required=True,
        )
        # Слой безошибочных правил: название без единой буквы не существует,
        # как и контрагент из одного символа. Отказываем сразу — ошибиться
        # эти правила не могут. У срока работ буквы не требуем: его законно
        # пишут датой.
        for label, value, need_letter in (
            ("Контрагент", counterparty_value, True),
            ("Статья", article_value, True),
            ("Срок исполнения работ по договору", work_deadline_value, False),
        ):
            broken = looks_broken(value, require_letter=need_letter)
            if broken:
                raise ValidationError(f"Поле «{label}»: {broken}.")
        # Комментарий необязателен: валидируем только непустой (длина).
        comment_value = (
            validate_text_field(comment, field_name="Комментарий", max_len=500)
            if comment.strip()
            else ""
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if currency not in CURRENCIES:
        raise HTTPException(status_code=422, detail="Некорректная валюта.")
    try:
        urgency_value = Urgency[urgency]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Некорректная срочность.") from exc
    if planned_value is None:
        # Срочно → сегодня; обычная → следующий рабочий день (пт/вых → пн).
        planned_value = auto_planned_date(urgency_value.is_urgent)

    if has_invoice not in ("0", "1"):
        raise HTTPException(status_code=422, detail="Некорректное значение признака счёта.")
    with_invoice = has_invoice == "1"
    now = datetime.now(ZoneInfo(settings.timezone))
    username = user.get("username")
    # Подтверждённое ФИО из справочника СБ, а не переименовываемый профиль.
    full_name = settings.employee_name_for(user["id"]) or " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p
    ) or "—"

    invoice = InvoiceRequest(
        telegram_id=user["id"],
        sender_username=f"@{username}" if username else "—",
        sender_name=full_name,
        amount=amount_value,
        currency=currency,
        counterparty=counterparty_value,
        article=article_value,
        planned_date=planned_value,
        work_deadline=work_deadline_value,
        comment=comment_value,
        urgency=urgency_value,
        has_invoice=with_invoice,
        created_at=now,
        request_id=new_request_id(now, user["id"]),
    )

    # --- Похоже на случайный набор символов --------------------------------
    # Это ПРЕДУПРЕЖДЕНИЕ, а не отказ: эвристика иногда ошибётся, а сорванная
    # оплата дороже одной кривой строки. Отдельный флаг, не общий force, —
    # иначе подтверждение мусора заодно отключало бы проверку на дубль.
    if confirm_text != "1":
        odd = [
            label
            for label, value in (
                ("Контрагент", counterparty_value),
                ("Статья", article_value),
                ("Срок исполнения работ по договору", work_deadline_value),
                ("Комментарий", comment_value),
            )
            if looks_like_gibberish(value)
        ]
        if odd:
            return JSONResponse(
                status_code=409,
                content={
                    "suspicious": True,
                    "fields": odd,
                    "detail": "Похоже на случайный набор символов: " + ", ".join(odd),
                },
            )

    # --- Дедуп: не подавалась ли такая же заявка недавно ---------------------
    if force != "1":
        last_seen = await dedup.check_duplicate(invoice)
        if last_seen:
            return JSONResponse(
                status_code=409,
                content={
                    "duplicate": True,
                    "detail": (
                        f"Похоже, такая заявка уже подавалась {last_seen}: "
                        "совпадают контрагент, сумма, валюта, статья и дата оплаты."
                    ),
                },
            )
    else:
        await audit.log_event(
            audit.DUPLICATE_CONFIRMED, user["id"], user.get("username"), invoice.request_id
        )

    # --- Файл счёта ИЛИ реквизиты -------------------------------------------
    invoice_bytes: bytes | None = None
    file_warning: str | None = None

    # --- Дополнительные документы: договор, акт, спецификация ---------------
    # Необязательны и не зависят от того, есть ли счёт: заявка по реквизитам
    # тоже бывает с договором. Проверяются тем же validate_file, что и счёт,
    # — формат и размер у них одни и те же, разница только в количестве.
    extras = [f for f in (extra_files or []) if f is not None and f.filename]
    if len(extras) > MAX_EXTRA_FILES:
        raise HTTPException(
            status_code=422,
            detail=f"Дополнительных документов не больше {MAX_EXTRA_FILES}.",
        )
    for position, extra in enumerate(extras, start=1):
        blob = await _read_limited(extra, MAX_FILE_SIZE_BYTES)
        if len(blob) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=422, detail=f"«{extra.filename}» больше 20 МБ."
            )
        try:
            validate_file(extra.content_type, len(blob))
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"«{extra.filename}»: {exc}"
            ) from exc
        name = build_extra_filename(extra.filename, invoice.request_id, position)
        invoice.extra_files.append(await storage.save_invoice(blob, name))
    # Счёт и реквизиты — оба НЕОБЯЗАТЕЛЬНЫ (с 26.08.2026). Раньше требовалось
    # ровно одно из двух, и заявку «оплатить по договору, документы будут
    # позже» подать было нельзя: человек придумывал реквизиты или прикладывал
    # что попало, лишь бы форма пропустила. Что именно приложено, видно
    # финансисту в карточке — включая случай, когда не приложено ничего.
    has_file = file is not None and bool(file.filename)
    if with_invoice and has_file:
        content = await _read_limited(file, MAX_FILE_SIZE_BYTES)
        if len(content) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=422, detail="Файл больше 20 МБ.")
        try:
            validate_file(file.content_type, len(content))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Мягкая автопроверка «похоже ли на счёт» (текст PDF / OCR).
        file_warning = await asyncio.to_thread(
            invoice_check.check_invoice_file, content, file.filename, amount_value
        )
        invoice.file_name = build_invoice_filename(
            file.filename, counterparty_value, amount_value, now
        )
        # Ссылка (Google Drive) или путь (локально) — колонка «Ссылка на счет».
        invoice.file_url = await storage.save_invoice(content, invoice.file_name)
        invoice_bytes = content
    elif requisites and requisites.strip():
        # Реквизиты проверяем, только если их прислали: пустые — не ошибка.
        try:
            invoice.requisites = validate_text_field(
                requisites, field_name="Реквизиты", max_len=1500
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    # has_invoice в модели отражает ФАКТ, а не выбор в форме: без файла
    # карточка не должна обещать финансисту вложение, которого нет.
    invoice.has_invoice = with_invoice and has_file

    # --- Возврат итога в группу (id пришёл из deep-link, проверяется в intake)
    return_chat_id: int | None = None
    cleaned = return_chat.strip()
    if cleaned and cleaned.lstrip("-").isdigit():
        return_chat_id = int(cleaned)

    # --- Финализация ---------------------------------------------------------
    bot = request.app.state.bot
    try:
        await finalize_submission(
            bot,
            invoice,
            return_chat_id=return_chat_id,
            invoice_file=invoice_bytes,
            file_warning=file_warning,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка финализации заявки %s из Mini App", invoice.request_id)
        raise HTTPException(
            status_code=500,
            detail="Не удалось сохранить заявку. Попробуйте позже или сообщите администратору.",
        ) from exc

    # warning показывается и в приложении (экран успеха), не только в чате.
    return {
        "ok": True,
        "request_id": invoice.request_id,
        "planned_date": planned_value.strftime("%d.%m.%Y"),
        "warning": file_warning,
    }
