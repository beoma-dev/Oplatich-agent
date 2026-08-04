"""Разбор счёта на поля формы: что распознаётся, а что честно молчит."""
from __future__ import annotations

from services import invoice_extract as ex

# Текст типового российского счёта — так он выглядит после PDF-слоя.
INVOICE = """
ООО «Ромашка»
ИНН 7707083893  КПП 773601001
Банк получателя: ПАО СБЕРБАНК г. Москва
БИК 044525225
Р/с 40702810400000012345
К/с 30101810400000000225

Счет на оплату № 118 от 04 августа 2026 г.

Поставщик (Исполнитель): ООО «Ромашка», ИНН 7707083893
Покупатель (Заказчик): ООО «Заказчик», ИНН 5024002119

№  Товары (работы, услуги)   Кол-во  Цена      Сумма
1  Аренда офиса, август      1       145322.68 145322.68

Итого:            145322.68
В том числе НДС:  29064.53
Всего к оплате:   174387.21

Всего наименований 1, на сумму 174 387,21 руб.
Сто семьдесят четыре тысячи триста восемьдесят семь рублей 21 копейка
"""

# Тот же счёт после OCR скана: съехавшие пробелы, неразрывные пробелы.
OCR_INVOICE = """
Cчет на оплату № 118 от 04.08.2026

Поставщик  (Исполнитель):  ООО «Ромашка»
ИНН  7707083893    КПП  773601001
БИК  044525225
Р/с  40702 810 4000 0001 2345
К/с  30101 810 4000 0000 0225

Всего к оплате:  174 387,21
"""


class TestAmount:
    def test_takes_total_not_line_item(self):
        """«Всего к оплате» больше строки товара — берём итог."""
        assert ex.find_amount(INVOICE) == ex.Decimal("174387.21")

    def test_spaces_and_comma(self):
        assert ex.find_amount(OCR_INVOICE) == ex.Decimal("174387.21")

    def test_no_total_means_none(self):
        assert ex.find_amount("Просто письмо без сумм") is None

    def test_garbage_is_not_a_sum(self):
        assert ex.find_amount("Итого: —") is None

    def test_absurd_amount_rejected(self):
        assert ex.find_amount("Итого: 9999999999999") is None


class TestRequisites:
    def test_inn_must_pass_checksum(self):
        assert ex.find_inn(INVOICE) == "7707083893"

    def test_broken_inn_is_ignored(self):
        """OCR склеил цифры — лучше пусто, чем неверный ИНН в платёжке."""
        assert ex.find_inn("ИНН 1234567890") is None

    def test_bank_details(self):
        fields = ex.extract_fields(INVOICE)
        assert fields["kpp"] == "773601001"
        assert fields["bik"] == "044525225"
        assert fields["account"] == "40702810400000012345"
        assert fields["corr_account"] == "30101810400000000225"

    def test_accounts_survive_ocr_spacing(self):
        fields = ex.extract_fields(OCR_INVOICE)
        assert fields["account"] == "40702810400000012345"
        assert fields["corr_account"] == "30101810400000000225"


class TestCounterparty:
    def test_supplier_not_buyer(self):
        """Платим поставщику: покупателя подставлять нельзя."""
        assert ex.find_counterparty(INVOICE) == "ООО «Ромашка»"

    def test_fallback_to_first_org(self):
        assert ex.find_counterparty("АО «Тензор» выставил счёт") == "АО «Тензор»"

    def test_trailing_inn_trimmed(self):
        assert ex.find_counterparty("Поставщик: ООО «Век» ИНН 7707083893") == "ООО «Век»"

    def test_nothing_to_find(self):
        assert ex.find_counterparty("счёт без организации") is None


class TestInvoiceNumber:
    def test_number_and_date(self):
        assert ex.find_invoice_number(INVOICE) == ("118", "04 августа 2026")

    def test_dotted_date(self):
        assert ex.find_invoice_number(OCR_INVOICE) == ("118", "04.08.2026")

    def test_absent(self):
        assert ex.find_invoice_number("нет тут счёта") == (None, None)


class TestExtractFields:
    def test_full_invoice(self):
        fields = ex.extract_fields(INVOICE)
        assert fields["amount"] == "174387.21"
        assert fields["counterparty"] == "ООО «Ромашка»"
        assert fields["inn"] == "7707083893"
        assert fields["invoice_number"] == "118"

    def test_requisites_block_is_ready_to_paste(self):
        block = ex.extract_fields(INVOICE)["requisites"]
        assert "Получатель: ООО «Ромашка»" in block
        assert "ИНН 7707083893, КПП 773601001" in block
        assert "Р/с 40702810400000012345" in block
        assert "БИК 044525225" in block

    def test_empty_text_gives_nothing(self):
        assert ex.extract_fields("") == {}
        assert ex.extract_fields("   ") == {}

    def test_unrecognized_fields_are_absent_not_empty(self):
        """Пустое поле честнее подставленного мусора — ключа просто нет."""
        fields = ex.extract_fields("Счет на оплату № 5 от 01.01.2026")
        assert "inn" not in fields
        assert "amount" not in fields
        assert fields["invoice_number"] == "5"

    def test_failure_never_raises(self, monkeypatch):
        monkeypatch.setattr(ex, "find_amount", lambda t: 1 / 0)
        assert ex.extract_fields(INVOICE) == {}
