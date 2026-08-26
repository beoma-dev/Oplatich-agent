"""Динамические настройки бота: финансисты и whitelist, управляемые из Telegram.

Значения из .env — «базовые» (правятся только на сервере), сюда пишутся
добавленные через админ-панель/команды. Эффективные списки = .env + динамика.
Хранение — JSON-файл (переживает перезапуск), потокобезопасно через Lock.
"""
from __future__ import annotations

import json
import logging
import re
import threading

from config import settings

log = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: dict | None = None

_DEFAULTS: dict = {
    "finance": [],
    # Финансисты из .env, отключённые через админ-панель: список из .env
    # правится только на сервере, но «убрал себя — а кнопка осталась»
    # выглядит как поломка, поэтому исключения храним здесь.
    "finance_off": [],
    "allowed": [],
    # Whitelist из .env, отозванный через панель, — по той же причине: сам
    # файл бот не правит, а отзыв должен работать сразу.
    "allowed_off": [],
    # Админы, назначенные из панели. Списка «отключённых» тут намеренно нет:
    # запись из .env снять нельзя, иначе назначенный админ разжаловал бы
    # того, кто его назначил.
    "admins": [],
    "admin_chats": {},
    # Заявки на доступ: {id: {"username": ..., "ts": ...}}. Живут здесь же,
    # чтобы не заводить второй файл с блокировкой ради пары записей.
    "access_requests": {},
    "backup": {},
    "reminders": {},
    # Личные настройки напоминаний: {"<id>": {time, days_before, ...}}.
    # Общие остаются значением по умолчанию для тех, кто себе ничего не
    # менял, — иначе новый финансист остался бы вовсе без напоминаний.
    "reminders_by_user": {},
    # Какие заявки присылать получателю карточкой сразу после подачи:
    # {"<id>": "all"|"urgent"}. Это НЕ напоминания о сроках — это первое
    # уведомление о новой заявке, и кому-то из финансистов нужны только
    # срочные. Умолчание "all": молча перестать присылать заявки человеку,
    # который об этом не просил, нельзя.
    "cards_by_user": {},
    "autofill": {},
    # Личный выключатель чтения счёта: {"<id>": true|false}. Общая настройка
    # остаётся значением по умолчанию — бета есть бета, и человек, которому
    # распознавание мешает, не должен идти за этим к админу.
    "autofill_by_user": {},
    # Уведомления о сбоях: главный режим, категории, порог по связи.
    "alerts": {},
    # Журнал инцидентов: что ломалось, когда и сколько раз. Ведётся всегда,
    # даже по выключенным категориям, — выключен звонок, а не датчик.
    "incidents": [],
}


def _fresh_cache() -> dict:
    """Пустые настройки. Один источник структуры — иначе ключи разъезжаются."""
    return {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in _DEFAULTS.items()}


def _load_locked() -> dict:
    global _cache
    if _cache is None:
        path = settings.runtime_settings_path
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                _cache = {
                    "finance": [str(x) for x in raw.get("finance", [])],
                    "finance_off": [str(x) for x in raw.get("finance_off", [])],
                    "allowed": [int(x) for x in raw.get("allowed", [])],
                    "allowed_off": [int(x) for x in raw.get("allowed_off", [])],
                    "admins": [int(x) for x in raw.get("admins", [])],
                    "admin_chats": {
                        str(k): str(v) for k, v in raw.get("admin_chats", {}).items()
                    },
                    "access_requests": dict(raw.get("access_requests", {})),
                    "backup": dict(raw.get("backup", {})),
                    "reminders": dict(raw.get("reminders", {})),
                    "cards_by_user": {
                        str(k): str(v)
                        for k, v in raw.get("cards_by_user", {}).items()
                    },
                    "reminders_by_user": {
                        str(k): dict(v)
                        for k, v in raw.get("reminders_by_user", {}).items()
                    },
                    "autofill": dict(raw.get("autofill", {})),
                    "autofill_by_user": dict(raw.get("autofill_by_user", {})),
                    "alerts": dict(raw.get("alerts", {})),
                    "incidents": [dict(x) for x in raw.get("incidents", [])],
                }
            except (ValueError, OSError):
                log.exception("Не удалось прочитать настройки %s — начинаю с пустых", path)
                _cache = _fresh_cache()
        else:
            _cache = _fresh_cache()
    # Файл мог быть создан версией без этих ключей.
    _cache.setdefault("finance_off", [])
    _cache.setdefault("allowed_off", [])
    _cache.setdefault("admins", [])
    _cache.setdefault("access_requests", {})
    _cache.setdefault("reminders", {})
    _cache.setdefault("reminders_by_user", {})
    _cache.setdefault("cards_by_user", {})
    _cache.setdefault("autofill", {})
    _cache.setdefault("autofill_by_user", {})
    _cache.setdefault("alerts", {})
    _cache.setdefault("incidents", [])
    return _cache


