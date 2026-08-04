"""Динамические настройки: объединение с .env, защита env-записей, дубли."""
from __future__ import annotations

from config import settings
from services import runtime_settings as rs


def test_effective_lists_merge_env_and_dynamic(tmp_paths, monkeypatch):
    settings.__dict__.pop("finance_recipients", None)
    settings.__dict__.pop("allowed_user_ids", None)
    monkeypatch.setattr(settings, "finance_chat_ids_raw", "@envfin")
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "111")

    assert rs.effective_finance_recipients() == ["@envfin"]
    assert rs.effective_allowed_ids() == [111]

    assert rs.add_financier("NewFin")
    assert not rs.add_financier("@newfin")  # нормализация и дубликаты
    assert rs.effective_finance_recipients() == ["@envfin", "@newfin"]

    assert rs.add_allowed(222)
    assert not rs.add_allowed(111)  # уже в .env
    assert rs.effective_allowed_ids() == [111, 222]


def test_env_financier_can_be_disabled_but_whitelist_cannot(tmp_paths, monkeypatch):
    """Финансиста из .env панель отключает, доступ к подаче — нет.

    Финансист правит своё присутствие сам (иначе «убрал себя, а кнопка
    осталась»), а whitelist подачи — вопрос безопасности: снимается только
    на сервере.
    """
    settings.__dict__.pop("finance_recipients", None)
    settings.__dict__.pop("allowed_user_ids", None)
    monkeypatch.setattr(settings, "finance_chat_ids_raw", "@envfin")
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "111")

    assert rs.remove_financier("@envfin")
    assert rs.effective_finance_recipients() == []
    assert not rs.remove_allowed(111)
    assert rs.effective_allowed_ids() == [111]

    # Динамические записи убираются как и раньше.
    rs.add_financier("@dyn")
    rs.add_allowed(222)
    assert rs.remove_financier("dyn")
    assert rs.remove_allowed(222)
    assert rs.effective_finance_recipients() == []
    assert rs.effective_allowed_ids() == [111]

    # Отключённого из .env возвращает обычное «добавить».
    assert rs.add_financier("@envfin")
    assert rs.effective_finance_recipients() == ["@envfin"]


def test_persisted_to_file(tmp_paths):
    rs.add_financier("@keeper")
    # Эмулируем перезапуск: сбрасываем кэш, файл должен пережить.
    rs._cache = None
    assert "@keeper" in rs.dynamic_finance()
