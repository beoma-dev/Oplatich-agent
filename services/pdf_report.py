"""PDF-документ заявки на оплату.

Формирует аккуратный печатный документ формата А4: фирменная шапка
организации, реквизиты заявки таблицами, сумма прописью, блок счёта или
платёжных реквизитов, служебный колонтитул. Кириллица — через вендоренные
шрифты DejaVu (assets/fonts, лицензия там же).

Генерация синхронная (reportlab) — вызывать через asyncio.to_thread.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from bot.models import InvoiceRequest
from config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Шрифты
# ---------------------------------------------------------------------------
_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
FONT = "DejaVu"
FONT_BOLD = "DejaVu-Bold"
_fonts_registered = False


def _register_fonts() -> None:
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(FONT, str(_FONT_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, str(_FONT_DIR / "DejaVuSans-Bold.ttf")))
    _fonts_registered = True


# ---------------------------------------------------------------------------
# Палитра документа
# ---------------------------------------------------------------------------
# Документ — бухгалтерская форма: чёрным по белому, без фирменных плашек.
INK = colors.black
MUTED = colors.HexColor("#555555")      # служебные подписи под линиями
LINE = colors.black                     # рамки таблиц — как в печатных формах
HEAD_BG = colors.HexColor("#EFEFEF")    # шапка табличной части

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm

_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def _date_words(value) -> str:
    """«4 августа 2026 г.» — как в бухгалтерских формах."""
    if value is None:
        return "—"
    return f"{value.day} {_MONTHS[value.month - 1]} {value.year} г."


# ---------------------------------------------------------------------------
# Сумма прописью
# ---------------------------------------------------------------------------
# (единственное, 2–4, 5+) — формы для целой части и копеек.
_CURRENCY_FORMS: dict[str, tuple[tuple[str, str, str], tuple[str, str, str]]] = {
    "RUB": (("рубль", "рубля", "рублей"), ("копейка", "копейки", "копеек")),
    "USD": (("доллар США", "доллара США", "долларов США"), ("цент", "цента", "центов")),
    "EUR": (("евро", "евро", "евро"), ("цент", "цента", "центов")),
    "KZT": (("тенге", "тенге", "тенге"), ("тиын", "тиына", "тиынов")),
    "CNY": (("юань", "юаня", "юаней"), ("фэнь", "фэня", "фэней")),
}


def _plural(n: int, forms: tuple[str, str, str]) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return forms[2]
    match n % 10:
        case 1:
            return forms[0]
        case 2 | 3 | 4:
            return forms[1]
        case _:
            return forms[2]


def _amount_in_words(amount: Decimal, currency: str) -> str | None:
    """«Сто двадцать пять тысяч рублей 50 копеек» или None, если не вышло."""
    try:
        from num2words import num2words
    except ImportError:
        return None
    forms = _CURRENCY_FORMS.get(currency)
    if forms is None:
        return None
    major_forms, minor_forms = forms
    major = int(amount)
    minor = int((amount - major) * 100)
    try:
        words = num2words(major, lang="ru")
    except NotImplementedError:
        return None
    text = f"{words} {_plural(major, major_forms)} {minor:02d} {_plural(minor, minor_forms)}"
    return text[0].upper() + text[1:]


def _fmt_amount(amount: Decimal) -> str:
    """125 000,50 — разделитель тысяч пробелом, десятичная запятая."""
    return f"{amount:,.2f}".replace(",", " ").replace(".", ",")


# ---------------------------------------------------------------------------
# Сборка документа
# ---------------------------------------------------------------------------
def _styles() -> dict[str, ParagraphStyle]:
    """Типографика печатной формы: без цвета, кегли как в бухгалтерских бланках."""
    return {
        "title": ParagraphStyle("title", fontName=FONT_BOLD, fontSize=14, leading=18,
                                textColor=INK, alignment=TA_CENTER),
        "subtitle": ParagraphStyle("subtitle", fontName=FONT_BOLD, fontSize=12.5, leading=17,
                                   textColor=INK, alignment=TA_CENTER),
        "label": ParagraphStyle("label", fontName=FONT, fontSize=9.5, leading=13, textColor=INK),
        "value": ParagraphStyle("value", fontName=FONT_BOLD, fontSize=9.5, leading=13,
                                textColor=INK),
        "cell": ParagraphStyle("cell", fontName=FONT, fontSize=8.5, leading=11, textColor=INK),
        "cell_c": ParagraphStyle("cell_c", fontName=FONT, fontSize=8.5, leading=11,
                                 textColor=INK, alignment=TA_CENTER),
        "cell_r": ParagraphStyle("cell_r", fontName=FONT, fontSize=8.5, leading=11,
                                 textColor=INK, alignment=TA_RIGHT),
        "head_c": ParagraphStyle("head_c", fontName=FONT_BOLD, fontSize=8.5, leading=11,
                                 textColor=INK, alignment=TA_CENTER),
        "total_r": ParagraphStyle("total_r", fontName=FONT_BOLD, fontSize=9, leading=12,
                                  textColor=INK, alignment=TA_RIGHT),
        "words": ParagraphStyle("words", fontName=FONT, fontSize=9.5, leading=13, textColor=INK),
        "block": ParagraphStyle("block", fontName=FONT, fontSize=9, leading=13, textColor=INK),
        "sign": ParagraphStyle("sign", fontName=FONT_BOLD, fontSize=10, leading=14,
                               textColor=INK),
        "sign_hint": ParagraphStyle("sign_hint", fontName=FONT, fontSize=7, leading=9,
                                    textColor=MUTED, alignment=TA_CENTER),
    }


class _NumberedCanvas(pdfcanvas.Canvas):
    """Холст, знающий итоговое число страниц: «Лист 1 из 2».

    reportlab отдаёт номер страницы по ходу отрисовки и не знает, сколько их
    будет, поэтому страницы копим и штампуем номера при save().
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved = []

    def showPage(self) -> None:  # noqa: N802 — имя из reportlab
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._draw_page_number(total)
            super().showPage()
        super().save()

    def _draw_page_number(self, total: int) -> None:
        self.saveState()
        self.setFillColor(MUTED)
        self.setFont(FONT, 7)
        self.drawRightString(
            PAGE_W - MARGIN, 10 * mm, f"Лист {self._pageNumber} из {total}"
        )
        self.restoreState()


