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
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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
NAVY = colors.HexColor("#1E2A5A")       # шапка
ACCENT = colors.HexColor("#2481CC")     # акцентные элементы
INK = colors.HexColor("#1C1C1E")        # основной текст
MUTED = colors.HexColor("#6B7280")      # подписи
LINE = colors.HexColor("#D7DEEA")       # линии таблиц
ROW_BG = colors.HexColor("#F4F7FB")     # фон колонок-подписей
URGENT_RED = colors.HexColor("#C62828")
NORMAL_GREEN = colors.HexColor("#2E7D32")

PAGE_W, PAGE_H = A4
MARGIN = 18 * mm
HEADER_H = 26 * mm


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
    base = dict(fontName=FONT, textColor=INK)
    return {
        "label": ParagraphStyle("label", fontSize=8.5, leading=11, textColor=MUTED, **{k: v for k, v in base.items() if k != "textColor"}),
        "value": ParagraphStyle("value", fontSize=10.5, leading=14, **base),
        "value_bold": ParagraphStyle("value_bold", fontName=FONT_BOLD, fontSize=10.5, leading=14, textColor=INK),
        "amount": ParagraphStyle("amount", fontName=FONT_BOLD, fontSize=17, leading=21, textColor=NAVY),
        "words": ParagraphStyle("words", fontName=FONT, fontSize=9, leading=12, textColor=MUTED),
        "section": ParagraphStyle("section", fontName=FONT_BOLD, fontSize=9.5, leading=12,
                                  textColor=NAVY, spaceBefore=10, spaceAfter=4),
        "mono": ParagraphStyle("mono", fontName=FONT, fontSize=10, leading=15, textColor=INK),
        "badge_r": ParagraphStyle("badge_r", fontName=FONT_BOLD, fontSize=10, leading=13,
                                  textColor=colors.white, alignment=TA_RIGHT),
    }