def _save_locked() -> None:
    path = settings.runtime_settings_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Через временный файл: журнал инцидентов сделал записи частыми, а
        # обрыв посреди write_text оставил бы обрезанный JSON — здесь состав
        # финансистов, whitelist и админы, терять их нельзя.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        log.exception("Не удалось сохранить настройки %s", path)


# ---------------------------------------------------------------------------
# Эффективные списки (.env + динамика)
# ---------------------------------------------------------------------------
def effective_finance_recipients() -> list[str]:
    """Финансисты: .env + добавленные из Telegram − отключённые из панели."""
    with _lock:
        data = _load_locked()
        dynamic = list(data["finance"])
        disabled = {_norm_fin(x) for x in data["finance_off"]}
    out = list(settings.finance_recipients)
    for entry in dynamic:
        if entry not in out:
            out.append(entry)
    return [e for e in out if _norm_fin(e) not in disabled]


def effective_allowed_ids() -> list[int]:
    """Whitelist: из .env плюс добавленные из Telegram, минус отозванные."""
    with _lock:
        data = _load_locked()
        dynamic = list(data["allowed"])
        off = set(data["allowed_off"])
    out = [uid for uid in settings.allowed_user_ids if uid not in off]
    for uid in dynamic:
        if uid not in out:
            out.append(uid)
    return out


def dynamic_finance() -> list[str]:
    with _lock:
        return list(_load_locked()["finance"])


def effective_admin_ids() -> list[int]:
    """Админы бота: заданные в .env плюс назначенные из панели.

    В отличие от финансистов и whitelist, запись из .env панелью НЕ снимается:
    владелец сервера — последняя инстанция, иначе назначенный админ мог бы
    разжаловать того, кто его назначил, и вернуть права было бы негде.
    """
    with _lock:
        dynamic = list(_load_locked()["admins"])
    out = list(settings.admin_ids)
    for uid in dynamic:
        if uid not in out:
            out.append(uid)
    return out


def dynamic_admins() -> list[int]:
    with _lock:
        return list(_load_locked()["admins"])


def add_admin(user_id: int) -> bool:
    """Назначает админа из панели."""
    with _lock:
        data = _load_locked()
        if user_id in data["admins"] or user_id in settings.admin_ids:
            return False
        data["admins"].append(user_id)
        _save_locked()
    log.info("Настройки: назначен админ %s", user_id)
    return True


def remove_admin(user_id: int) -> bool:
    """Снимает права у назначенного из панели. Админа из .env — не трогает."""
    with _lock:
        data = _load_locked()
        if user_id not in data["admins"]:
            return False
        data["admins"].remove(user_id)
        _save_locked()
    log.info("Настройки: снят админ %s", user_id)
    return True


def dynamic_allowed() -> list[int]:
    with _lock:
        return list(_load_locked()["allowed"])


def disabled_allowed() -> list[int]:
    """Записи whitelist из .env, отозванные из панели."""
    with _lock:
        return list(_load_locked()["allowed_off"])


# ---------------------------------------------------------------------------
# Изменение (возвращают False, если менять нечего)
# ---------------------------------------------------------------------------
# Формат username Telegram: 5–32 символа, буквы/цифры/подчёркивание.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def valid_financier_entry(entry: str) -> bool:
    """Числовой id или корректный @username — иначе запись не принимаем."""
    e = entry.strip().lstrip("@")
    return bool(e) and (e.lstrip("-").isdigit() or _USERNAME_RE.fullmatch(e) is not None)


def _norm_fin(entry: str) -> str:
    e = entry.strip()
    return e if e.lstrip("-").isdigit() else "@" + e.lstrip("@").lower()


