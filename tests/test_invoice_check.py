"""Автопроверка «похож ли файл на счёт»: скоринг, извлечение, fail-open."""
from __future__ import annotations

from decimal import Decimal

from config import settings
from services.invoice_check import (
    THRESHOLD,
    _amount_variants,
    _inn_valid,
    check_invoice_file,
    score_text,
)
from tests.conftest import make_request

# Синтетический текст с маркерами реального счёта (структура — как у образца).
INVOICE_TEXT = """
АО "Банк" г. Москва БИК 044525974
Сч. № 30101810145250000974
ИНН 7707083893 КПП 770701001 Сч. № 40702810610001012250
Счет на оплату № 5090 от 31 июля 2026 г.
Поставщик (Исполнитель): ООО Пример
Покупатель (Заказчик): ИП Иванов
Итого: 174 387,21
В том числе НДС 22%: 31 446,87
Всего к оплате: 174 387,21
Сто семьдесят четыре тысячи рублей 21 копейка
Оплатить не позднее 15.08.2026
"""

CAT_TEXT = "Смотрите какой милый котик! Пушистый и рыжий. Фото на телефон, вчера."


class TestScoring:
    def test_real_invoice_scores_high(self):
        score, found = score_text(INVOICE_TEXT)
        assert score >= THRESHOLD * 2
        assert "заголовок счёта" in found and "валидный ИНН" in found

    def test_amount_match_bonus(self):
        base, _ = score_text(INVOICE_TEXT)
        with_amount, found = score_text(INVOICE_TEXT, Decimal("174387.21"))
        assert with_amount == base + 3
        assert "сумма совпадает с формой" in found

    def test_cat_scores_zero(self):
        score, found = score_text(CAT_TEXT)
        assert score < THRESHOLD
        assert found == []

    def test_invalid_inn_gives_no_bonus(self):
        text = "Счет на оплату № 1. ИНН 7707083892. Итого: 100"
        _, found = score_text(text)
        assert "валидный ИНН" not in found


class TestHelpers:
    def test_inn_valid(self):
        assert _inn_valid("7707083893")
        assert _inn_valid("500100732259")
        assert not _inn_valid("7707083892")
        assert not _inn_valid("123")

    def test_amount_variants(self):
        v = _amount_variants(Decimal("174387.21"))
        assert {"174387.21", "174387,21", "174 387,21"} <= v
        v2 = _amount_variants(Decimal("5000.00"))
        assert {"5000", "5 000"} <= v2


class TestCheckFile:
    def _pdf_with_text(self, text: str) -> bytes:
        """Настоящий PDF с текстовым слоем (кириллица через DejaVu)."""
        import io

        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        from services.pdf_report import FONT, _register_fonts

        _register_fonts()
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.setFont(FONT, 10)
        y = 800
        for line in text.strip().split("\n"):
            c.drawString(40, y, line)
            y -= 14
        c.save()
        return buf.getvalue()

    def test_real_like_pdf_passes(self, tmp_paths):
        pdf = self._pdf_with_text(INVOICE_TEXT)
        assert check_invoice_file(pdf, "счёт.pdf", Decimal("174387.21")) is None

    def test_cat_pdf_warns(self, tmp_paths):
        pdf = self._pdf_with_text(CAT_TEXT * 5)  # длинный текст без маркеров
        warning = check_invoice_file(pdf, "kotik.pdf")
        assert warning is not None and "не похоже на счёт" in warning

    def test_broken_image_fails_open(self, tmp_paths):
        # Не изображение и не PDF: OCR недоступен/падает → fail-open (None).
        assert check_invoice_file(b"\x00\x01\x02notanimage", "cat.jpg") is None

    def test_disabled_toggle(self, tmp_paths, monkeypatch):
        monkeypatch.setattr(settings, "invoice_check_enabled", False)
        pdf = self._pdf_with_text(CAT_TEXT)
        assert check_invoice_file(pdf, "kotik.pdf") is None

    def test_generated_request_pdf_is_not_invoice(self, tmp_paths):
        """Наш PDF заявки — не счёт поставщика, но и не котик: важно, что
        проверка не падает на нём (скоринг может быть любым)."""
        from services.pdf_report import build_request_pdf

        check_invoice_file(build_request_pdf(make_request()), "req.pdf")
