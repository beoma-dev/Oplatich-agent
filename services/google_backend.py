"""Google-бэкенд: реестр в Google Sheets, файлы счетов в Google Drive.

Авторизация — service account (JSON-ключ в secrets/, каталог в .gitignore).
Доступ к данным определяется правами на таблицу и папку: они расшариваются
только на service account и уполномоченных лиц, публичных ссылок бот не
создаёт — «Ссылка на счет» открывается только тем, у кого есть права
на папку (защита файла счёта и папки хранения по ТЗ).

Все функции синхронные (googleapiclient) — вызывать через to_thread
из storage-фасада.
"""
from __future__ import annotations

import io
import logging
import re
from collections import Counter
from functools import lru_cache

from bot.models import SHEET_HEADERS, InvoiceRequest
from config import settings

log = logging.getLogger(__name__)

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def _col_letter(index: int) -> str:
    """0 → A, 1 → B, … 13 → N."""
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


_ID_LETTER = _col_letter(SHEET_HEADERS.index("ID заявки"))
_STATUS_LETTER = _col_letter(SHEET_HEADERS.index("Статус оплаты"))
_CP_LETTER = _col_letter(SHEET_HEADERS.index("Контрагент"))
_LAST_LETTER = _col_letter(len(SHEET_HEADERS) - 1)
_AMOUNT_IDX = SHEET_HEADERS.index("Сумма")
_STATUS_IDX = SHEET_HEADERS.index("Статус оплаты")
_URGENCY_IDX = SHEET_HEADERS.index("Срочность")
_CP_IDX = SHEET_HEADERS.index("Контрагент")
_REQUISITES_IDX = SHEET_HEADERS.index("Реквизиты")
_ID_IDX = SHEET_HEADERS.index("ID заявки")
_TELEGRAM_IDX = SHEET_HEADERS.index("Telegram ID")

# Ширины колонок в пикселях (символы xlsx × ~7.5) — тот же вид, что у зеркала.
_PIXEL_WIDTHS = [128, 112, 195, 210, 105, 165, 105, 315, 315, 60, 82, 315, 195, 90, 225]

# Колонки с выравниванием по центру (данные).
_CENTER_IDX = {1, 6, 9, 10}  # Плановая дата, Статус, Валюта, Срочность


def _rgb(hex_color: str) -> dict:
    return {
        "red": int(hex_color[0:2], 16) / 255,
        "green": int(hex_color[2:4], 16) / 255,
        "blue": int(hex_color[4:6], 16) / 255,
    }