def add_financier(entry: str) -> bool:
    """Добавляет финансиста; отключённого ранее из .env — возвращает обратно."""
    e = _norm_fin(entry)
    if not e or e == "@":
        return False
    with _lock:
        data = _load_locked()
        was_disabled = e in data["finance_off"]
        if was_disabled:
            data["finance_off"].remove(e)
        already = e in data["finance"] or any(
            _norm_fin(x) == e for x in settings.finance_recipients
        )
        if already and not was_disabled:
            return False
        if not already:
            data["finance"].append(e)
        _save_locked()
    log.info("Настройки: добавлен финансист %s", e)
    return True


def remove_financier(entry: str) -> bool:
    """Убирает финансиста — и добавленного из Telegram, и заданного в .env.

    Строку в .env бот не правит (файл — за пределами его полномочий), поэтому
    запись из .env заносится в список отключённых: эффективный список её
    больше не содержит. Вернуть — тем же «добавить».
    """
    e = _norm_fin(entry)
    with _lock:
        data = _load_locked()
        if e in data["finance"]:
            data["finance"].remove(e)
            _save_locked()
            log.info("Настройки: удалён финансист %s", e)
            return True
        from_env = any(_norm_fin(x) == e for x in settings.finance_recipients)
        if from_env and e not in data["finance_off"]:
            data["finance_off"].append(e)
            _save_locked()
            log.info("Настройки: финансист из .env отключён — %s", e)
            return True
    return False


def add_allowed(user_id: int) -> bool:
    """Открывает доступ: снимает отзыв записи из .env либо добавляет свою."""
    with _lock:
        data = _load_locked()
        if user_id in data["allowed_off"]:
            data["allowed_off"].remove(user_id)
            _save_locked()
            log.info("Настройки: доступ из .env возвращён %s", user_id)
            return True
        if user_id in data["allowed"] or user_id in settings.allowed_user_ids:
            return False
        data["allowed"].append(user_id)
        _save_locked()
    log.info("Настройки: в whitelist добавлен %s", user_id)
    return True


def remove_allowed(user_id: int) -> bool:
    """Закрывает доступ — и добавленному из Telegram, и заданному в .env.

    Строку в .env бот не правит (файл вне его полномочий), поэтому запись
    из .env заносится в список отозванных: эффективный whitelist её больше
    не содержит. Вернуть — тем же «добавить». Так же устроены финансисты.
    """
    with _lock:
        data = _load_locked()
        if user_id in data["allowed"]:
            data["allowed"].remove(user_id)
            _save_locked()
            log.info("Настройки: из whitelist удалён %s", user_id)
            return True
        if user_id in settings.allowed_user_ids and user_id not in data["allowed_off"]:
            data["allowed_off"].append(user_id)
            _save_locked()
            log.info("Настройки: доступ из .env отозван — %s", user_id)
            return True
    return False


# ---------------------------------------------------------------------------
# Заявки на доступ к подаче
# ---------------------------------------------------------------------------
def add_access_request(user_id: int, username: str, when: float) -> bool:
    """Регистрирует просьбу о доступе. False — такая уже висит.

    Повторные нажатия не должны заваливать админов уведомлениями, поэтому
    вторая заявка от того же человека молча игнорируется.
    """
    with _lock:
        data = _load_locked()
        key = str(user_id)
        if key in data["access_requests"]:
            return False
        data["access_requests"][key] = {"username": username, "ts": when}
        _save_locked()
    log.info("Доступ: поступила заявка от %s", user_id)
    return True


def access_request_pending(user_id: int) -> bool:
    with _lock:
        return str(user_id) in _load_locked()["access_requests"]


def clear_access_request(user_id: int) -> bool:
    """Снимает заявку — после решения админа или выданного доступа."""
    with _lock:
        data = _load_locked()
        if data["access_requests"].pop(str(user_id), None) is None:
            return False
        _save_locked()
    return True


# ---------------------------------------------------------------------------
# Доверенные чаты: их администраторы = админы бота
# ---------------------------------------------------------------------------
def remember_admin_chat(chat_id: int, title: str) -> bool:
    """Запоминает канал/группу как источник админов бота."""
    key = str(chat_id)
    with _lock:
        data = _load_locked()
        if data["admin_chats"].get(key) == title:
            return False
        data["admin_chats"][key] = title
        _save_locked()
    log.info("Настройки: чат «%s» (%s) даёт права админа бота", title, chat_id)
    return True


