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
    "autofill": {},
}


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
                    "autofill": dict(raw.get("autofill", {})),
                }
            except (ValueError, OSError):
                log.exception("Не удалось прочитать настройки %s — начинаю с пустых", path)
                _cache = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in _DEFAULTS.items()}
        else:
            _cache = {
                "finance": [], "finance_off": [], "allowed": [], "allowed_off": [],
                "admins": [],
                "admin_chats": {},
    # Заявки на доступ: {id: {"username": ..., "ts": ...}}. Живут здесь же,
    # чтобы не заводить второй файл с блокировкой ради пары записей.
    "access_requests": {}, "backup": {}, "reminders": {}, "autofill": {},
            }
    # Файл мог быть создан версией без этих ключей.
    _cache.setdefault("finance_off", [])
    _cache.setdefault("allowed_off", [])
    _cache.setdefault("admins", [])
    _cache.setdefault("access_requests", {})
    _cache.setdefault("reminders", {})
    _cache.setdefault("autofill", {})
    return _cache


def _save_locked() -> None:
    path = settings.runtime_settings_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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


def set_autofill(enabled: bool) -> bool:
    with _lock:
        data = _load_locked()
        data.setdefault("autofill", {})["enabled"] = bool(enabled)
        _save_locked()
    log.info("Настройки: автозаполнение из счёта — %s", "включено" if enabled else "выключено")
    return autofill_enabled()
