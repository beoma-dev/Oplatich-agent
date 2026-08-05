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


def test_env_entries_can_be_disabled_from_the_panel(tmp_paths, monkeypatch):
    """Записи из .env отзываются панелью — финансист и доступ к подаче.

    Сам файл бот не правит (он вне его полномочий), поэтому отзыв хранится
    списком исключений. Раньше whitelist так снять было нельзя, и «отозвал
    доступ, а человек всё ещё подаёт» выглядело как поломка.
    """
    for name in ("finance_recipients", "allowed_user_ids"):
        settings.__dict__.pop(name, None)
    monkeypatch.setattr(settings, "finance_chat_ids_raw", "@envfin")
    monkeypatch.setattr(settings, "allowed_user_ids_raw", "111")

    assert rs.remove_financier("@envfin")
    assert rs.effective_finance_recipients() == []
    assert rs.remove_allowed(111)
    assert rs.effective_allowed_ids() == []

    # Динамические записи убираются как и раньше.
    rs.add_financier("@dyn")
    rs.add_allowed(222)
    assert rs.remove_financier("dyn")
    assert rs.remove_allowed(222)
    assert rs.effective_finance_recipients() == []
    assert rs.effective_allowed_ids() == []

    # Отключённого из .env возвращает обычное «добавить».
    assert rs.add_financier("@envfin")
    assert rs.effective_finance_recipients() == ["@envfin"]
    assert rs.add_allowed(111)
    assert rs.effective_allowed_ids() == [111]


def test_env_admin_cannot_be_demoted_from_the_panel(tmp_paths, monkeypatch):
    """Админ из .env — владелец сервера, и панель его не разжалует.

    Иначе назначенный из панели админ мог бы снять права тому, кто его
    назначил, и вернуть их было бы негде, кроме правки .env и рестарта.
    """
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", "999")

    assert not rs.remove_admin(999)
    assert rs.effective_admin_ids() == [999]
    assert not rs.add_admin(999)          # он и так админ

    # Назначенный из панели снимается свободно.
    assert rs.add_admin(333)
    assert rs.effective_admin_ids() == [999, 333]
    assert rs.remove_admin(333)
    assert rs.effective_admin_ids() == [999]


def test_persisted_to_file(tmp_paths):
    rs.add_financier("@keeper")
    # Эмулируем перезапуск: сбрасываем кэш, файл должен пережить.
    rs._cache = None
    assert "@keeper" in rs.dynamic_finance()