def forget_admin_chat(chat_id: int) -> bool:
    """Убирает чат из доверенных (бота удалили из чата)."""
    key = str(chat_id)
    with _lock:
        data = _load_locked()
        if key not in data["admin_chats"]:
            return False
        del data["admin_chats"][key]
        _save_locked()
    log.info("Настройки: чат %s больше не даёт права админа", chat_id)
    return True


def admin_chat_ids() -> list[int]:
    with _lock:
        return [int(k) for k in _load_locked()["admin_chats"]]


# ---------------------------------------------------------------------------
# Настройки автобэкапа: overrides из админ-панели поверх .env
# ---------------------------------------------------------------------------
def backup_config() -> dict:
    """Эффективная конфигурация бэкапа: overrides админа поверх .env."""
    with _lock:
        override = dict(_load_locked().get("backup", {}))
    enabled_default = bool(settings.backup_time.strip())
    try:
        keep = int(override.get("keep", settings.backup_keep))
    except (TypeError, ValueError):
        keep = settings.backup_keep
    return {
        "enabled": bool(override.get("enabled", enabled_default)),
        "time": str(override.get("time") or settings.backup_time.strip() or "03:30"),
        "keep": max(1, min(keep, 60)),
    }


def set_backup_config(
    *, enabled: bool | None = None, time: str | None = None, keep: int | None = None
) -> dict:
    """Сохраняет overrides бэкапа. Возвращает эффективную конфигурацию."""
    with _lock:
        data = _load_locked()
        override = data.setdefault("backup", {})
        if enabled is not None:
            override["enabled"] = bool(enabled)
        if time is not None:
            override["time"] = time
        if keep is not None:
            override["keep"] = int(keep)
        _save_locked()
    log.info("Настройки бэкапа обновлены: %s", backup_config())
    return backup_config()


# ---------------------------------------------------------------------------
# Напоминания финансистам о сроках оплаты
# ---------------------------------------------------------------------------
# Кому уходит сводка по просрочке.
OVERDUE_TARGETS = ("financiers", "admins", "both")


def reminders_config() -> dict:
    """Эффективные настройки напоминаний: overrides админа поверх .env."""
    with _lock:
        override = dict(_load_locked().get("reminders", {}))

    try:
        days_before = int(override.get("days_before", 1))
    except (TypeError, ValueError):
        days_before = 1
    target = str(override.get("overdue_to", "admins"))
    return {
        "enabled": bool(override.get("enabled", settings.reminders_enabled)),
        "time": str(override.get("time") or settings.reminder_time.strip() or "09:30"),
        # За сколько дней предупреждать: 1 — «завтра», 0 — только в день оплаты.
        "days_before": max(0, min(days_before, 14)),
        "overdue_enabled": bool(override.get("overdue_enabled", True)),
        "overdue_to": target if target in OVERDUE_TARGETS else "admins",
    }


def set_reminders_config(
    *,
    enabled: bool | None = None,
    time: str | None = None,
    days_before: int | None = None,
    overdue_enabled: bool | None = None,
    overdue_to: str | None = None,
) -> dict:
    """Сохраняет настройки напоминаний. Возвращает эффективную конфигурацию."""
    with _lock:
        data = _load_locked()
        override = data.setdefault("reminders", {})
        if enabled is not None:
            override["enabled"] = bool(enabled)
        if time is not None:
            override["time"] = time
        if days_before is not None:
            override["days_before"] = int(days_before)
        if overdue_enabled is not None:
            override["overdue_enabled"] = bool(overdue_enabled)
        if overdue_to is not None and overdue_to in OVERDUE_TARGETS:
            override["overdue_to"] = overdue_to
        _save_locked()
    log.info("Настройки напоминаний обновлены: %s", reminders_config())
    return reminders_config()


