"""Алерты админам о сбоях: «тихих» падений быть не должно.

Отправляются в личку всем из ADMIN_IDS. Троттлинг двухуровневый:
одна и та же ошибка (по сигнатуре) — не чаще раза в 30 минут, суммарно —
не более 10 алертов в час: шторм однотипных ошибок не превращается в спам.

Категория (kind) сверяется с настройками из админ-панели: админ решает, о чём
его будить, — кроме критичного (потеря заявки), которое не выключается.
В журнал инцидентов сбой попадает ВСЕГДА, даже выключенный: выключен звонок,
а не датчик, и панель показывает то, что было на самом деле.

Модуль обязан быть безопасным: сбой отправки алерта никогда не влияет
на основной сценарий.
"""
from __future__ import annotations

import hashlib
import html
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.constants import ParseMode

from config import settings
from services import runtime_settings as rs
from services.runtime_settings import effective_admin_ids

log = logging.getLogger(__name__)

# Одинаковая ошибка — не чаще раза в этот интервал (сек).
SAME_SIGNATURE_WINDOW = 1800.0
# Общий потолок: не больше GLOBAL_MAX алертов за GLOBAL_WINDOW.
GLOBAL_WINDOW = 3600.0
GLOBAL_MAX = 10

_last_by_signature: dict[str, float] = {}
_sent_times: list[float] = []


def _allowed(signature: str, now: float | None = None, *, critical: bool = False) -> bool:
    """Пропускает алерт через троттлинг (и учитывает его).

    critical=True обходит ОБЩИЙ потолок, но не окно по сигнатуре. Иначе
    потолок можно было выесть чем угодно — например, десятком разных ошибок
    из браузера, ручка для которых открыта любому с подписью Telegram, — и
    следующее «заявка НЕ сохранилась» не ушло бы никому целый час. Защита от
    шторма не должна становиться способом заглушить главное.
    """
    global _sent_times
    now = time.monotonic() if now is None else now

    _sent_times = [t for t in _sent_times if now - t < GLOBAL_WINDOW]
    if not critical and len(_sent_times) >= GLOBAL_MAX:
        return False

    last = _last_by_signature.get(signature)
    if last is not None and now - last < SAME_SIGNATURE_WINDOW:
        return False

    _last_by_signature[signature] = now
    _sent_times.append(now)
    return True


def _journal(
    kind: str | None, title: str, *, sent: bool, details: str = "", reason: str = ""
) -> None:
    """Отметка в журнале инцидентов. Сбой записи не мешает самому алерту."""
    try:
        rs.record_incident(
            kind, title, sent=sent, when=time.time(), details=details, reason=reason
        )
    except Exception:  # noqa: BLE001 — журнал вторичен по отношению к алерту
        log.exception("Не удалось записать инцидент «%s» в журнал", title)


# Категории, к которым уместны ссылки на реестр и папку счетов: смотреть
# «что там на самом деле» админ идёт именно по ним.
_REGISTRY_KINDS = frozenset({"storage", "mirror", "delivery"})


def _buttons(kind: str | None) -> InlineKeyboardMarkup | None:
    """Кнопки под алертом: куда идти разбираться.

    Сообщение о сбое без ссылок заставляет админа искать нужное место
    руками — в переписке, в закладках, в почте, — и именно поэтому разбор
    откладывается. Панель открывается кнопкой Mini App (обычная ссылка на
    наш домен вне Telegram отдаст «откройте из Telegram»), реестр и папка —
    обычными ссылками: они и в браузере открываются нормально.
    """
    rows: list[list[InlineKeyboardButton]] = []
    url = (settings.webapp_url or "").strip()
    if url.startswith("https://"):
        rows.append(
            [InlineKeyboardButton("🛟 Здоровье бота", web_app=WebAppInfo(url=url))]
        )
    if kind in _REGISTRY_KINDS:
        from services import storage  # локально: фасад тянет бэкенды, а мы в импорте

        sheet, drive = storage.registry_links()
        line = []
        if sheet:
            line.append(InlineKeyboardButton("📊 Реестр", url=sheet))
        if drive:
            line.append(InlineKeyboardButton("📁 Счета", url=drive))
        if line:
            rows.append(line)
    return InlineKeyboardMarkup(rows) if rows else None


async def _deliver(
    bot: Bot,
    text: str,
    targets: list[int],
    markup: InlineKeyboardMarkup | None = None,
) -> int:
    delivered = 0
    for chat_id in targets:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            delivered += 1
        except Exception:  # noqa: BLE001 — алерт не должен ломать сценарий
            log.warning("Не удалось доставить алерт админу %s", chat_id)
    return delivered


