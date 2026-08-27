"""Модели предметной области: заявка на оплату."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class Urgency(StrEnum):
    """Срочность платежа."""

    NORMAL = "Обычная"
    URGENT = "Срочно"

    @property
    def is_urgent(self) -> bool:
        return self is Urgency.URGENT


# Поддерживаемые валюты (первая — по умолчанию).
CURRENCIES: list[str] = ["RUB", "USD", "EUR", "KZT", "CNY"]

# Статьи расходов (последняя — «Прочее»; в формах можно ввести свою).
# Держать в синхроне с webapp/index.html (ARTICLES).
ARTICLES: list[str] = [
    "Аренда",
    "Закупка товаров",
    "Услуги подрядчиков",
    "Хостинг и ПО",
    "Командировки",
    "Реклама и маркетинг",
    "Прочее",
]

# Статусы заявки: ключ callback-данных → (подпись кнопки, значение в реестре).
REQUEST_STATUSES: dict[str, tuple[str, str]] = {
    "PAID": ("✅ Оплачено", "Оплачена"),
    "DEFERRED": ("⏸ Отложено", "Отложена"),
    "REJECTED": ("❌ Отклонено", "Отклонена"),
}

# Начальный статус заявки (InvoiceRequest.status).
STATUS_NEW = "Новая"
# Отзыв автором: ставится не кнопкой финансиста, а самим автором из «Моих
# заявок», и только пока заявку никто не тронул (статус ещё «Новая»).
STATUS_WITHDRAWN = "Отозвана"


# Формат ID заявки — защита от произвольных значений в callback_data и API.
REQUEST_ID_RE = re.compile(r"^INV-\d{8}-\d{6}-\d{4}$")


def new_request_id(now: datetime, telegram_id: int) -> str:
    """ID заявки вида INV-20260803-142233-0451.

    Суффикс — из telegram_id И микросекунд: разные сотрудники в одну
    секунду и повторные отправки одного сотрудника дают разные ID.
    Уникальность дополнительно закреплена UNIQUE-индексом в SQLite-реестре.
    """
    suffix = (telegram_id + now.microsecond) % 10000
    return f"INV-{now.strftime('%Y%m%d-%H%M%S')}-{suffix:04d}"


@dataclass
class InvoiceRequest:
    """Заявка на оплату, собранная из диалога Telegram или формы Mini App."""

    # Метаданные отправителя
    telegram_id: int
    sender_username: str        # @username или "—"
    sender_name: str            # ФИО / отображаемое имя

    # Данные платежа
    amount: Decimal
    currency: str
    counterparty: str
    comment: str
    urgency: Urgency
    article: str = ""                    # статья расходов
    planned_date: date | None = None     # плановая дата оплаты
    # Срок исполнения работ по договору. Свободный текст, а НЕ дата: в счетах
    # он сплошь нечисловой — «текущий месяц», «поставка в декабре», «услуга
    # на 6 месяцев». Необязательное поле: у хостинга и подписок срока работ нет.
    work_deadline: str = ""

    # Счёт: либо файл, либо реквизиты (если счёта нет)
    has_invoice: bool = True
    file_url: str = ""          # ссылка (Google Drive) или путь к файлу счёта
    file_name: str = ""
    # Дополнительные документы: договор, акт, спецификация. Список ссылок —
    # тех же, что и у счёта. Пусто у подавляющего большинства заявок, поэтому
    # отдельной колонкой в конце, а не расширением «Ссылки на счет».
    extra_files: list[str] = field(default_factory=list)
    requisites: str = ""        # заполняется, если счёта нет

    # Системные
    created_at: datetime | None = None
    request_id: str = ""
    status: str = STATUS_NEW

    @property
    def payment_source(self) -> str:
        """На основании чего платить: "invoice" | "requisites" | "none".

        Один источник истины на все тексты — карточку финансисту, ответ
        автору, PDF, групповую сводку и «Мои заявки». Пока их различали
        условием `if has_invoice: … else: …`, каждое место утверждало «оплата
        по реквизитам» и там, где реквизитов не было: случаев ТРИ, а веток
        писали две. Правится здесь — исправляется везде.
        """
        if self.has_invoice:
            return "invoice"
        return "requisites" if self.requisites else "none"

    def as_sheet_row(self) -> list[str]:
        """Строка реестра. Порядок строго совпадает с SHEET_HEADERS."""
        created = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        planned = self.planned_date.strftime("%d.%m.%Y") if self.planned_date else ""
        return [
            created,                                        # Дата внесения в реестр
            planned,                                        # Плановая дата оплаты
            f"{self.sender_username} ({self.sender_name})",  # Сотрудник по заявке
            self.counterparty,                              # Контрагент
            f"{self.amount:.2f}",                           # Сумма
            self.article,                                   # Статья
            self.status,                                    # Статус оплаты
            self.comment,                                   # Комментарий
            self.file_url,                                  # Ссылка на счет
            self.currency,                                  # Валюта
            self.urgency.value,                             # Срочность
            self.requisites,                                # Реквизиты
            self.request_id,                                # ID заявки
            str(self.telegram_id),                          # Telegram ID
            self.work_deadline,                             # Срок исполнения работ
            "\n".join(self.extra_files),                     # Дополнительные документы
        ]


def excel_safe(value: str) -> str:
    """Защита от инъекции формул в Excel/CSV/xlsx.

    Ячейка, начинающаяся с = + - @, интерпретируется Excel как формула —
    экранируем апострофом (стандартная мера, Excel показывает текст как есть).
    Для Google Sheets не нужно: там пишем с valueInputOption=RAW.
    """
    if value and value[0] in ("=", "+", "-", "@"):
        return "'" + value
    return value


# Заголовки реестра — единый источник правды для порядка колонок.
# Первые девять — ровно по ТЗ; дальше — служебные колонки решения.
SHEET_HEADERS: list[str] = [
    "Дата внесения в реестр",
    "Плановая дата оплаты",
    "Сотрудник по заявке",
    "Контрагент",
    "Сумма",
    "Статья",
    "Статус оплаты",
    "Комментарий",
    "Ссылка на счет",
    "Валюта",
    "Срочность",
    "Реквизиты",
    "ID заявки",
    "Telegram ID",
    # Новая колонка приписана В КОНЕЦ намеренно. Первые девять фиксированы ТЗ,
    # а вставка в середину сдвинула бы «ID заявки» и «Telegram ID» — в уже
    # заполненных реестрах (Sheets, xlsx, SQLite) все старые строки уехали бы
    # на колонку влево. Дописать в конец — единственное безопасное место.
    "Срок исполнения работ по договору",
    # Тоже в конец и по той же причине, что и предыдущая: вставка в середину
    # сдвинула бы все заполненные строки на колонку влево.
    "Дополнительные документы",
]