def personal_reminders(user_id: int) -> dict:
    """Настройки напоминаний конкретного получателя.

    Общие настройки — значения по умолчанию: пока человек ничего себе не
    менял, он получает напоминания «как все». Что он поменял — перекрывает
    общее только для него.
    """
    base = reminders_config()
    with _lock:
        own = dict(_load_locked()["reminders_by_user"].get(str(user_id), {}))
    try:
        days_before = int(own.get("days_before", base["days_before"]))
    except (TypeError, ValueError):
        days_before = base["days_before"]
    return {
        "enabled": bool(own.get("enabled", base["enabled"])),
        "time": str(own.get("time") or base["time"]),
        "days_before": max(0, min(days_before, 14)),
        # Два потока раздельно: кому-то нужна только просрочка.
        "due_enabled": bool(own.get("due_enabled", True)),
        "overdue_enabled": bool(own.get("overdue_enabled", base["overdue_enabled"])),
        # По выходным платежей обычно нет — незачем и напоминать.
        "weekdays_only": bool(own.get("weekdays_only", False)),
        # True — человек настроил себе сам, а не идёт по общему умолчанию.
        "custom": bool(own),
        # True — он ЯВНО отписался. Общий выключатель — это расписание, его
        # ручной прогон обходит; личный отказ обходить нельзя.
        "muted": own.get("enabled") is False,
    }


CARD_URGENCY_ALL = "all"
CARD_URGENCY_URGENT = "urgent"
CARD_URGENCY_CHOICES = (CARD_URGENCY_ALL, CARD_URGENCY_URGENT)


def personal_card_urgency(user_id: int) -> str:
    """Какие заявки слать этому получателю карточкой: все или только срочные.

    Речь о первом уведомлении — том, что приходит сразу после подачи, — а не
    о напоминаниях по срокам: у тех свои настройки. Умолчание «все»: тихо
    перестать показывать человеку заявки, о чём он не просил, нельзя.
    """
    with _lock:
        value = _load_locked()["cards_by_user"].get(str(user_id), CARD_URGENCY_ALL)
    return value if value in CARD_URGENCY_CHOICES else CARD_URGENCY_ALL


def set_personal_card_urgency(user_id: int, value: str) -> str:
    """Сохраняет выбор получателя. Неизвестное значение — отказ, не «как-нибудь»."""
    if value not in CARD_URGENCY_CHOICES:
        raise ValueError(f"Неизвестный фильтр срочности: {value!r}")
    with _lock:
        data = _load_locked()
        data["cards_by_user"][str(user_id)] = value
        _save_locked()
    log.info("Карточки: получатель %s теперь получает «%s»", user_id, value)
    return value


def set_personal_reminders(
    user_id: int,
    *,
    enabled: bool | None = None,
    time: str | None = None,
    days_before: int | None = None,
    due_enabled: bool | None = None,
    overdue_enabled: bool | None = None,
    weekdays_only: bool | None = None,
) -> dict:
    """Сохраняет личные настройки получателя. Возвращает эффективные."""
    with _lock:
        data = _load_locked()
        own = data["reminders_by_user"].setdefault(str(user_id), {})
        if enabled is not None:
            own["enabled"] = bool(enabled)
        if time is not None:
            own["time"] = time
        if days_before is not None:
            own["days_before"] = int(days_before)
        if due_enabled is not None:
            own["due_enabled"] = bool(due_enabled)
        if overdue_enabled is not None:
            own["overdue_enabled"] = bool(overdue_enabled)
        if weekdays_only is not None:
            own["weekdays_only"] = bool(weekdays_only)
        _save_locked()
    log.info("Напоминания: личные настройки %s обновлены", user_id)
    return personal_reminders(user_id)


def clear_personal_reminders(user_id: int) -> dict:
    """Возврат к общим настройкам: «как у всех»."""
    with _lock:
        data = _load_locked()
        if data["reminders_by_user"].pop(str(user_id), None) is not None:
            _save_locked()
    return personal_reminders(user_id)


# ---------------------------------------------------------------------------
# Бета: автозаполнение формы из счёта
# ---------------------------------------------------------------------------
def autofill_enabled() -> bool:
    """Показывать ли предложение заполнить поля по распознанному счёту.

    Бета-функция: выключается из админ-панели одним тумблером, и форма
    возвращается к прежнему поведению — ручному вводу.
    """
    with _lock:
        override = _load_locked().get("autofill", {})
    return bool(override.get("enabled", settings.invoice_autofill))


