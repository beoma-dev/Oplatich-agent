"""Проверка бэкапа: она сама не должна врать.

Инструмент, который кричит на исправном архиве, перестают читать — и он
молчит уже по делу. 25.08.2026 verify_backup именно так и делал: после
переезда на Google он объявлял «реестра в архиве нет» и выносил вердикт
«восстановление под вопросом» на совершенно годном бэкапе.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from config import settings
from scripts import verify_backup as vb


@pytest.fixture(autouse=True)
def _clean_counter(monkeypatch):
    monkeypatch.setattr(vb, "_failures", 0)
    yield


class TestMissingMirror:
    def test_google_backend_without_xlsx_is_normal(self, tmp_path, monkeypatch, tmp_paths):
        """На google-бэкенде xlsx-зеркала может не быть — это не провал."""
        monkeypatch.setattr(settings, "storage_backend", "google")
        (tmp_path / "storage").mkdir()
        assert vb._check_registry(tmp_path) == []
        assert vb._failures == 0, "исправный архив объявлен сломанным"

    def test_local_backend_still_demands_the_registry(self, tmp_path, monkeypatch, tmp_paths):
        """А вот на локальном бэкенде реестр обязан быть — иначе терять нечего."""
        monkeypatch.setattr(settings, "storage_backend", "local")
        monkeypatch.setattr(settings, "registry_xlsx_file", "registry.xlsx")
        (tmp_path / "storage").mkdir()
        assert vb._check_registry(tmp_path) == []
        assert vb._failures == 1, "пропажу реестра проглотили"


class TestFileCount:
    def test_count_is_never_negative(self, tmp_path, tmp_paths):
        """Счётчик «файлов счетов» уходил в −1: реестр вычитался всегда."""
        storage = tmp_path / "storage"
        storage.mkdir()
        vb._check_files(tmp_path, [])
        assert vb._failures == 0

    def test_registry_itself_is_not_counted_as_an_invoice(self, tmp_path, tmp_paths):
        storage = tmp_path / "storage"
        storage.mkdir()
        (storage / "registry.xlsx").write_bytes(b"x")
        (storage / "20260825_ООО Ромашка_100.pdf").write_bytes(b"%PDF")
        printed: list[str] = []
        vb._say = lambda good, text: printed.append(text)  # noqa: SLF001
        vb._check_files(tmp_path, [])
        assert any("счетов от пользователей: 1" in t for t in printed), printed


def test_extraction_uses_the_data_filter():
    """Распаковка обязана отбивать пути наружу — как в services/restore."""
    source = Path(vb.__file__).read_text(encoding="utf-8")
    assert 'filter="data"' in source, "tar разворачивается без фильтра"
