"""Конфигурация приложения. Секреты читаются из .env / переменных окружения.

Ни один секрет не хранится в коде — только в .env (в .gitignore) или в
переменных окружения среды исполнения.
"""
from __future__ import annotations

import logging
from functools import cached_property
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_id_list(raw: str | None) -> list[int]:
    """Парсит "111, 222" -> [111, 222]; пусто/None -> [].

    Нечисловые элементы («@username» и опечатки) пропускаются с warning,
    а не роняют приложение при первом обращении к списку.
    """
    result: list[int] = []
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if chunk.lstrip("-").isdigit():
            result.append(int(chunk))
        else:
            logging.getLogger(__name__).warning(
                "Игнорирую нечисловой id в списке: %r (нужен числовой Telegram ID)", chunk
            )
    return result


def _parse_str_list(raw: str | None) -> list[str]:
    """Парсит "@fin, 123" -> ["@fin", "123"]; пусто/None -> []."""
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- Telegram ----
    telegram_bot_token: str = Field(..., alias="TELEGRAM_BOT_TOKEN")
    # Списки читаем как строку и парсим в свойствах — так избегаем JSON-декодинга
    # pydantic-settings для полей-списков.
    finance_chat_ids_raw: str = Field("", alias="FINANCE_CHAT_IDS")
    allowed_user_ids_raw: str = Field("", alias="ALLOWED_USER_IDS")
    # Админы бота: могут управлять финансистами и whitelist прямо из Telegram.
    admin_ids_raw: str = Field("", alias="ADMIN_IDS")
    # Справочник СБ «user_id:ФИО» через запятую: в реестр попадает
    # подтверждённое ФИО, а не имя из Telegram-профиля (его можно сменить).
    employee_names_raw: str = Field("", alias="EMPLOYEE_NAMES")

    # ---- Хранилище ----
    # Бэкенд реестра и файлов счетов: "local" (CSV + каталог) или "google"
    # (Google Sheets + Google Drive, нужен service account).
    storage_backend: str = Field("local", alias="STORAGE_BACKEND")

    # --- Google (для STORAGE_BACKEND=google) ---
    # JSON-ключ service account (файл НЕ коммитится, каталог secrets/ в .gitignore).
    google_credentials_file: str = Field(
        "secrets/service_account.json", alias="GOOGLE_CREDENTIALS_FILE"
    )
    google_sheet_id: str = Field("", alias="GOOGLE_SHEET_ID")
    google_drive_folder_id: str = Field("", alias="GOOGLE_DRIVE_FOLDER_ID")
    # OAuth-токен владельца папки для Drive: на ЛИЧНЫХ аккаунтах у service
    # account нет квоты хранилища (ограничение Google), файлами должен
    # владеть человек. Создаётся scripts/google_oauth_setup.py; если файла
    # нет — Drive работает через service account (путь для Shared Drive).
    google_oauth_token_file: str = Field(
        "secrets/google_oauth_token.json", alias="GOOGLE_OAUTH_TOKEN_FILE"
    )

    # --- Локальное хранилище ---
    # Каталог, куда складываются файлы счетов. Относительный путь — от каталога
    # проекта (см. storage_path).
    storage_dir: Path = Field(Path("storage"), alias="STORAGE_DIR")
    # Необязательное xlsx-зеркало реестра (шаблон «Реестр.xlsx» из ТЗ):
    # если задано, заявки и статусы дублируются в этот файл при любом бэкенде.
    registry_xlsx_file: str = Field("", alias="REGISTRY_XLSX_FILE")
    # Имя (или абсолютный путь) xlsx-реестра заявок (формат шаблона ТЗ).
    registry_file: str = Field("registry.xlsx", alias="REGISTRY_FILE")
    # Файл справочника @username → chat_id (относительно каталога проекта).
    user_directory_file: str = Field("data/known_users.json", alias="USER_DIRECTORY_FILE")
    # Файл динамических настроек (финансисты/whitelist, добавленные из Telegram).
    runtime_settings_file: str = Field("data/bot_settings.json", alias="RUNTIME_SETTINGS_FILE")
    # SQLite с аудит-журналом и отпечатками дедупа заявок.
    security_db_file: str = Field("data/security.db", alias="SECURITY_DB_FILE")
    # Окно поиска дублей заявки в днях (0 = проверка выключена).
    dedup_window_days: int = Field(14, alias="DEDUP_WINDOW_DAYS")
    # Автопроверка «похож ли файл на счёт» (текст PDF + OCR). Мягкая:
    # не блокирует подачу, а предупреждает автора и финансиста.
    invoice_check_enabled: bool = Field(True, alias="INVOICE_CHECK")
    # Ежедневный автобэкап данных: время ЧЧ:ММ в TIMEZONE (пусто = выключен).
    backup_time: str = Field("03:30", alias="BACKUP_TIME")
    # Сколько последних архивов хранить в data/backups.
    backup_keep: int = Field(7, alias="BACKUP_KEEP")

    # ---- Mini App / HTTP API ----
    # Публичный HTTPS-адрес формы Mini App. Пусто = мини-приложение выключено,
    # бот работает пошаговой чат-формой (удобно для локальной разработки).
    webapp_url: str = Field("", alias="WEBAPP_URL")
    # Короткое имя Mini App из @BotFather (/newapp, Web App URL = WEBAPP_URL).
    # Если задано — кнопка в группе/канале открывает форму прямой ссылкой
    # t.me/<бот>/<имя> сразу поверх чата, без перехода в личку.
    miniapp_short_name: str = Field("", alias="MINIAPP_SHORT_NAME")
    # Где слушает встроенный HTTP API (за реверс-прокси).
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8080, alias="API_PORT")

    # ---- Сеть ----
    # Прокси для доступа к api.telegram.org, если он недоступен с сервера.
    # Форматы: socks5://[user:pass@]host:port или http://host:port.
    # Можно НЕСКОЛЬКО через запятую — при старте бот выберет первый
    # отвечающий (фейловер). Пусто = прямое подключение.
    proxy_url: str = Field("", alias="PROXY_URL")

    # ---- Прочее ----
    # Название организации — печатается в шапке PDF-документа заявки.
    org_name: str = Field("Beoma", alias="ORG_NAME")
    # Метка контура: непустая — над формой висит плашка. Нужна стенду, чтобы
    # его нельзя было спутать с боевым ботом: оба выглядят одинаково, а
    # заявка со стенда никому не уходит.
    env_label: str = Field("", alias="ENV_LABEL")
    # Реквизиты организации строкой — вторая строка шапки PDF. Пусто —
    # печатается нейтральная подпись «Финансовый документ».
    org_details: str = Field("", alias="ORG_DETAILS")
    # ИНН НАШИХ юрлиц и ИП через запятую. Нужен автозаполнению: мы сами не
    # можем быть контрагентом, платим не себе. В счетах наша сторона стоит
    # рядом с чужой и выглядит так же («ИП Иванов И.И.»), а PDF-слой к тому
    # же путает колонки и подставляет метку поставщика к строке покупателя —
    # без этого списка отличить их нечем. Пусто = проверка выключена.
    org_inn: str = Field("", alias="ORG_INN")
    # Напоминания о сроках: финансистам — что оплатить завтра, админам —
    # что просрочено. Раз в сутки в reminder_time (в TIMEZONE).
    reminders_enabled: bool = Field(True, alias="REMINDERS_ENABLED")
    reminder_time: str = Field("09:30", alias="REMINDER_TIME")

    # Бета: предлагать заполнение формы по распознанному счёту.
    invoice_autofill: bool = Field(True, alias="INVOICE_AUTOFILL")

    log_level: str = Field("INFO", alias="LOG_LEVEL")
    timezone: str = Field("Europe/Moscow", alias="TIMEZONE")

    @cached_property
    def proxy_urls(self) -> list[str]:
        """Кандидаты прокси из PROXY_URL по порядку предпочтения."""
        return _parse_str_list(self.proxy_url)

    @cached_property
    def finance_recipients(self) -> list[str]:
        """Получатели срочных: @username или числовой id (строками)."""
        return _parse_str_list(self.finance_chat_ids_raw)

    @cached_property
    def allowed_user_ids(self) -> list[int]:
        return _parse_id_list(self.allowed_user_ids_raw)

    @cached_property
    def admin_ids(self) -> list[int]:
        return _parse_id_list(self.admin_ids_raw)

    @cached_property
    def bot_id(self) -> int:
        """id самого бота — это числовая часть токена до двоеточия.

        Нужен, чтобы не показывать бота в списке пользователей: он попадал
        в справочник со своих же постов в канале.
        """
        head = self.telegram_bot_token.split(":", 1)[0]
        return int(head) if head.isdigit() else 0

    @cached_property
    def employee_names(self) -> dict[int, str]:
        """Справочник подтверждённых ФИО: {telegram_id: "Фамилия Имя"}."""
        result: dict[int, str] = {}
        for chunk in self.employee_names_raw.split(","):
            uid, _, name = chunk.strip().partition(":")
            if uid.strip().isdigit() and name.strip():
                result[int(uid.strip())] = name.strip()
        return result

    def employee_name_for(self, user_id: int) -> str | None:
        return self.employee_names.get(user_id)

    @property
    def own_inn(self) -> set[str]:
        """ИНН наших организаций (только цифры) — множество для проверок."""
        return {
            digits
            for part in self.org_inn.split(",")
            if (digits := "".join(ch for ch in part if ch.isdigit()))
        }

    @property
    def webapp_enabled(self) -> bool:
        """True, если задан WEBAPP_URL — включает форму Mini App и HTTP API."""
        return bool(self.webapp_url.strip())

    @property
    def storage_path(self) -> Path:
        """Абсолютный путь к каталогу файлов счетов.

        Относительный STORAGE_DIR разрешается от каталога проекта,
        а не от текущего рабочего каталога.
        """
        p = self.storage_dir
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def registry_path(self) -> Path:
        """Абсолютный путь к xlsx-реестру.

        Если REGISTRY_FILE относительный — кладём его внутрь STORAGE_DIR.
        Бэккомпат: старое значение *.csv прозрачно превращается в *.xlsx
        (CSV-реестр упразднён; прежний файл остаётся на диске как архив).
        """
        p = Path(self.registry_file)
        if p.suffix.lower() == ".csv":
            p = p.with_suffix(".xlsx")
        return p if p.is_absolute() else self.storage_path / p

    @property
    def user_directory_path(self) -> Path:
        """Абсолютный путь к справочнику @username → id."""
        p = Path(self.user_directory_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def runtime_settings_path(self) -> Path:
        """Абсолютный путь к файлу динамических настроек."""
        p = Path(self.runtime_settings_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def security_db_path(self) -> Path:
        """Абсолютный путь к SQLite аудита/дедупа."""
        p = Path(self.security_db_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def storage_is_google(self) -> bool:
        return self.storage_backend.strip().lower() == "google"

    @property
    def google_credentials_path(self) -> Path:
        p = Path(self.google_credentials_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def google_oauth_token_path(self) -> Path:
        p = Path(self.google_oauth_token_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p

    @property
    def registry_xlsx_path(self) -> Path | None:
        """Путь к xlsx-зеркалу реестра (None = зеркало выключено)."""
        raw = self.registry_xlsx_file.strip()
        if not raw:
            return None
        p = Path(raw)
        return p if p.is_absolute() else self.storage_path / p


settings = Settings()  # type: ignore[call-arg]
