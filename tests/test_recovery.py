"""Подтверждение исправления: перезапуск, частичная доставка и честные границы."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import settings
from services import alerts, health, recovery
from services import runtime_settings as rs


@pytest.fixture(autouse=True)
def isolated(tmp_paths, monkeypatch):
    monkeypatch.setattr(settings, 'admin_ids_raw', '11,22')
    settings.__dict__.pop('admin_ids', None)
    monkeypatch.setattr(alerts, '_last_by_signature', {})
    monkeypatch.setattr(alerts, '_sent_times', [])
    yield
    settings.__dict__.pop('admin_ids', None)


def bot():
    value = MagicMock()
    value.send_message = AsyncMock()
    value.get_me = AsyncMock()
    return value


async def test_no_recovery_without_a_failure():
    b = bot()
    await recovery.recover(b, 'mirror', 'Всё хорошо')
    b.send_message.assert_not_awaited()


async def test_recovery_survives_restart_and_is_not_a_new_incident():
    b = bot()
    await alerts.alert_admins(b, 'Ошибка зеркала', kind='mirror', recovery_key='mirror')
    started = recovery.get_sync('mirror')['started']
    # Нет кэша состояния: подтверждение читает сохранённую запись.
    b.send_message.reset_mock()
    await recovery.recover(b, 'mirror', 'Зеркало восстановлено', 'Состав совпал.')
    assert b.send_message.await_count == 2
    text = b.send_message.await_args.kwargs['text']
    assert text.startswith('✅') and '🚨' not in text
    assert 'Зеркало восстановлено' in text
    assert recovery.get_sync('mirror')['started'] == started
    assert rs.incidents_since(0) == 1
    assert rs.recent_incidents()[0]['recovered_ts']
    await recovery.recover(b, 'mirror', 'Зеркало восстановлено')
    assert b.send_message.await_count == 2


async def test_partial_delivery_retries_only_the_missing_admin():
    b = bot()
    await recovery.fail('x', 'backup', 'Сбой')
    async def send(**kw):
        if kw['chat_id'] == 22:
            raise ConnectionError()
    b.send_message.side_effect = send
    await recovery.recover(b, 'x', 'Восстановлено')
    assert recovery.get_sync('x')['finished'] == 0
    b.send_message.reset_mock()
    b.send_message.side_effect = None
    await recovery.flush(b)
    assert [c.kwargs['chat_id'] for c in b.send_message.await_args_list] == [22]
    assert recovery.get_sync('x')['finished'] == 1


async def test_recovery_not_hidden_by_error_throttle_and_html_escaped():
    b = bot()
    alerts._sent_times.extend([__import__('time').monotonic()] * alerts.GLOBAL_MAX)
    await recovery.fail('x', 'backup', 'Сбой')
    await recovery.recover(b, 'x', '<исправлено>', '<данные>')
    assert b.send_message.await_count == 2
    assert '&lt;исправлено&gt;' in b.send_message.await_args.kwargs['text']


async def test_muted_category_still_records_resolution_without_a_message():
    b = bot()
    await recovery.fail('x', 'backup', 'Сбой')
    rs.set_alerts_config(kinds={'backup': False})
    await recovery.recover(b, 'x', 'Исправлено')
    b.send_message.assert_not_awaited()
    assert recovery.get_sync('x')['finished'] == 1


async def test_concurrent_recovery_sends_one_confirmation_each():
    b = bot()
    await recovery.fail('x', 'backup', 'Сбой')
    await asyncio.gather(*[recovery.recover(b, 'x', 'Исправлено') for _ in range(3)])
    assert b.send_message.await_count == 2


async def test_repeated_failure_starts_a_new_cycle():
    b = bot()
    await recovery.fail('x', 'backup', 'Сбой')
    await recovery.recover(b, 'x', 'Исправлено')
    await recovery.fail('x', 'backup', 'Сбой')
    assert recovery.get_sync('x')['resolved'] is None
    await recovery.recover(b, 'x', 'Исправлено')
    assert b.send_message.await_count == 4


async def test_telegram_recovery_after_restart(monkeypatch):
    import time
    b = bot()
    recovery.fail_sync('telegram-link', 'telegram', health.LINK_DOWN_TITLE)
    with recovery._connect() as conn:
        conn.execute('UPDATE alert_recovery SET started=?', (time.time() - 600,))
    monkeypatch.setattr(health, '_down_since', None)
    monkeypatch.setattr(health, '_last_ok', None)
    assert await health.probe_once(b, True)
    assert b.send_message.await_count == 2
    assert 'Связь с Telegram восстановлена' in b.send_message.await_args.kwargs['text']


async def test_one_restored_proxy_does_not_close_other_proxy_failure():
    b = bot()
    await alerts.alert_admins(b, 'Резерв недоступен', kind='telegram', recovery_key='proxy:a')
    await recovery.fail('proxy:b', 'telegram', 'Резерв недоступен')
    await recovery.recover(b, 'proxy:a', 'Первый канал восстановлен')
    assert not rs.recent_incidents()[0].get('recovered_ts')
    assert recovery.get_sync('proxy:b')['resolved'] is None


async def test_spare_recovery_imports_the_last_old_alert(monkeypatch):
    from services import proxy, spare_channel
    monkeypatch.setattr(settings, 'proxy_url', 'socks5://main:1080,socks5://spare:1080')
    settings.__dict__.pop('proxy_urls', None)
    monkeypatch.setattr(proxy, 'active', lambda: 'socks5://main:1080')
    monkeypatch.setattr(proxy, 'probe_proxy', AsyncMock(return_value=''))
    rs.record_incident('telegram', 'Запасной канал до Telegram не отвечает',
                       sent=True, when=__import__('time').time(),
                       details='Проверка: socks5://spare:1080 — NetworkError')
    b = bot()
    await spare_channel.check_spares(b)
    assert b.send_message.await_count == 2
    assert '✅' in b.send_message.await_args.kwargs['text']
    assert 'Канал до Telegram восстановлен' in b.send_message.await_args.kwargs['text']
    settings.__dict__.pop('proxy_urls', None)


async def test_skipped_registry_check_is_not_recovery(monkeypatch):
    from services import registry_check
    b = bot()
    await recovery.fail('registry-mirror', 'mirror', 'Расхождение')
    monkeypatch.setattr(registry_check, 'check', AsyncMock(return_value={
        'checked': False, 'ok': True, 'reason': 'реестр недоступен',
    }))
    await registry_check.alert_if_diverged(b)
    b.send_message.assert_not_awaited()
    assert recovery.get_sync('registry-mirror')['resolved'] is None


async def test_backup_creation_does_not_prove_archive_delivery(monkeypatch, tmp_path):
    from services import backup, tg_retry
    b = bot()
    archive = tmp_path / 'archive.tar.gz'
    archive.write_bytes(b'archive')
    monkeypatch.setattr(backup, 'create_backup_sync', lambda: archive)
    monkeypatch.setattr(tg_retry, 'send_with_retry', AsyncMock(side_effect=ConnectionError()))
    await recovery.fail('backup-create', 'backup', 'Сборка')
    await recovery.fail('backup-delivery:11', 'backup', 'Отправка')
    await backup.run_backup(b)
    assert recovery.get_sync('backup-create')['resolved'] is not None
    assert recovery.get_sync('backup-delivery:11')['resolved'] is None


async def test_browser_error_never_claims_recovery_from_server_health():
    b = bot()
    await alerts.alert_admins(b, 'Ошибка в форме', kind='error')
    assert 'требует проверки той же операции' in b.send_message.await_args.kwargs['text']
    b.send_message.reset_mock()
    await recovery.flush(b)
    b.send_message.assert_not_awaited()


async def test_broken_recovery_database_does_not_stop_telegram_pulse(monkeypatch):
    b = bot()
    def broken(*args):
        raise OSError('диск недоступен')
    monkeypatch.setattr(recovery, 'get_sync', broken)
    assert await health.probe_once(b, True)
