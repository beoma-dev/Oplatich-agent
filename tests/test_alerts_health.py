"""Уведомления о сбоях: категории, журнал инцидентов и пульс связи.

Проверяем ровно то, ради чего блок делался: критичное нельзя выключить,
выключенная категория всё равно попадает в журнал, а моргание прокси не
превращается в поток сообщений.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import services.alerts as alerts
import services.health as health
import services.runtime_settings as rs
from config import settings


@pytest.fixture(autouse=True)
def _isolated(tmp_paths, monkeypatch):
    """Настройки и журнал — во временном файле, троттлинг чист."""
    monkeypatch.setattr(alerts, "_last_by_signature", {})
    monkeypatch.setattr(alerts, "_sent_times", [])
    monkeypatch.setattr(health, "_down_since", None)
    monkeypatch.setattr(health, "_down_reported", False)
    monkeypatch.setattr(health, "_last_ok", None)
    settings.__dict__.pop("admin_ids", None)
    monkeypatch.setattr(settings, "admin_ids_raw", "77")
    yield


def _bot() -> MagicMock:
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------
class TestConfig:
    def test_defaults_are_all_on(self):
        cfg = rs.alerts_config()
        assert cfg["enabled"] is True
        assert set(cfg["kinds"]) == set(rs.ALERT_KEYS)
        assert all(cfg["kinds"].values())
        assert cfg["link_grace_min"] == rs.LINK_GRACE_DEFAULT

    def test_critical_cannot_be_switched_off(self):
        cfg = rs.set_alerts_config(kinds={"storage": False, "backup": False})
        assert cfg["kinds"]["storage"] is True, "критичное выключили — потеря данных немая"
        assert cfg["kinds"]["backup"] is False

    def test_quiet_mode_keeps_critical(self):
        rs.set_alerts_config(enabled=False)
        assert rs.alert_kind_enabled("storage") is True
        assert rs.alert_kind_enabled("backup") is False
        # Категории пережили режим тишины: вернул — вернулись свои галочки.
        rs.set_alerts_config(kinds={"backup": False})
        rs.set_alerts_config(enabled=True)
        assert rs.alert_kind_enabled("backup") is False
        assert rs.alert_kind_enabled("delivery") is True

    def test_grace_is_clamped(self):
        assert rs.set_alerts_config(link_grace_min=999)["link_grace_min"] == rs.LINK_GRACE_MAX
        assert rs.set_alerts_config(link_grace_min=0)["link_grace_min"] == rs.LINK_GRACE_MIN

    def test_unknown_kind_is_not_silently_muted(self):
        """Новый вид алерта без записи в настройках должен доходить."""
        assert rs.alert_kind_enabled("что-то-новое") is True
        assert rs.alert_kind_enabled(None) is True


# ---------------------------------------------------------------------------
# Отправка и журнал
# ---------------------------------------------------------------------------
class TestAlertGating:
    async def test_disabled_kind_is_not_sent_but_is_logged(self):
        rs.set_alerts_config(kinds={"backup": False})
        bot = _bot()
        assert await alerts.alert_admins(bot, "Сбой бэкапа", kind="backup") == 0
        bot.send_message.assert_not_called()
        journal = rs.recent_incidents()
        assert [i["title"] for i in journal] == ["Сбой бэкапа"]
        assert journal[0]["sent"] is False, "выключен звонок, а не датчик"

    async def test_critical_goes_through_quiet_mode(self):
        rs.set_alerts_config(enabled=False)
        bot = _bot()
        assert await alerts.alert_admins(bot, "Заявка НЕ сохранилась", kind="storage") == 1
        assert rs.recent_incidents()[0]["sent"] is True

    async def test_throttled_repeat_counts_in_journal(self):
        bot = _bot()
        for _ in range(4):
            await alerts.alert_admins(bot, "Связь пропала", signature="tg", kind="telegram")
        assert bot.send_message.call_count == 1, "троттлинг пропустил шторм"
        journal = rs.recent_incidents()
        assert len(journal) == 1 and journal[0]["count"] == 4

    async def test_journal_keeps_only_recent(self):
        for i in range(rs.INCIDENT_LIMIT + 5):
            rs.record_incident("error", f"Сбой {i}", sent=True, when=time.time() + i)
        assert len(rs.recent_incidents(limit=999)) == rs.INCIDENT_LIMIT

    def test_journal_write_is_atomic(self):
        """Частые записи не должны оставлять обрезанный JSON или мусор рядом."""
        import json

        rs.record_incident("error", "Сбой", sent=True, when=time.time())
        path = settings.runtime_settings_path
        assert json.loads(path.read_text(encoding="utf-8"))["incidents"]
        assert not list(path.parent.glob("*.tmp")), "временный файл не убран"

    def test_day_counter_ignores_old(self):
        now = time.time()
        rs.record_incident("error", "Свежий", sent=True, when=now)
        rs.record_incident("error", "Позавчерашний", sent=True, when=now - 200_000)
        assert rs.incidents_since(now - 86400) == 1

    async def test_global_cap_cannot_silence_a_lost_request(self):
        """Потолок в 10 алертов за час не должен глушить потерю заявки.

        Ручка /api/client-error открыта любому с подписью Telegram, и десяток
        разных ошибок из браузера выел бы весь потолок — а следующая «заявка
        НЕ сохранилась» не ушла бы никому. Критичное идёт вне очереди.
        """
        bot = _bot()
        for i in range(alerts.GLOBAL_MAX + 3):
            await alerts.alert_admins(bot, f"Ошибка в форме {i}", kind="error")
        sent_before = bot.send_message.call_count
        assert sent_before == alerts.GLOBAL_MAX, "потолок не сработал"

        assert await alerts.alert_admins(
            bot, "Заявка НЕ сохранилась в реестр", kind="storage"
        ) == 1
        assert bot.send_message.call_count == sent_before + 1

    async def test_critical_still_respects_its_own_signature_window(self):
        """Но шторм одинакового критичного всё равно склеивается."""
        bot = _bot()
        for _ in range(4):
            await alerts.alert_admins(
                bot, "Заявка НЕ сохранилась", signature="req-fail", kind="storage"
            )
        assert bot.send_message.call_count == 1

    async def test_test_alert_ignores_settings_and_throttle(self):
        rs.set_alerts_config(enabled=False, kinds={"telegram": False})
        bot = _bot()
        for _ in range(3):
            assert await alerts.send_test_alert(bot, 77) is True
        assert bot.send_message.call_count == 3, "проверка обязана срабатывать всегда"
        assert bot.send_message.call_args.kwargs["chat_id"] == 77
        assert rs.recent_incidents() == [], "проверка — не инцидент"

    async def test_test_alert_reports_undelivered(self):
        bot = _bot()
        bot.send_message = AsyncMock(side_effect=RuntimeError("чат не начат"))
        assert await alerts.send_test_alert(bot, 77) is False


# ---------------------------------------------------------------------------
# Пульс связи
# ---------------------------------------------------------------------------
class TestLinkPulse:
    @pytest.fixture()
    def sent(self, monkeypatch):
        box: list[tuple[str, str]] = []

        async def _fake(_bot, title, details="", **_kw):
            box.append((title, details))
            return 1

        monkeypatch.setattr(alerts, "alert_admins", _fake)
        return box

    async def test_short_blink_is_silent(self, sent):
        rs.set_alerts_config(link_grace_min=5)
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("прокси моргнул"))
        assert await health.probe_once(bot, True) is False
        assert await health.probe_once(bot, False) is False
        bot.get_me = AsyncMock()
        assert await health.probe_once(bot, False) is True
        assert sent == [], "о секундном моргании прокси будить незачем"

    async def test_long_outage_reports_once_and_then_recovery(self, sent):
        rs.set_alerts_config(link_grace_min=1)
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("нет сети"))
        await health.probe_once(bot, True)
        health._down_since = time.monotonic() - 600      # провал идёт 10 минут
        await health.probe_once(bot, False)
        await health.probe_once(bot, False)              # второй раз — молчим
        assert [t for t, _d in sent] == ["Связь с Telegram пропала"]

        bot.get_me = AsyncMock()
        assert await health.probe_once(bot, False) is True
        assert [t for t, _d in sent][-1] == "Связь с Telegram восстановлена"
        assert "10 мин" in sent[-1][1]
        assert health.down_for() is None

    async def test_recovery_is_silent_below_threshold(self, sent):
        rs.set_alerts_config(link_grace_min=30)
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("нет сети"))
        await health.probe_once(bot, True)
        health._down_since = time.monotonic() - 120
        bot.get_me = AsyncMock()
        await health.probe_once(bot, False)
        assert sent == []

    async def test_alert_failure_does_not_break_the_pulse(self, monkeypatch):
        rs.set_alerts_config(link_grace_min=1)

        async def _boom(*_a, **_kw):
            raise RuntimeError("Telegram недоступен — как и ожидалось")

        monkeypatch.setattr(alerts, "alert_admins", _boom)
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=ConnectionError("нет сети"))
        await health.probe_once(bot, True)
        health._down_since = time.monotonic() - 600
        assert await health.probe_once(bot, False) is False  # цикл жив

    def test_link_state_shape(self):
        state = health.link_state()
        assert set(state) == {"alive", "last_ok_age", "down_for", "grace_min"}
        assert state["grace_min"] == rs.alerts_config()["link_grace_min"]