def personal_autofill(user_id: int) -> bool:
    """Читать ли счёт ДЛЯ ЭТОГО человека.

    Общая настройка — значение по умолчанию; личный выбор перекрывает её
    только для него. Общий выключатель при этом остаётся главным: выключили
    для всех — не работает ни у кого, даже у того, кто включил себе.
    """
    if not autofill_enabled():
        return False
    with _lock:
        own = _load_locked().get("autofill_by_user", {}).get(str(user_id))
    return True if own is None else bool(own)


def set_personal_autofill(user_id: int, enabled: bool | None) -> bool:
    """Сохраняет личный выбор. None — вернуться к общей настройке."""
    with _lock:
        data = _load_locked()
        by_user = data.setdefault("autofill_by_user", {})
        if enabled is None:
            by_user.pop(str(user_id), None)
        else:
            by_user[str(user_id)] = bool(enabled)
        _save_locked()
    log.info("Чтение счёта для %s: %s", user_id, enabled)
    return personal_autofill(user_id)


def set_autofill(enabled: bool) -> bool:
    with _lock:
        data = _load_locked()
        data.setdefault("autofill", {})["enabled"] = bool(enabled)
        _save_locked()
    log.info("Настройки: автозаполнение из счёта — %s", "включено" if enabled else "выключено")
    return autofill_enabled()


# ---------------------------------------------------------------------------
# Уведомления о сбоях
# ---------------------------------------------------------------------------
# Категории: (ключ, подпись для панели, включена по умолчанию, критичная).
# Критичную выключить нельзя — это молчание там, где потеряны данные, и
# именно такое молчание однажды съело заявку. Порядок = порядок в панели.
ALERT_KINDS: tuple[tuple[str, str, bool, bool], ...] = (
    ("storage", "Заявка не сохранилась в реестр", True, True),
    ("delivery", "Карточка не дошла финансисту", True, False),
    ("telegram", "Пропадала связь с Telegram", True, False),
    ("backup", "Сбой бэкапа", True, False),
    ("mirror", "Реестр и зеркало разошлись", True, False),
    ("error", "Внутренние ошибки бота", True, False),
    ("moderation", "Мат в заявке", True, False),
)
ALERT_KEYS = tuple(k for k, _t, _d, _c in ALERT_KINDS)
CRITICAL_ALERT_KEYS = frozenset(k for k, _t, _d, crit in ALERT_KINDS if crit)
# Границы порога «связь пропала»: минута — нижняя граница пульса, час — выше
# уже не уведомление, а сводка.
LINK_GRACE_MIN, LINK_GRACE_MAX = 1, 60
LINK_GRACE_DEFAULT = 5
# Сколько инцидентов держим в журнале и в каком окне считаем повторы одним.
INCIDENT_LIMIT = 60
INCIDENT_MERGE_WINDOW = 1800.0


def alerts_config() -> dict:
    """Эффективные настройки уведомлений о сбоях.

    enabled=False — режим «только критичные»: полной тишины здесь нет и быть
    не может. Категории при этом сохраняются как были: вернул режим — вернул
    свои галочки, а не дефолт.
    """
    with _lock:
        override = dict(_load_locked().get("alerts", {}))
    raw_kinds = override.get("kinds") or {}
    kinds = {}
    for key, _title, default_on, critical in ALERT_KINDS:
        kinds[key] = True if critical else bool(raw_kinds.get(key, default_on))
    try:
        grace = int(override.get("link_grace_min", LINK_GRACE_DEFAULT))
    except (TypeError, ValueError):
        grace = LINK_GRACE_DEFAULT
    return {
        "enabled": bool(override.get("enabled", True)),
        "kinds": kinds,
        "link_grace_min": max(LINK_GRACE_MIN, min(grace, LINK_GRACE_MAX)),
    }


def set_alerts_config(
    *,
    enabled: bool | None = None,
    kinds: dict | None = None,
    link_grace_min: int | None = None,
) -> dict:
    """Сохраняет настройки уведомлений. Возвращает эффективные."""
    with _lock:
        data = _load_locked()
        override = data.setdefault("alerts", {})
        if enabled is not None:
            override["enabled"] = bool(enabled)
        if kinds is not None:
            stored = override.setdefault("kinds", {})
            for key, value in kinds.items():
                if key in ALERT_KEYS and key not in CRITICAL_ALERT_KEYS:
                    stored[key] = bool(value)
        if link_grace_min is not None:
            override["link_grace_min"] = int(link_grace_min)
        _save_locked()
    log.info("Настройки уведомлений о сбоях обновлены: %s", alerts_config())
    return alerts_config()