def _kv_table(rows: list[tuple[str, Paragraph]], st: dict) -> Table:
    """Таблица «подпись — значение» с фирменным оформлением."""
    data = [[Paragraph(label, st["label"]), value] for label, value in rows]
    t = Table(data, colWidths=[42 * mm, PAGE_W - 2 * MARGIN - 42 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), ROW_BG),
        ("GRID", (0, 0), (-1, -1), 0.5, LINE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def build_request_pdf(request: InvoiceRequest) -> bytes:
    """Формирует PDF заявки, возвращает содержимое файла."""
    _register_fonts()
    st = _styles()
    e = _esc

    created = request.created_at.strftime("%d.%m.%Y %H:%M") if request.created_at else "—"
    urgent = request.urgency.is_urgent

    def draw_frame(canvas, _doc) -> None:
        """Шапка и колонтитул на странице."""
        canvas.saveState()
        # --- Шапка ---
        canvas.setFillColor(NAVY)
        canvas.rect(0, PAGE_H - HEADER_H, PAGE_W, HEADER_H, stroke=0, fill=1)
        canvas.setFillColor(ACCENT)
        canvas.rect(0, PAGE_H - HEADER_H - 1.2 * mm, PAGE_W, 1.2 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white)
        canvas.setFont(FONT_BOLD, 17)
        canvas.drawString(MARGIN, PAGE_H - 12.5 * mm, settings.org_name)
        canvas.setFont(FONT, 8.5)
        canvas.setFillColor(colors.HexColor("#B9C4E0"))
        canvas.drawString(MARGIN, PAGE_H - 17.5 * mm, "Финансовый документ · сформирован автоматически")
        canvas.setFillColor(colors.white)
        canvas.setFont(FONT_BOLD, 11)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 12.5 * mm, "ЗАЯВКА НА ОПЛАТУ")
        canvas.setFont(FONT, 9)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 17.5 * mm, f"№ {request.request_id}")
        # --- Колонтитул ---
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(0.6)
        canvas.line(MARGIN, 14 * mm, PAGE_W - MARGIN, 14 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont(FONT, 7.5)
        canvas.drawString(
            MARGIN, 10 * mm,
            f"{settings.org_name} · invoice-bot · заявка {request.request_id} от {created}",
        )
        canvas.drawRightString(PAGE_W - MARGIN, 10 * mm, f"стр. {canvas.getPageNumber()}")
        canvas.restoreState()

    story: list = []

    # --- Статус-строка: дата и срочность ------------------------------------
    badge_color = URGENT_RED if urgent else NORMAL_GREEN
    badge_text = "СРОЧНЫЙ ПЛАТЁЖ" if urgent else "обычный платёж"
    head = Table(
        [[
            Paragraph(f"Дата подачи: <b>{created}</b>", st["value"]),
            Paragraph(badge_text.upper(), st["badge_r"]),
        ]],
        colWidths=[None, 58 * mm],
    )
    head.setStyle(TableStyle([
        ("BACKGROUND", (1, 0), (1, 0), badge_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 8),
        ("RIGHTPADDING", (1, 0), (1, 0), 8),
        ("TOPPADDING", (1, 0), (1, 0), 5),
        ("BOTTOMPADDING", (1, 0), (1, 0), 5),
    ]))
    story.append(head)
    story.append(Spacer(1, 6 * mm))

    # --- 1. Платёж ------------------------------------------------------------
    story.append(Paragraph("1 · ПЛАТЁЖ", st["section"]))
    amount_line = f"{_fmt_amount(request.amount)}&nbsp;{e(request.currency)}"
    rows = [
        ("Сумма", Paragraph(amount_line, st["amount"])),
    ]
    words = _amount_in_words(request.amount, request.currency)
    if words:
        rows.append(("Сумма прописью", Paragraph(e(words), st["words"])))
    planned = (
        request.planned_date.strftime("%d.%m.%Y") if request.planned_date else "—"
    )
    rows += [
        ("Контрагент", Paragraph(e(request.counterparty), st["value_bold"])),
        ("Статья расходов", Paragraph(e(request.article or "—"), st["value"])),
        ("Плановая дата оплаты", Paragraph(e(planned), st["value_bold"])),
        ("Комментарий", Paragraph(e(request.comment or "—"), st["value"])),
    ]
    story.append(_kv_table(rows, st))

    # --- 2. Основание оплаты ---------------------------------------------------
    story.append(Paragraph("2 · ОСНОВАНИЕ ОПЛАТЫ", st["section"]))
    if request.has_invoice:
        rows = [
            ("Способ", Paragraph("Счёт на оплату (файл прилагается)", st["value"])),
            ("Файл счёта", Paragraph(e(request.file_name or "—"), st["value_bold"])),
        ]
    else:
        req_html = e(request.requisites or "—").replace("\n", "<br/>")
        rows = [
            ("Способ", Paragraph("Оплата по реквизитам (счёт отсутствует)", st["value"])),
            ("Реквизиты", Paragraph(req_html, st["mono"])),
        ]
    story.append(_kv_table(rows, st))

    # --- 3. Отправитель ---------------------------------------------------------
    story.append(Paragraph("3 · ОТПРАВИТЕЛЬ ЗАЯВКИ", st["section"]))
    story.append(_kv_table([
        ("Сотрудник", Paragraph(e(request.sender_name), st["value_bold"])),
        ("Telegram", Paragraph(
            f"{e(request.sender_username)} · id {request.telegram_id}", st["value"])),
    ], st))

    # --- 4. Служебное -------------------------------------------------------------
    story.append(Paragraph("4 · СЛУЖЕБНАЯ ИНФОРМАЦИЯ", st["section"]))
    story.append(_kv_table([
        ("Номер заявки", Paragraph(e(request.request_id), st["value_bold"])),
        ("Статус", Paragraph(e(request.status), st["value"])),
        ("Канал подачи", Paragraph("Telegram · invoice-bot", st["value"])),
    ], st))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=HEADER_H + 8 * mm,
        bottomMargin=20 * mm,
        title=f"Заявка на оплату {request.request_id}",
        author=settings.org_name,
        subject="Заявка на оплату",
        creator="invoice-bot",
    )
    doc.build(story, onFirstPage=draw_frame, onLaterPages=draw_frame)
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