def _pairs_table(left: list[tuple[str, str]], right: list[tuple[str, str]],
                 st: dict) -> Table:
    """Шапка формы: «подпись — значение» в две колонки, без рамок.

    Ровно как в печатных бланках: слева «Организация:», справа «Оплата:».
    """
    rows = max(len(left), len(right))
    inner = PAGE_W - 2 * MARGIN
    data = []
    for i in range(rows):
        lab_l, val_l = left[i] if i < len(left) else ("", "")
        lab_r, val_r = right[i] if i < len(right) else ("", "")
        data.append([
            Paragraph(lab_l, st["label"]), Paragraph(val_l, st["value"]),
            Paragraph(lab_r, st["label"]), Paragraph(val_r, st["value"]),
        ])
    t = Table(data, colWidths=[30 * mm, inner / 2 - 30 * mm, 28 * mm, inner / 2 - 28 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


def _rule(width: float = 1.2) -> Table:
    """Горизонтальная линейка во всю ширину полосы набора."""
    t = Table([[""]], colWidths=[PAGE_W - 2 * MARGIN], rowHeights=[0.1])
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), width, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return t


def _sign_row(role: str, decoding: str, st: dict) -> Table:
    """Строка подписи: «Разрешил ______ / ______» с подписями под линиями.

    Одна таблица без вложенных: так текст извлекается из PDF и строка не
    разъезжается при переносе на новую страницу.
    """
    widths = [32 * mm, 52 * mm, 8 * mm, 52 * mm]
    t = Table([
        [Paragraph(role, st["sign"]), "", "", ""],
        ["", Paragraph("подпись", st["sign_hint"]), "",
         Paragraph(decoding or "расшифровка подписи", st["sign_hint"])],
    ], colWidths=widths)
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("LINEBELOW", (1, 0), (1, 0), 0.75, LINE),
        ("LINEBELOW", (3, 0), (3, 0), 0.75, LINE),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (0, 0), 2),
        ("TOPPADDING", (0, 1), (-1, 1), 1),
    ]))
    return t