def _style_requests(sheet_id: int) -> list[dict]:
    """Оформление листа: как у xlsx-реестра (шапка, ширины, фильтр, цвета)."""
    n = len(SHEET_HEADERS)
    requests: list[dict] = [
        # Шапка: тёмно-синяя, белый жирный, по центру, с переносом.
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                          "startColumnIndex": 0, "endColumnIndex": n},
                "cell": {"userEnteredFormat": {
                    "backgroundColor": _rgb("1E2A5A"),
                    "horizontalAlignment": "CENTER",
                    "verticalAlignment": "MIDDLE",
                    "wrapStrategy": "WRAP",
                    "textFormat": {"bold": True, "fontSize": 10,
                                   "foregroundColor": _rgb("FFFFFF")},
                }},
                "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,wrapStrategy,textFormat)",
            }
        },
        # Закрепление шапки + высота строки.
        {
            "updateSheetProperties": {
                "properties": {"sheetId": sheet_id,
                               "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "ROWS",
                          "startIndex": 0, "endIndex": 1},
                "properties": {"pixelSize": 36},
                "fields": "pixelSize",
            }
        },
        # Фильтр по всем колонкам.
        {
            "setBasicFilter": {
                "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0,
                                     "startColumnIndex": 0, "endColumnIndex": n}}
            }
        },
        # Сумма: числовой формат + вправо.
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "startColumnIndex": _AMOUNT_IDX,
                          "endColumnIndex": _AMOUNT_IDX + 1},
                "cell": {"userEnteredFormat": {
                    "numberFormat": {"type": "NUMBER", "pattern": "#,##0.00"},
                    "horizontalAlignment": "RIGHT",
                }},
                "fields": "userEnteredFormat(numberFormat,horizontalAlignment)",
            }
        },
    ]
    # Ширины колонок.
    for idx, pixels in enumerate(_PIXEL_WIDTHS[:n]):
        requests.append({
            "updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                          "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": pixels},
                "fields": "pixelSize",
            }
        })
    # Выравнивание по центру для дат/валюты/срочности/статуса.
    for idx in sorted(_CENTER_IDX):
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1,
                          "startColumnIndex": idx, "endColumnIndex": idx + 1},
                "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
                "fields": "userEnteredFormat.horizontalAlignment",
            }
        })
    # Цвета статусов и красная «Срочно» — условное форматирование.
    status_colors = [
        ("Оплачена", "D1E7DD"),
        ("Отложена", "FFF3CD"),
        ("Отклонена", "F8D7DA"),
        ("Отозвана", "E2E3E5"),
    ]
    status_range = {"sheetId": sheet_id, "startRowIndex": 1,
                    "startColumnIndex": _STATUS_IDX, "endColumnIndex": _STATUS_IDX + 1}
    for value, color in status_colors:
        requests.append({
            "addConditionalFormatRule": {
                "rule": {
                    "ranges": [status_range],
                    "booleanRule": {
                        "condition": {"type": "TEXT_EQ",
                                      "values": [{"userEnteredValue": value}]},
                        "format": {"backgroundColor": _rgb(color)},
                    },
                },
                "index": 0,
            }
        })
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{"sheetId": sheet_id, "startRowIndex": 1,
                            "startColumnIndex": _URGENCY_IDX,
                            "endColumnIndex": _URGENCY_IDX + 1}],
                "booleanRule": {
                    "condition": {"type": "TEXT_EQ",
                                  "values": [{"userEnteredValue": "Срочно"}]},
                    "format": {"textFormat": {"bold": True,
                                              "foregroundColor": _rgb("C62828")}},
                },
            },
            "index": 0,
        }
    })
    return requests


def _ensure_style_sync() -> None:
    """Оформляет лист один раз: маркер «уже оформлен» — закреплённая шапка."""
    global _checked_style
    if _checked_style:
        return
    sheet_id = _target_sheet()[0]
    props = next(
        (
            item["properties"]
            for item in _sheets()
            .spreadsheets()
            .get(
                spreadsheetId=settings.google_sheet_id,
                fields="sheets(properties(sheetId,gridProperties(frozenRowCount)))",
            )
            .execute()
            .get("sheets", [])
            if item.get("properties", {}).get("sheetId") == sheet_id
        ),
        {},
    )
    if props.get("gridProperties", {}).get("frozenRowCount", 0) >= 1:
        _checked_style = True
        return
    _sheets().spreadsheets().batchUpdate(
        spreadsheetId=settings.google_sheet_id,
        body={"requests": _style_requests(sheet_id)},
    ).execute()
    _checked_style = True
    log.info("Google-таблица оформлена: шапка, ширины, фильтр, статусные цвета")


@lru_cache(maxsize=1)
def _credentials():
    from google.oauth2.service_account import Credentials

    return Credentials.from_service_account_file(
        str(settings.google_credentials_path), scopes=_SCOPES
    )


@lru_cache(maxsize=1)
def _sheets():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=_credentials(), cache_discovery=False)


