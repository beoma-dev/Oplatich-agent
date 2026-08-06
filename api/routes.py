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
    MAX_FILE_SIZE_BYTES,
    ValidationError,
    parse_amount,
    parse_planned_date,
    parse_registry_filter_date,
    validate_file,
    validate_text_field,
)
from config import settings
from services import (
    audit,
    backup,
    dedup,
    invoice_check,
    invoice_extract,
    request_meta,
    storage,
)
from services import health as health_pulse
from services import runtime_settings as rs
from services.access_requests import request_access
from services.deletion import delete_request as delete_request_service
from services.intake import finalize_submission
from services.local_storage import build_invoice_filename
from services.reminders import run_reminders
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
    # Заодно отдаём признак финансиста: приложение опрашивает эту ручку и по
    # ней же убирает/возвращает кнопку панели, когда права поменяли в чате.
    return {
        "allowed": is_allowed(user["id"]),
        "financier": is_financier(user["id"]),
        "pending": rs.access_request_pending(user["id"]),
        "has_admins": bool(rs.effective_admin_ids()),
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
        "requisites": row.get("Реквизиты", ""),
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
    rows = await storage.recent_by_author(user["id"], limit=MY_LIST_LIMIT)
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


def _matches(
    row: dict[str, str],
    *,
    status: str,
    urgency: str,
    query: str,
    date_from: date | None,
    date_to: date | None,
) -> bool:
    if status and row.get("Статус оплаты", "") != status:
        return False
    if urgency and row.get("Срочность", "") != urgency:
        return False
    if query:
        haystack = " ".join((
            row.get("Контрагент", ""), row.get("Сотрудник по заявке", ""),
            row.get("Статья", ""), row.get("Комментарий", ""), row.get("ID заявки", ""),
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
    registry_url = (
        f"https://docs.google.com/spreadsheets/d/{settings.google_sheet_id}"
        if settings.storage_is_google and settings.google_sheet_id
        else None
    )
    # Папка Диска, куда складываются файлы счетов, — рядом с реестром.
    drive_url = (
        f"https://drive.google.com/drive/folders/{settings.google_drive_folder_id}"
        if settings.storage_is_google and settings.google_drive_folder_id
        else None
    )
    return {
        "autofill": rs.autofill_enabled(),
        "financiers": financiers,
        "allowed": allowed,
        "org_name": settings.org_name,
        "backup": rs.backup_config(),
        "reminders": rs.reminders_config(),
        "registry_url": registry_url,
        "drive_url": drive_url,
        "admins": admins,
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


@router.post("/admin/reminders")
async def admin_reminders(request: Request) -> dict:
    """Напоминания финансистам: {"action": "save"|"run", …}.

    «run» — прогнать рассылку прямо сейчас: удобно проверить, что финансист
    настроен и сообщение доходит, не дожидаясь утра.
    """
    user = await _require_admin(request)
    body = await request.json()
    action = body.get("action")

    if action == "run":
        try:
            due, overdue = await run_reminders(request.app.state.bot)
        except Exception as exc:  # noqa: BLE001
            log.exception("Сбой ручного прогона напоминаний")
            raise HTTPException(
                status_code=500, detail="Не удалось разослать напоминания."
            ) from exc
        if not due and not overdue:
            message = "Напоминать не о чем: нет ни ближайших, ни просроченных заявок."
        else:
            message = f"Разослано: к оплате {due}, просрочено {overdue}."
        return {"ok": True, "message": message, "due": due, "overdue": overdue}

    if action == "save":
        time_value = str(body.get("time", "")).strip()
        if time_value and not _TIME_RE.fullmatch(time_value):
            raise HTTPException(status_code=422, detail="Время — в формате ЧЧ:ММ.")

        days_raw = str(body.get("days_before", "")).strip()
        days_value: int | None = None
        if days_raw:
            if not days_raw.isdigit() or not 0 <= int(days_raw) <= 14:
                raise HTTPException(
                    status_code=422, detail="Предупреждать за 0–14 дней."
                )
            days_value = int(days_raw)

        target = str(body.get("overdue_to", "")).strip()
        if target and target not in rs.OVERDUE_TARGETS:
            raise HTTPException(status_code=422, detail="Некорректный получатель.")

        cfg = await asyncio.to_thread(
            rs.set_reminders_config,
            enabled=bool(body.get("enabled")),
            time=time_value or None,
            days_before=days_value,
            overdue_enabled=bool(body.get("overdue_enabled")),
            overdue_to=target or None,
        )
        await audit.log_event(
            audit.REMINDER_SETTINGS,
            user["id"],
            user.get("username"),
            f"enabled={cfg['enabled']} time={cfg['time']} days={cfg['days_before']} "
            f"overdue={cfg['overdue_enabled']}→{cfg['overdue_to']}",
        )
        state = "включены" if cfg["enabled"] else "выключены"
        return {
            "ok": True,
            "message": f"Напоминания {state}: ежедневно в {cfg['time']}.",
            "reminders": cfg,
        }

    raise HTTPException(status_code=422, detail="Некорректный запрос.")


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

    content = await file.read()
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
    if rs.autofill_enabled() and text:
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
    comment: str = Form(""),
    urgency: str = Form(...),
    has_invoice: str = Form(...),
    requisites: str = Form(""),
    return_chat: str = Form(""),
    force: str = Form("0"),
    file: UploadFile | None = File(None),
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
        comment=comment_value,
        urgency=urgency_value,
        has_invoice=with_invoice,
        created_at=now,
        request_id=new_request_id(now, user["id"]),
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
    if with_invoice:
        if file is None or not file.filename:
            raise HTTPException(status_code=422, detail="Прикрепите файл счёта.")
        content = await file.read()
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
    else:
        try:
            invoice.requisites = validate_text_field(
                requisites, field_name="Реквизиты", max_len=1500
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
