"""Общие фикстуры: токен-заглушка и изоляция путей во временный каталог."""
from __future__ import annotations

import os
import tempfile

# Принудительно, не setdefault: подпись initData в интеграционных тестах
# считается от этого токена, внешний TELEGRAM_BOT_TOKEN сломал бы проверку.
os.environ["TELEGRAM_BOT_TOKEN"] = "123456:TESTTOKEN"

# Тесты ВСЕГДА идут в локальный бэкенд. Переменные окружения приоритетнее
# .env, и это принципиально: в рабочем каталоге развёрнутого бота лежит
# боевой .env со STORAGE_BACKEND=google — без этой страховки `pytest`
# писал бы тестовые заявки в настоящую Google-таблицу и Drive.
os.environ["STORAGE_BACKEND"] = "local"
os.environ["REGISTRY_XLSX_FILE"] = ""
os.environ["GOOGLE_SHEET_ID"] = ""
os.environ["GOOGLE_DRIVE_FOLDER_ID"] = ""

# И рабочие ФАЙЛЫ тоже мимо боевых. Фикстура tmp_paths подменяет их только
# тем тестам, которые её просят, а тест без неё пишет в каталог проекта —
# то есть, на сервере, в настоящие data/. Так уже случилось 25.08.2026:
# tests/test_proxy_and_health.py крутит пульс без изоляции, датчик связи
# записал «ошибку» в боевой журнал инцидентов, а файл, переписанный из
# контейнера от root, стал боту недоступен на запись. Каталог общий на
# прогон: тестам, которым нужна чистота, её даёт tmp_paths.
_SANDBOX = tempfile.mkdtemp(prefix="invoice-bot-tests-")
os.environ["RUNTIME_SETTINGS_FILE"] = os.path.join(_SANDBOX, "bot_settings.json")
os.environ["SECURITY_DB_FILE"] = os.path.join(_SANDBOX, "security.db")
os.environ["USER_DIRECTORY_FILE"] = os.path.join(_SANDBOX, "known_users.json")
os.environ["STORAGE_DIR"] = os.path.join(_SANDBOX, "storage")

from datetime import date, datetime  # noqa: E402
from decimal import Decimal  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from bot.models import InvoiceRequest, Urgency, new_request_id  # noqa: E402
from config import settings  # noqa: E402

NOW = datetime(2026, 8, 3, 21, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def make_request(**overrides) -> InvoiceRequest:
    """Заявка с разумными значениями по умолчанию для тестов."""
    telegram_id = overrides.pop("telegram_id", 1001)
    base = dict(
        telegram_id=telegram_id,
        sender_username="@tester",
        sender_name="Тест Тестов",
        amount=Decimal("125000.50"),
        currency="RUB",
        counterparty="ООО «Ромашка»",
        article="Аренда",
        planned_date=date(2026, 8, 15),
        comment="Аренда за август",
        urgency=Urgency.NORMAL,
        has_invoice=False,
        requisites="ИНН 7707083893",
        created_at=NOW,
        request_id=new_request_id(NOW, telegram_id),
    )
    base.update(overrides)
    return InvoiceRequest(**base)


@pytest.fixture()
def tmp_paths(tmp_path, monkeypatch):
    """Реестр, хранилище и настройки — во временном каталоге."""
    # Дублирует страховку из окружения: ни один тест не ходит в Google.
    monkeypatch.setattr(settings, "storage_backend", "local")
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    monkeypatch.setattr(settings, "registry_file", str(tmp_path / "registry.xlsx"))
    monkeypatch.setattr(settings, "registry_xlsx_file", "")
    monkeypatch.setattr(settings, "runtime_settings_file", str(tmp_path / "settings.json"))
    monkeypatch.setattr(settings, "security_db_file", str(tmp_path / "security.db"))
    monkeypatch.setattr(settings, "user_directory_file", str(tmp_path / "known_users.json"))

    import services.runtime_settings as rs
    import services.user_directory as directory

    monkeypatch.setattr(rs, "_cache", None)
    # Справочник тоже держит карту в модуле: без сброса @username, записанный
    # одним тестом, «переезжал» в следующий и ломал соседей.
    monkeypatch.setattr(directory, "_cache", None)
    # Сбрасываем кэшированные списки из реального .env.
    settings.__dict__.pop("finance_recipients", None)
    settings.__dict__.pop("allowed_user_ids", None)
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "finance_chat_ids_raw", "")
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "")
    monkeypatch.setattr(settings, "admin_ids_raw", "")
    yield tmp_path
    settings.__dict__.pop("finance_recipients", None)
    settings.__dict__.pop("allowed_user_ids", None)
    settings.__dict__.pop("admin_ids", None)