@lru_cache(maxsize=1)
def _drive_credentials():
    """Drive: OAuth-токен владельца папки, если есть; иначе service account.

    На личных Google-аккаунтах service account не может владеть файлами
    (нет квоты хранилища) — загруженными счетами владеет ваш аккаунт,
    что заодно закрывает требование ТЗ о защите папки.
    """
    token_path = settings.google_oauth_token_path
    if token_path.exists():
        from google.oauth2.credentials import Credentials as UserCredentials

        log.info("Drive: используется OAuth-токен владельца (%s)", token_path.name)
        return UserCredentials.from_authorized_user_file(
            str(token_path), scopes=["https://www.googleapis.com/auth/drive"]
        )
    return _credentials()


@lru_cache(maxsize=1)
def _drive():
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=_drive_credentials(), cache_discovery=False)


def _values():
    return _sheets().spreadsheets().values()


# Кэш «gid → название листа» на процесс: название нужно в каждом диапазоне,
# а лишний запрос к API на каждую операцию реестр не переживёт (см. R9).
_sheet_ref: tuple[int, str] | None = None


def reset_sheet_ref() -> None:
    """Сбрасывает кэш листа. Нужен тестам и смене таблицы на лету."""
    global _sheet_ref
    _sheet_ref = None
    reset_sheet_checks()


def _target_sheet() -> tuple[int, str]:
    """(sheetId, название) листа реестра — того, что задан GOOGLE_SHEET_GID.

    Отсутствующий gid — это ОШИБКА, а не повод взять первый лист. Молча
    уехавшая в соседнюю вкладку заявка хуже громкого отказа: отказ поднимет
    критичный алерт «Заявка не сохранилась в реестр», который не отключается
    ничем, а тихая запись не туда обнаружится через месяц при сверке.
    """
    global _sheet_ref
    if _sheet_ref is not None:
        return _sheet_ref
    gid = settings.google_sheet_gid
    sheets = (
        _sheets()
        .spreadsheets()
        .get(
            spreadsheetId=settings.google_sheet_id,
            fields="sheets(properties(sheetId,title))",
        )
        .execute()
        .get("sheets", [])
    )
    for item in sheets:
        props = item.get("properties", {})
        if props.get("sheetId") == gid:
            _sheet_ref = (gid, props.get("title", ""))
            return _sheet_ref
    known = ", ".join(
        f"«{i.get('properties', {}).get('title', '?')}» (gid={i.get('properties', {}).get('sheetId')})"
        for i in sheets
    ) or "их нет"
    raise RuntimeError(
        f"В таблице нет листа с gid={gid}; есть: {known}. "
        "GOOGLE_SHEET_GID берётся из адреса таблицы после #gid=."
    )


def _rng(a1: str) -> str:
    """Диапазон с явным именем листа.

    Без имени Google адресует ПЕРВЫЙ лист книги: пока реестр был один, это
    сходило с рук, а рядом с любой второй вкладкой («Справочник» и т. п.)
    перестановка вкладок тихо уводит записи не туда. Апостроф в названии
    листа по правилам A1 удваивается.
    """
    title = _target_sheet()[1].replace("'", "''")
    return f"'{title}'!{a1}"


# Шапка и оформление проверяются один раз на процесс. Обе операции по
# смыслу однократны за всю жизнь таблицы, но звались на КАЖДУЮ заявку и
# стоили двух лишних обращений к API — около 1,7 с из 6 с на подачу
# (замеры reports/005, R19). Рестарт кэш сбрасывает: если таблицу подменят
# под работающим ботом, следующий запуск всё проверит заново.
_checked_header = False
_checked_style = False


def reset_sheet_checks() -> None:
    """Сбрасывает памятку о проверках. Нужна тестам и смене таблицы."""
    global _checked_header, _checked_style
    _checked_header = _checked_style = False


def _ensure_header_sync() -> None:
    """Пустой лист получает заголовки; существующий лист НЕ трогаем.

    Таблица может принадлежать СБ заказчика: чужая шапка, порядок и
    оформление — неприкосновенны. Наши первые 9 колонок совпадают
    с шаблоном ТЗ, служебные данные уходят правее без своих заголовков.
    """
    global _checked_header
    if _checked_header:
        return
    got = (
        _values()
        .get(spreadsheetId=settings.google_sheet_id, range=_rng("1:1"))
        .execute()
        .get("values", [])
    )
    if not got or not any(got[0]):
        _values().update(
            spreadsheetId=settings.google_sheet_id,
            range=_rng("A1"),
            valueInputOption="RAW",
            body={"values": [SHEET_HEADERS]},
        ).execute()
    _checked_header = True