def alert_kind_enabled(kind: str | None) -> bool:
    """Уведомлять ли о сбое этой категории.

    Неизвестная категория (в том числе None — старый вызов без kind) считается
    обычной и подчиняется главному режиму: молча потерять новый вид алерта
    хуже, чем показать лишний.
    """
    if kind in CRITICAL_ALERT_KEYS:
        return True
    cfg = alerts_config()
    if not cfg["enabled"]:
        return False
    return bool(cfg["kinds"].get(kind, True)) if kind in ALERT_KEYS else True


# Длина хвоста ошибки в журнале. Панель — не лог: нужна строка, по которой
# видно, к кому идти (прокси, диск, Google), а не полный traceback.
INCIDENT_DETAILS_LIMIT = 160


def record_incident(
    kind: str | None,
    title: str,
    *,
    sent: bool,
    when: float,
    bump: bool = True,
    details: str = "",
    reason: str = "",
) -> None:
    """Пишет сбой в журнал. Повтор того же в окне склейки — счётчиком.

    Журнал ведётся независимо от настроек: админ, выключивший категорию,
    должен видеть в панели, что она всё-таки срабатывала.

    bump=False — не новый случай, а уточнение уже записанного: например
    «о том же обрыве наконец удалось сообщить». Счётчик при этом не растёт,
    иначе один провал считался бы за два — сначала датчиком, потом звонком.

    details — короткий хвост ошибки: «×18» без причины не говорит ничего,
    а «ProxyError: Host unreachable» сразу указывает на прокси.
    reason — КОД причины, по которой не позвонили (категория выключена,
    троттлинг, некому, не дозвонились). Раньше панель писала «уведомление
    не отправлялось» без объяснений, и отличить «я сам выключил» от
    «не смогли доставить» было нельзя. Отправленный алерт причину стирает.
    """
    with _lock:
        data = _load_locked()
        journal = data.setdefault("incidents", [])
        for item in journal:
            same = item.get("kind") == kind and item.get("title") == title
            if same and when - float(item.get("ts", 0.0)) < INCIDENT_MERGE_WINDOW:
                item["ts"] = when
                if bump:
                    item["count"] = int(item.get("count", 1)) + 1
                item["sent"] = bool(item.get("sent")) or sent
                if details:
                    item["details"] = details[:INCIDENT_DETAILS_LIMIT]
                item["reason"] = "" if item["sent"] else (reason or item.get("reason", ""))
                _save_locked()
                return
        journal.insert(0, {
            "kind": kind or "other",
            "title": title,
            # Первое срабатывание держим отдельно от последнего: «×18» без
            # периода не говорит, шло это минуту или трое суток.
            "first_ts": when,
            "ts": when,
            "count": 1,
            "sent": sent,
            "details": details[:INCIDENT_DETAILS_LIMIT],
            "reason": "" if sent else reason,
        })
        del journal[INCIDENT_LIMIT:]
        _save_locked()


def recent_incidents(limit: int = 8) -> list[dict]:
    """Последние инциденты, свежие первыми."""
    with _lock:
        journal = [dict(x) for x in _load_locked().get("incidents", [])]
    journal.sort(key=lambda x: float(x.get("ts", 0.0)), reverse=True)
    return journal[: max(0, limit)]


def incidents_since(since: float) -> int:
    """Сколько сбоев (с учётом повторов) случилось после указанного времени."""
    with _lock:
        journal = [dict(x) for x in _load_locked().get("incidents", [])]
    return sum(
        int(x.get("count", 1)) for x in journal if float(x.get("ts", 0.0)) >= since
    )


def clear_incidents() -> int:
    """Очищает журнал инцидентов. Возвращает, сколько записей удалено.

    Нужен подготовке к прод-запуску (scripts/purge_data.py): тестовые сбои
    не должны висеть в карточке «Здоровье» рядом с боевыми. Остальные
    настройки — состав финансистов, whitelist, админы, напоминания — не
    затрагиваются: чистится только история.
    """
    with _lock:
        data = _load_locked()
        removed = len(data.get("incidents", []))
        if removed:
            data["incidents"] = []
            _save_locked()
    return removed
