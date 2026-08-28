"""Сводка для админа: считает то, что обещает, и не смешивает валюты."""
from __future__ import annotations

import time
from datetime import date

from services import analytics

TODAY = date(2026, 8, 27)


def _row(**over) -> dict[str, str]:
    row = {
        "Дата внесения в реестр": "2026-08-25 10:00",
        "Плановая дата оплаты": "28.08.2026",
        "Сотрудник по заявке": "@petya (Пётр)",
        "Контрагент": "ООО «Ромашка»",
        "Сумма": "1000.00",
        "Статья": "Аренда",
        "Статус оплаты": "Новая",
        "Ссылка на счет": "/files/inv.pdf",
        "Валюта": "RUB",
        "Срочность": "Обычная",
        "Реквизиты": "",
        "ID заявки": "INV-20260825-100000-0001",
        "Telegram ID": "42",
        "Срок исполнения работ по договору": "текущий месяц",
        "Закрывающие документы": "",
    }
    row.update(over)
    return row


def _build(rows, paid_at=None, users=None, allowed=None, days=30):
    return analytics.build(
        rows, paid_at or {}, users or [], allowed or set(), TODAY, days
    )


class TestPeople:
    def test_counts_authors_and_ranks_them(self):
        rows = [
            _row(), _row(**{"ID заявки": "INV-2"}),
            _row(**{"Сотрудник по заявке": "@masha (Маша)", "Telegram ID": "43",
                    "ID заявки": "INV-3"}),
        ]
        out = _build(rows)["people"]
        assert out["authors_period"] == 2
        assert out["authors_ever"] == 2
        assert [t["name"] for t in out["top"]] == ["@petya (Пётр)", "@masha (Маша)"]
        assert out["top"][0]["count"] == 2

    def test_access_granted_but_never_used_is_visible(self):
        """Ровно тот вопрос, ради которого сводку и просили: кто молчит."""
        out = _build(
            [_row()],
            users=[(42, "@petya"), (77, "@lena"), (99, "@vasya")],
            allowed={42, 77},
        )["people"]
        # @vasya без доступа — он не «молчит», ему просто не давали.
        assert out["idle"] == ["@lena"]

    def test_period_cuts_off_old_requests(self):
        rows = [_row(), _row(**{"Дата внесения в реестр": "2026-01-05 10:00",
                                "ID заявки": "INV-old"})]
        out = _build(rows, days=7)
        assert out["flow"]["period_count"] == 1
        assert out["flow"]["total_count"] == 2


class TestFlow:
    def test_currencies_are_never_summed_together(self):
        """100 ₽ и 100 $ — это не 200. Проверяем, потому что соблазн велик."""
        rows = [_row(), _row(**{"Валюта": "USD", "Сумма": "50.00", "ID заявки": "INV-2"})]
        assert _build(rows)["flow"]["period_sums"] == {"RUB": "1000.00", "USD": "50.00"}

    def test_median_days_from_submission_to_payment(self):
        paid = date(2026, 8, 27)
        stamp = time.mktime(paid.timetuple()) + 12 * 3600
        rows = [_row(**{"Статус оплаты": "Оплачена"})]
        out = _build(rows, paid_at={"INV-20260825-100000-0001": stamp})["flow"]
        assert out["median_days"] == 2
        assert out["paid_measured"] == 1

    def test_median_is_none_without_audit(self):
        """Аудит недоступен — метрика пропадает, а не превращается в ноль."""
        out = _build([_row(**{"Статус оплаты": "Оплачена"})])["flow"]
        assert out["median_days"] is None and out["paid_measured"] == 0

    def test_waiting_longer_than_norm(self):
        rows = [
            _row(**{"Дата внесения в реестр": "2026-08-26 10:00", "ID заявки": "INV-fresh"}),
            _row(**{"Дата внесения в реестр": "2026-08-20 10:00", "ID заявки": "INV-slow"}),
            # Оплаченная не ждёт, сколько бы ей ни было лет.
            _row(**{"Дата внесения в реестр": "2026-01-01 10:00", "ID заявки": "INV-paid",
                    "Статус оплаты": "Оплачена"}),
        ]
        assert _build(rows)["flow"]["waiting_long"] == 1

    def test_overdue_uses_the_same_rule_as_reminders(self):
        rows = [
            _row(**{"Плановая дата оплаты": "20.08.2026", "ID заявки": "INV-late"}),
            _row(**{"Плановая дата оплаты": "20.08.2026", "ID заявки": "INV-late-paid",
                    "Статус оплаты": "Оплачена"}),
        ]
        out = _build(rows)["flow"]
        assert out["overdue_now"] == 1
        assert out["overdue_sums"] == {"RUB": "1000.00"}

    def test_articles_are_ranked(self):
        rows = [_row(), _row(**{"Статья": "Реклама", "ID заявки": "INV-2"}),
                _row(**{"Статья": "Реклама", "ID заявки": "INV-3"})]
        out = _build(rows)["flow"]["articles"]
        assert out[0]["name"] == "Реклама" and out[0]["count"] == 2


class TestDocs:
    def test_requests_without_any_documents(self):
        rows = [
            _row(),
            _row(**{"Ссылка на счет": "", "Реквизиты": "ИНН 7707083893", "ID заявки": "INV-2"}),
            _row(**{"Ссылка на счет": "", "Реквизиты": "", "ID заявки": "INV-3"}),
        ]
        assert _build(rows)["docs"]["no_docs"] == 1

    def test_paid_without_closing_documents(self):
        """Боль бухгалтерии перед закрытием периода."""
        rows = [
            _row(**{"Статус оплаты": "Оплачена", "ID заявки": "INV-1"}),
            _row(**{"Статус оплаты": "Оплачена", "ID заявки": "INV-2",
                    "Закрывающие документы": "/files/akt.pdf"}),
            _row(**{"ID заявки": "INV-3"}),
        ]
        out = _build(rows)["docs"]
        assert out["paid_total"] == 2 and out["paid_without_closing"] == 1


class TestEmpty:
    def test_no_requests_at_all_does_not_break(self):
        """На боевом сейчас шесть заявок, а бывает и ноль."""
        out = _build([])
        assert out["flow"]["period_count"] == 0
        assert out["flow"]["median_days"] is None
        assert out["people"]["top"] == []


class TestPaidTimesFromAudit:
    """Разбор строки аудита — единственный источник времени оплаты."""

    def test_reads_the_first_payment_mark(self, tmp_paths):
        from services import audit

        audit.log_event_sync(audit.STATUS_CHANGED, 1, "fin", "INV-7 → Оплачена")
        audit.log_event_sync(audit.STATUS_CHANGED, 1, "fin",
                             "INV-8 → Отклонена · причина: не тот счёт")
        out = audit.paid_times_sync()
        assert set(out) == {"INV-7"}

    def test_reason_suffix_does_not_confuse_the_parser(self, tmp_paths):
        from services import audit

        audit.log_event_sync(audit.STATUS_CHANGED, 1, "fin",
                             "INV-9 → Оплачена · причина: догнали")
        assert "INV-9" in audit.paid_times_sync()

    def test_status_change_format_is_the_one_we_parse(self):
        """Формат задаётся status_change; разъедется — метрика молча обнулится."""
        from pathlib import Path

        src = Path("services/status_change.py").read_text(encoding="utf-8")
        assert 'f"{request_id} → {status_text}"' in src