def append_invoice_sync(request: InvoiceRequest) -> int:
    """Дописывает заявку в Google Таблицу. Возвращает порядковый номер.

    valueInputOption=RAW — обязательно: пользовательский ввод пишется как
    литеральный текст, «=IMPORTRANGE(...)» в контрагенте останется строкой,
    а не исполнится формулой. insertDataOption=OVERWRITE — новая строка не
    наследует оформление шапки и не ломает data validation листа.
    Сумма — настоящим числом (float безопасен: формат контролируем мы),
    чтобы в таблице работали сортировка и автосумма.
    """
    _ensure_header_sync()
    try:
        _ensure_style_sync()
    except Exception:  # noqa: BLE001 — оформление вторично, запись важнее
        log.exception("Не удалось оформить Google-таблицу")
    values: list = list(request.as_sheet_row())
    values[_AMOUNT_IDX] = float(request.amount)
    resp = (
        _values()
        .append(
            spreadsheetId=settings.google_sheet_id,
            range=_rng("A1"),
            valueInputOption="RAW",
            insertDataOption="OVERWRITE",
            body={"values": [values]},
        )
        .execute()
    )
    updated_range = resp.get("updates", {}).get("updatedRange", "")
    # Номер строки берём из части ПОСЛЕ «!»: имя листа вроде «SHEET1» иначе
    # ошибочно совпало бы с шаблоном ячейки.
    cell_ref = updated_range.split("!")[-1]
    m = re.search(r"[A-Z]+(\d+)", cell_ref)
    row = int(m.group(1)) if m else 0
    number = max(row - 1, 1)  # минус строка заголовков
    if request.extra_files:
        _apply_link_runs(row, "Дополнительные документы",
                         "\n".join(request.extra_files))
    log.info("Заявка %s записана в Google Sheets (№%s)", request.request_id, number)
    return number


def set_status_sync(request_id: str, status_text: str) -> dict[str, str] | None:
    """Меняет «Статус оплаты» по ID заявки. Возвращает строку или None."""
    ids = (
        _values()
        .get(
            spreadsheetId=settings.google_sheet_id,
            range=_rng(f"{_ID_LETTER}2:{_ID_LETTER}"),
        )
        .execute()
        .get("values", [])
    )
    for i, row in enumerate(ids):
        if row and row[0] == request_id:
            row_number = i + 2
            _values().update(
                spreadsheetId=settings.google_sheet_id,
                range=_rng(f"{_STATUS_LETTER}{row_number}"),
                valueInputOption="RAW",
                body={"values": [[status_text]]},
            ).execute()
            full = (
                _values()
                .get(
                    spreadsheetId=settings.google_sheet_id,
                    range=_rng(f"A{row_number}:{_LAST_LETTER}{row_number}"),
                )
                .execute()
                .get("values", [[]])[0]
            )
            padded = full + [""] * (len(SHEET_HEADERS) - len(full))
            log.info("Заявка %s: статус в Google Sheets → «%s»", request_id, status_text)
            return dict(zip(SHEET_HEADERS, padded, strict=False))
    return None


def _all_rows_sync() -> list[list[str]]:
    """Все строки реестра без шапки, дополненные до ширины SHEET_HEADERS.

    Google возвращает строки обрезанными по последней заполненной ячейке —
    дополняем, чтобы индексы колонок работали единообразно.
    """
    values = (
        _values()
        .get(
            spreadsheetId=settings.google_sheet_id,
            range=_rng(f"A2:{_LAST_LETTER}"),
        )
        .execute()
        .get("values", [])
    )
    width = len(SHEET_HEADERS)
    return [list(row) + [""] * (width - len(row)) for row in values]


