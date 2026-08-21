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


# Счёт от физлица/ИП: банк получателя стоит ВЫШЕ строки получателя — именно
# на такой вёрстке в поле «Контрагент» подставлялся банк вместо ФИО.
INVOICE_FIZ = """
Счет на оплату № 7 от 01.08.2026

Банк получателя: ПАО СБЕРБАНК г. Москва
БИК 044525225
Р/с 40817810400000012345
К/с 30101810400000000225

Получатель: Петрова Анна Сергеевна
ИНН 500100732259

Всего к оплате: 30 000,00
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

    def test_individual_is_not_the_bank(self):
        """Получатель — физлицо: банк из реквизитов подставлять нельзя.

        Раньше ФИО не распознавалось вообще, срабатывал запасной путь и брал
        первую орг-форму по всему тексту — а это «Банк получателя: ПАО
        СБЕРБАНК», он стоит выше получателя в любом типовом счёте.
        """
        assert ex.find_counterparty(INVOICE_FIZ) == "Петрова Анна Сергеевна"

    def test_initials_keep_trailing_dot(self):
        """«Сидоров П. И» без точки выглядит обрубком."""
        text = "Банк получателя: АО «АЛЬФА-БАНК»\nБИК 044525593\nПоставщик: Сидоров П. И."
        assert ex.find_counterparty(text) == "Сидоров П. И."

    def test_double_surname(self):
        assert ex.find_counterparty("Получатель: Петрова-Водкина Анна Сергеевна") == (
            "Петрова-Водкина Анна Сергеевна"
        )

    def test_ip_with_full_name(self):
        text = (
            "Банк получателя: АО «ТИНЬКОФФ БАНК»\n"
            "Поставщик: ИП Иванов Иван Иванович, ИНН 771234567890"
        )
        assert ex.find_counterparty(text) == "ИП Иванов Иван Иванович"

    def test_bank_alone_gives_nothing(self):
        """Есть только банковский блок — честнее пусто, чем название банка."""
        assert ex.find_counterparty("Банк получателя: ПАО СБЕРБАНК\nБИК 044525225") is None

    def test_bank_can_be_the_counterparty_by_label(self):
        """Фильтр банка — только в запасном пути: банку тоже платят за услуги."""
        text = "Поставщик (Исполнитель): АО «Альфа-Банк», ИНН 7728168971"
        assert ex.find_counterparty(text) == "АО «Альфа-Банк»"

    def test_wide_list_of_legal_forms(self):
        """Форм в России куда больше семи — берём их с привязкой к началу."""
        for line, want in (
            ("Поставщик: НАО «Красная поляна»", "НАО «Красная поляна»"),
            ("Поставщик: ФГБОУ ВО «МГУ»", "ФГБОУ ВО «МГУ»"),
            ("Получатель: ГБУ «Автомобильные дороги»", "ГБУ «Автомобильные дороги»"),
            ("Получатель: ТСЖ «Первомайское»", "ТСЖ «Первомайское»"),
            ("Поставщик: КФХ «Заря»", "КФХ «Заря»"),
            ("Получатель: УК «Домсервис»", "УК «Домсервис»"),
            ("Получатель: СНТ «Ромашка»", "СНТ «Ромашка»"),
        ):
            assert ex.find_counterparty(line) == want, line

    def test_legal_form_spelled_out(self):
        for line, want in (
            (
                "Поставщик: Индивидуальный предприниматель Иванов И.И.",
                "Индивидуальный предприниматель Иванов И.И.",
            ),
            ("Поставщик: Акционерное общество «Тензор»", "Акционерное общество «Тензор»"),
            (
                "Поставщик: Общество с ограниченной ответственностью «Век»",
                "Общество с ограниченной ответственностью «Век»",
            ),
        ):
            assert ex.find_counterparty(line) == want, line

    def test_form_inside_a_word_is_not_a_form(self):
        """Без \\b «ип» из «Типовой» считалось формой, и в поле летел обрывок."""
        text = "Типовой договор поставки. Получатель: Сидоров Пётр Ильич"
        assert ex.find_counterparty(text) == "Сидоров Пётр Ильич"

    def test_stray_words_are_not_the_counterparty(self):
        """Широкий список без привязки ловил «по договору» и «ГК РФ»."""
        assert ex.find_counterparty("Получатель: Петрова А.С. по договору №5") == "Петрова А.С."
        assert (
            ex.find_counterparty("Поставщик: Сидоров Пётр Ильич, ГК РФ ст. 421")
            == "Сидоров Пётр Ильич"
        )


class TestCounterpartyOnRealLayouts:
    """Раскладки, снятые с настоящих счетов (данные обезличены).

    Бот читает PDF через pypdf, а он рассыпает колонки: метка оказывается
    то в одной строке со значением, то над ним, то под ним. Эти случаи
    ловятся только на живых документах, синтетика их не воспроизводила.
    """

    def test_label_without_colon_and_wide_gap(self):
        """«Исполнитель<пробелы>Адвокат ФИО» — двоеточия в счёте нет.

        Прежний разделитель допускал любые 30 символов и съедал начало
        имени, оставляя огрызок «ИЧ, 196084, РОССИЯ».
        """
        text = (
            "Счет на оплату №42 от 10 августа 2026 г.\n"
            "Исполнитель Адвокат ПЕТРОВ АНДРЕЙ ИГОРЕВИЧ, 196084, РОССИЯ, г. Санкт - Петербург\n"
            "Заказчик ИП Хайрулин Владислав Ренатович\n"
        )
        assert ex.find_counterparty(text) == "Адвокат ПЕТРОВ АНДРЕЙ ИГОРЕВИЧ"

    def test_payer_never_becomes_the_counterparty(self):
        """Получателя не распознали — лучше пусто, чем заказчик.

        Это была самая скверная ошибка: подставлялась НАША сторона, и по
        виду («ИП» с ФИО) она ничем не отличалась от контрагента.
        """
        text = (
            "ФИЛИАЛ «САНКТ-ПЕТЕРБУРГСКИЙ» АО «АЛЬФА-БАНК» БИК 044030786\n"
            "Банк получателя Сч.№ 30101810600000000786\n"
            "Заказчик ИП Хайрулин Владислав Ренатович\n"
        )
        assert ex.find_counterparty(text) is None

    def test_payment_order_label_below_the_name(self):
        """Платёжное поручение 0401060: метка стоит НИЖЕ значения.

        Метки получателя тут нет вовсе (после неё идёт назначение платежа),
        поэтому работает запасной путь — и он обязан пропустить и банки,
        и блок плательщика, который в этой форме идёт первым.
        """
        text = (
            "ПЛАТЕЖНОЕ ПОРУЧЕНИЕ № 888 20.08.2026\n"
            "Плательщик\n"
            "Индивидуальный предприниматель Хайрулин Владислав\n"
            "Ренатович\n"
            "ООО «Банк Точка» г. Москва\n"
            "БИК 044525104\n"
            "Банк плательщика\n"
            "Банк получателя\n"
            "СЕВЕРО-ЗАПАДНЫЙ БАНК ПАО СБЕРБАНК г. Санкт-Петербург\n"
            "ООО «ОБЛАКОПРОДАЖИ»\n"
            "Получатель\n"
            "Счет № 32179423 от 20.08.2026 Неисключительная лицензия\n"
        )
        assert ex.find_counterparty(text) == "ООО «ОБЛАКОПРОДАЖИ»"

    def test_label_split_across_lines(self):
        """«Поставщик» и «(Исполнитель):» оказались на разных строках."""
        text = (
            "АО «ТБанк» г. Москва БИК 044525974\n"
            "Банк получателя\n"
            "Счет на оплату № 5090 от 31 июля 2026 г.\n"
            "Поставщик\n"
            "(Исполнитель):\n"
            "ООО Облакопродажи, ИНН 5042129630, КПП 504201001, 141308\n"
            "Покупатель\n"
            "(Заказчик):\n"
            "ИП Хайрулин Владислав Ренатович, ИНН 784809946092\n"
        )
        assert ex.find_counterparty(text) == "ООО Облакопродажи"

    def test_caps_name_after_label(self):
        """ФИО в счетах сплошь капслоком — вариант «Иванов» его не видел."""
        assert ex.find_counterparty("Получатель: ИВАНОВА АННА СЕРГЕЕВНА") == (
            "ИВАНОВА АННА СЕРГЕЕВНА"
        )

    def test_bank_name_is_not_the_counterparty(self):
        """Название банка содержит «банк» — в запасном пути это отсев."""
        text = "ООО «Банк Точка» г. Москва\nСч. № 30101810745374525104\n"
        assert ex.find_counterparty(text) is None


class TestCounterpartyIsNeverOurOwnSide:
    """Мы сами не можем быть контрагентом: платим не себе.

    В форме СберБизнеса PDF-слой рассыпает колонки — метка «Поставщик:»
    оказывается над строкой ПОКУПАТЕЛЯ, и идти за меткой нельзя. Отличить
    свою сторону от чужой по виду невозможно, обе выглядят как «ИП с ФИО»,
    поэтому опираемся на ИНН из ORG_INN.
    """

    # Раскладка снята с реального счёта: метка врёт, имя получателя лежит
    # выше, рядом со своим ИНН, и разорвано переносом строки.
    SCRAMBLED = (
        "Создано в СберБизнес\n"
        "ПАО Сбербанк, генеральная лицензия № 1481\n"
        " Банк получателя\n"
        "БИК\n"
        "Счёт на оплату № 25 от 21.08.2026\n"
        "ИНН 500100732259\n"
        "ПАО Сбербанк 044525225\n"
        "Получатель\n"
        "30101 810 4 0000 0000225\n"
        "24 августа 2026\n"
        "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ СИДОРОВА\n"
        "ТАТЬЯНА ВАЛЕРЬЕВНА\n"
        "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ СИДОРОВА ТАТЬЯНА ВАЛЕРЬЕВНА, ИНН\n"
        "500100732259\n"
        "Поставщик:\n"
        "Индивидуальный предприниматель Петров Пётр Петрович, ИНН 771234567890, 183008\n"
        "Покупатель:\n"
    )

    def test_label_pointing_at_us_is_skipped(self, monkeypatch):
        """Метка ведёт на нас — значит колонки перепутаны, ищем дальше."""
        monkeypatch.setattr(ex.settings, "org_inn", "771234567890")
        assert ex.find_counterparty(self.SCRAMBLED) == (
            "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ СИДОРОВА ТАТЬЯНА ВАЛЕРЬЕВНА"
        )

    def test_without_org_inn_nothing_changes(self, monkeypatch):
        """ORG_INN пуст — проверка выключена, поведение прежнее."""
        monkeypatch.setattr(ex.settings, "org_inn", "")
        assert ex.find_counterparty(self.SCRAMBLED) == (
            "Индивидуальный предприниматель Петров Пётр Петрович"
        )


class TestInnLength:
    def test_twelve_digit_inn_is_not_truncated(self):
        """У предпринимателя ИНН из 12 цифр.

        В альтернативе «(\\d{10}|\\d{12})» движок брал первые десять,
        контрольные числа не сходились — и ИНН у любого ИП не определялся.
        """
        assert ex.find_inn("ИНН 500100732259") == "500100732259"

    def test_ten_digit_inn_still_works(self):
        assert ex.find_inn("ИНН 7707083893") == "7707083893"


class TestGluedTail:
    def test_inn_glued_to_the_name_is_cut(self):
        """PDF-слой склеивает хвост с именем: «…РЕНАТОВИЧИНН 7712…»."""
        text = "Поставщик: ИП ПЕТРОВ ПЁТР ПЕТРОВИЧИНН 771234567890"
        assert ex.find_counterparty(text) == "ИП ПЕТРОВ ПЁТР ПЕТРОВИЧ"

    def test_name_ending_in_inn_survives(self):
        """«ООО ФИНН» не должно превратиться в «ООО Ф» — цифр после нет."""
        assert ex.find_counterparty("Поставщик: ООО ФИНН") == "ООО ФИНН"


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