def build_request_pdf(request: InvoiceRequest) -> bytes:
    """Формирует PDF заявки в виде печатной бухгалтерской формы."""
    _register_fonts()
    st = _styles()
    e = _esc

    created = request.created_at.strftime("%d.%m.%Y %H:%M") if request.created_at else "—"
    created_words = _date_words(request.created_at.date() if request.created_at else None)
    generated = datetime.now(ZoneInfo(settings.timezone)).strftime("%d.%m.%Y %H:%M")
    inner = PAGE_W - 2 * MARGIN

    def draw_frame(canvas, _doc) -> None:
        """Служебный колонтитул — единственное, что печатается вне потока."""
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#BBBBBB"))
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 7)
        canvas.drawString(
            MARGIN, 10 * mm,
            f"{settings.org_name} · заявка {request.request_id} · "
            f"сформирована автоматически {generated}",
        )
        # Номер листа ставит _NumberedCanvas: здесь ещё неизвестно, сколько их.
        canvas.restoreState()

    story: list = []

    # --- Заголовок формы ---------------------------------------------------------
    story.append(Paragraph("Заявка на расходование денежных средств", st["title"]))
    story.append(Paragraph(
        f"№ {e(request.request_id)} от {e(created_words)}", st["subtitle"]))
    story.append(Spacer(1, 5 * mm))
    story.append(_rule())
    story.append(Spacer(1, 4 * mm))

    # --- Шапка: кто, кому, сколько -----------------------------------------------
    if settings.org_details:
        story.append(Paragraph(e(settings.org_details), st["block"]))
        story.append(Spacer(1, 2 * mm))
    urgency = "Срочная — оплата в день подачи" if request.urgency.is_urgent else "Обычная"
    left = [
        ("Организация:", e(settings.org_name)),
        ("Сумма:", f"{_fmt_amount(request.amount)} {e(request.currency)}"),
        ("Заявитель:", e(request.sender_name)),
    ]
    right = [
        ("Дата подачи:", e(created)),
        ("Оплата:", {"invoice": "Счёт на оплату", "requisites": "По реквизитам",
                     "none": "Документы не приложены"}[request.payment_source]),
        ("Получатель:", e(request.counterparty)),
    ]
    story.append(_pairs_table(left, right, st))
    story.append(Spacer(1, 5 * mm))

    # --- Табличная часть ------------------------------------------------------------
    if request.payment_source == "invoice":
        basis = (
            f"Счёт на оплату, файл «{e(request.file_name)}»"
            if request.file_name else "Счёт на оплату (файл приложен)"
        )
    elif request.payment_source == "requisites":
        basis = "Оплата по реквизитам получателя"
    else:
        basis = "Основание не приложено — уточнить у заявителя"
    planned = request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    widths = [8 * mm, 40 * mm, inner - 8 * mm - 40 * mm - 24 * mm - 26 * mm - 21 * mm,
              24 * mm, 26 * mm, 21 * mm]
    table = Table([
        [Paragraph(h, st["head_c"]) for h in
         ("№", "Статья расходов", "Основание", "Срок оплаты", "Сумма", "Валюта")],
        [Paragraph("1", st["cell_c"]),
         Paragraph(e(request.article or "—"), st["cell"]),
         Paragraph(basis, st["cell"]),
         Paragraph(e(planned), st["cell_c"]),
         Paragraph(_fmt_amount(request.amount), st["cell_r"]),
         Paragraph(e(request.currency), st["cell_c"])],
        ["", "", "", Paragraph("Итого:", st["total_r"]),
         Paragraph(_fmt_amount(request.amount), st["total_r"]),
         Paragraph(e(request.currency), st["cell_c"])],
    ], colWidths=widths)
    table.setStyle(TableStyle([
        # Рамка только у шапки и строк — «Итого» вынесено за неё, как в бланках.
        ("GRID", (0, 0), (-1, 1), 0.75, LINE),
        ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 2), (-1, 2), 5),
    ]))
    story.append(table)
    story.append(Spacer(1, 4 * mm))

    words = _amount_in_words(request.amount, request.currency)
    if words:
        story.append(Paragraph(f"Сумма прописью: <b>{e(words)}</b>", st["words"]))
        story.append(Spacer(1, 3 * mm))
    story.append(Paragraph(f"Срочность: <b>{urgency}</b>", st["words"]))
    if request.work_deadline:
        story.append(Spacer(1, 1.5 * mm))
        story.append(Paragraph(
            f"Срок исполнения работ по договору: <b>{e(request.work_deadline)}</b>",
            st["words"],
        ))
    story.append(Spacer(1, 4 * mm))

    # --- Реквизиты получателя --------------------------------------------------------
    if not request.has_invoice and request.requisites:
        story.append(Paragraph("Реквизиты для оплаты:", st["label"]))
        story.append(Spacer(1, 1.5 * mm))
        req = Table(
            [[Paragraph(e(request.requisites).replace("\n", "<br/>"), st["block"])]],
            colWidths=[inner],
        )
        req.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.75, LINE),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(req)
        story.append(Spacer(1, 4 * mm))

    # --- Комментарий -------------------------------------------------------------------
    story.append(Paragraph("Комментарий:", st["label"]))
    story.append(Paragraph(e(request.comment or "—"), st["block"]))
    story.append(Spacer(1, 12 * mm))

    # --- Подписи ------------------------------------------------------------------------
    story.append(_sign_row("Заявитель", e(request.sender_name), st))
    story.append(Spacer(1, 9 * mm))
    story.append(_sign_row("Разрешил", "", st))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"Заявка на расходование денежных средств {request.request_id}",
        author=settings.org_name,
        subject="Заявка на расходование денежных средств",
        creator="invoice-bot",
    )
    doc.build(
        story,
        onFirstPage=draw_frame,
        onLaterPages=draw_frame,
        canvasmaker=_NumberedCanvas,
    )
    pdf = buf.getvalue()
    log.info("PDF заявки %s сформирован (%d байт)", request.request_id, len(pdf))
    return pdf



def _esc(text: str) -> str:
    """Экранирование для мини-разметки Paragraph (XML-подобная)."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