# Колонки, где в одной ячейке лежит НЕСКОЛЬКО ссылок через перенос строки.
LINK_COLUMNS = ("Дополнительные документы", "Закрывающие документы")


def _link_runs(text: str) -> list[dict]:
    """Разметка «каждая строка — своя ссылка» для ячейки с несколькими URL.

    Sheets сам делает ссылку кликабельной, только если ВСЯ ячейка — один URL
    (проверено: «Ссылка на счет» получает hyperlink, а две ссылки через \n —
    уже нет). Ставить формулу =HYPERLINK нельзя: она требует USER_ENTERED,
    а он в этом реестре запрещён — через него пользовательский текст стал бы
    формулой. Поэтому размечаем текст напрямую: каждому куску свой link.
    """
    runs: list[dict] = []
    offset = 0
    for line in text.split("\n"):
        stripped = line.strip()
        run: dict = {"startIndex": offset, "format": {}}
        if stripped.startswith("http://") or stripped.startswith("https://"):
            run["format"] = {"link": {"uri": stripped}, "underline": True}
        runs.append(run)
        offset += len(line) + 1          # +1 на сам перенос строки
    return runs


def _apply_link_runs(row_number: int, header: str, text: str) -> None:
    """Делает ссылки в ячейке кликабельными. Сбой не отменяет саму запись."""
    if header not in LINK_COLUMNS or not text.strip():
        return
    sheet_id = _target_sheet()[0]
    column = SHEET_HEADERS.index(header)
    try:
        _sheets().spreadsheets().batchUpdate(
            spreadsheetId=settings.google_sheet_id,
            body={"requests": [{"updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_number - 1, "endRowIndex": row_number,
                    "startColumnIndex": column, "endColumnIndex": column + 1,
                },
                "rows": [{"values": [{"textFormatRuns": _link_runs(text)}]}],
                "fields": "textFormatRuns",
            }}]},
        ).execute()
    except Exception:  # noqa: BLE001 — ссылки уже в ячейке, кликабельность вторична
        log.exception("Не удалось сделать ссылки кликабельными в «%s»", header)


def set_cell_sync(request_id: str, header: str, value: str) -> dict[str, str] | None:
    """Меняет ОДНУ ячейку по ID заявки и имени колонки. None — заявки нет."""
    if header not in SHEET_HEADERS:
        raise ValueError(f"Неизвестная колонка реестра: {header!r}")
    letter = _col_letter(SHEET_HEADERS.index(header))
    ids = (
        _values()
        .get(spreadsheetId=settings.google_sheet_id,
             range=_rng(f"{_ID_LETTER}2:{_ID_LETTER}"))
        .execute()
        .get("values", [])
    )
    for i, row in enumerate(ids):
        if row and row[0] == request_id:
            row_number = i + 2
            _values().update(
                spreadsheetId=settings.google_sheet_id,
                range=_rng(f"{letter}{row_number}"),
                valueInputOption="RAW",
                body={"values": [[value]]},
            ).execute()
            _apply_link_runs(row_number, header, value)
            log.info("Заявка %s: колонка «%s» обновлена", request_id, header)
            return get_request_sync(request_id)
    return None


def get_request_sync(request_id: str) -> dict[str, str] | None:
    """Заявка по ID в формате SHEET_HEADERS (None — такой заявки нет)."""
    for row in _all_rows_sync():
        if row[_ID_IDX] == request_id:
            return dict(zip(SHEET_HEADERS, row, strict=True))
    return None


def recent_by_author_sync(telegram_id: int, limit: int) -> list[dict[str, str]]:
    """Последние заявки автора — новые сверху («Мои заявки»)."""
    author = str(telegram_id)
    mine = [row for row in _all_rows_sync() if row[_TELEGRAM_IDX] == author]
    return [
        dict(zip(SHEET_HEADERS, row, strict=True)) for row in reversed(mine[-limit:])
    ]