def _stamp() -> str:
    """Момент сбоя в часовом поясе бота.

    Без него неясно, свежее это или лежало в очереди: алерт о связи почти
    всегда приходит уже после восстановления, а троттлинг задерживает повторы.
    """
    try:
        return datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m %H:%M")
    except Exception:  # noqa: BLE001 — часы не повод потерять алерт
        return ""


def _format(title: str, details: str = "", *, icon: str = "🚨", hint: str = "") -> str:
    e = html.escape
    head = f"{icon} <b>{e(title)}</b>"
    # Плашка стенда: два бота пишут одному и тому же человеку, и без метки
    # тревога со стенда неотличима от боевой.
    label = (settings.env_label or "").strip()
    if label:
        head = f"[{e(label)}] {head}"
    parts = [head]
    stamp = _stamp()
    if stamp:
        parts.append(f"<i>{e(stamp)}</i>")
    if details:
        parts.append(f"<code>{e(details[:900])}</code>")
    if hint:
        parts.append(f"👉 {e(hint)}")
    return "\n".join(parts)


async def alert_admins(
    bot: Bot,
    title: str,
    details: str = "",
    *,
    signature: str | None = None,
    kind: str | None = None,
    hint: str = "",
    journal: bool = True,
) -> int:
    """Шлёт алерт всем админам. Возвращает число доставленных сообщений.

    kind — категория из ALERT_KINDS; выключенная в панели не отправляется, но
    в журнал инцидентов попадает всё равно.

    journal=False — вызывающий ведёт журнал сам. Нужно там, где событие
    записано РАНЬШЕ попытки дозвона (пульс связи пишет обрыв с первой
    неудачной пробы), иначе один провал попал бы в журнал дважды.
    """
    admins = effective_admin_ids()
    if not admins:
        log.warning("Алерт «%s» не отправлен: ADMIN_IDS пуст", title)
        if journal:
            _journal(kind, title, sent=False, details=details, reason="no-admins")
        return 0

    if not rs.alert_kind_enabled(kind):
        log.info("Алерт «%s»: категория %s выключена в панели", title, kind)
        if journal:
            _journal(kind, title, sent=False, details=details, reason="kind-off")
        return 0

    sig = signature or hashlib.sha256(f"{title}|{details[:100]}".encode()).hexdigest()[:16]
    if not _allowed(sig, critical=kind in rs.CRITICAL_ALERT_KEYS):
        if journal:
            _journal(kind, title, sent=False, details=details, reason="throttled")
        return 0

    delivered = await _deliver(
        bot, _format(title, details, hint=hint), admins, _buttons(kind)
    )
    if journal:
        _journal(
            kind, title,
            sent=bool(delivered),
            details=details,
            reason="" if delivered else "undelivered",
        )
    return delivered


async def send_test_alert(bot: Bot, chat_id: int) -> bool:
    """Проверочное уведомление — только тому, кто нажал кнопку.

    Обходит и категории, и троттлинг: это осознанная проверка канала
    «бот → админ», и она обязана срабатывать с первого раза. В журнал
    инцидентов не пишется — сбоя не было.
    """
    text = _format(
        "Проверка уведомлений",
        "Канал работает: так будет выглядеть сообщение о сбое.",
        icon="✅",
        hint="Кнопки ниже ведут туда, где сбой видно целиком.",
    )
    return bool(await _deliver(bot, text, [chat_id], _buttons("storage")))


def _origin(error: BaseException) -> str:
    """Файл и строка, где всё случилось.

    «Ошибка бота: NetworkError» без места — это ребус: тип есть, искать
    негде. Берём последний кадр трассировки, он и есть точка отказа.
    """
    tb = error.__traceback__
    if tb is None:
        return ""
    while tb.tb_next is not None:
        tb = tb.tb_next
    name = tb.tb_frame.f_code.co_filename.split("/")[-1]
    return f"{name}:{tb.tb_lineno}"


async def alert_error(bot: Bot, error: BaseException, where: str = "") -> None:
    """Алерт о необработанной ошибке (сигнатура — тип + начало сообщения)."""
    details = f"{where}\n{error}" if where else str(error)
    origin = _origin(error)
    if origin:
        details = f"{details}\n({origin})"
    signature = f"{type(error).__name__}:{str(error)[:80]}"
    try:
        await alert_admins(
            bot,
            f"Ошибка бота: {type(error).__name__}",
            details,
            signature=signature,
            kind="error",
        )
    except Exception:  # noqa: BLE001
        log.exception("Сбой при отправке алерта об ошибке")