def delete_request_sync(request_id: str) -> bool:
    """Удаляет строку заявки из Google Таблицы. True — строка была и удалена."""
    ids = (
        _values()
        .get(spreadsheetId=settings.google_sheet_id, range=_rng(f"{_ID_LETTER}2:{_ID_LETTER}"))
        .execute()
        .get("values", [])
    )
    for i, row in enumerate(ids):
        if row and row[0] == request_id:
            row_number = i + 2  # +1 за шапку, +1 за нумерацию с единицы
            sheet_id = _target_sheet()[0]
            _sheets().spreadsheets().batchUpdate(
                spreadsheetId=settings.google_sheet_id,
                body={"requests": [{"deleteDimension": {"range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,  # API считает с нуля
                    "endIndex": row_number,
                }}}]},
            ).execute()
            log.info("Заявка %s удалена из Google Sheets (строка %s)", request_id, row_number)
            return True
    return False


def recent_requests_sync(limit: int) -> list[dict[str, str]]:
    """Последние заявки ВСЕХ авторов — панель финансиста (новые сверху)."""
    rows = _all_rows_sync()
    return [
        dict(zip(SHEET_HEADERS, row, strict=True)) for row in reversed(rows[-limit:])
    ]


def counterparty_book_sync(limit: int) -> list[dict[str, str]]:
    """Справочник контрагентов из Google Таблицы: имя + последние реквизиты."""
    counter: Counter[str] = Counter()
    last_pos: dict[str, int] = {}
    requisites: dict[str, str] = {}
    for i, row in enumerate(_all_rows_sync()):
        name = row[_CP_IDX].strip()
        if not name:
            continue
        counter[name] += 1
        last_pos[name] = i
        if row[_REQUISITES_IDX].strip():
            requisites[name] = row[_REQUISITES_IDX].strip()
    ordered = sorted(counter, key=lambda n: (-counter[n], -last_pos[n]))
    return [
        {"name": name, "requisites": requisites.get(name, "")}
        for name in ordered[:limit]
    ]


def recent_counterparties_sync(limit: int) -> list[str]:
    """Частые контрагенты из Google Таблицы."""
    values = (
        _values()
        .get(
            spreadsheetId=settings.google_sheet_id,
            range=_rng(f"{_CP_LETTER}2:{_CP_LETTER}"),
        )
        .execute()
        .get("values", [])
    )
    counter: Counter[str] = Counter()
    last_pos: dict[str, int] = {}
    for i, row in enumerate(values):
        name = (row[0] if row else "").strip()
        if name:
            counter[name] += 1
            last_pos[name] = i
    ordered = sorted(counter, key=lambda n: (-counter[n], -last_pos[n]))
    return ordered[:limit]


def upload_invoice_file_sync(content: bytes, filename: str) -> str:
    """Загружает файл счёта в папку Drive. Возвращает webViewLink.

    Права не расширяются: ссылка работает только у тех, кому расшарена папка.
    """
    from googleapiclient.http import MediaIoBaseUpload

    media = MediaIoBaseUpload(
        io.BytesIO(content), mimetype="application/octet-stream", resumable=False
    )
    created = (
        _drive()
        .files()
        .create(
            body={"name": filename, "parents": [settings.google_drive_folder_id]},
            media_body=media,
            fields="id, webViewLink",
            supportsAllDrives=True,
        )
        .execute()
    )
    link = created.get("webViewLink", "")
    log.info("Файл счёта %s загружен в Drive (%s)", filename, created.get("id"))
    return link


def all_request_ids_sync() -> list[str]:
    """Все ID заявок из таблицы — для сверки с xlsx-зеркалом."""
    col = SHEET_HEADERS.index("ID заявки")
    return [row[col].strip() for row in _all_rows_sync() if row[col].strip()]
