"""Анти-дрейф: JS-зеркало формы синхронно с серверными константами.

Списки валют/статей и лимиты продублированы в webapp/index.html — этот тест
ловит рассинхронизацию при изменении только одной из сторон.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bot.models import ARTICLES, CURRENCIES
from bot.validators import MAX_FILE_SIZE_BYTES

HTML = (Path(__file__).resolve().parent.parent / "webapp" / "index.html").read_text(
    encoding="utf-8"
)


def _js_array(name: str) -> list[str]:
    m = re.search(rf"var {name} = (\[[^\]]*\])", HTML, re.S)
    assert m, f"var {name} = [...] не найден в webapp/index.html"
    return json.loads(m.group(1))


def test_currencies_mirror():
    assert _js_array("CURRENCIES") == CURRENCIES


def test_articles_mirror():
    assert _js_array("ARTICLES") == ARTICLES


def test_file_size_limit_mirror():
    m = re.search(r"var MAX_FILE_SIZE = (\d+) \* 1024 \* 1024", HTML)
    assert m, "MAX_FILE_SIZE не найден в webapp/index.html"
    assert int(m.group(1)) * 1024 * 1024 == MAX_FILE_SIZE_BYTES


def test_field_limits_mirror():
    # Длины полей: counterparty 200, comment 500, requisites 1500, article 100.
    for field_id, limit in [
        ("counterparty", 200),
        ("comment", 500),
        ("requisites", 1500),
        ("article-custom", 100),
    ]:
        m = re.search(rf'id="{field_id}" maxlength="(\d+)"', HTML)
        assert m, f"maxlength для #{field_id} не найден"
        assert int(m.group(1)) == limit, f"#{field_id}: {m.group(1)} != {limit}"


def _js_object(name: str) -> dict:
    m = re.search(rf"var {name} = (\{{[^}}]*\}})", HTML, re.S)
    assert m, f"var {name} = {{...}} не найден в webapp/index.html"
    return json.loads(m.group(1))


def test_statuses_mirror():
    """Все статусы реестра известны и «Моим заявкам» в чате, и Mini App."""
    from bot.models import REQUEST_STATUSES, STATUS_NEW, STATUS_WITHDRAWN
    from bot.my_requests import _STATUS_ICONS

    statuses = {STATUS_NEW, STATUS_WITHDRAWN} | {v for _, v in REQUEST_STATUSES.values()}
    assert set(_STATUS_ICONS) == statuses
    assert set(_js_object("STATUS_ICON")) == statuses
    assert set(_js_object("STATUS_CLASS")) == statuses


def test_popups_are_in_app_not_native():
    """Свои модальные окна: нативные рисуются в ГЛАВНОМ окне Telegram.

    На десктопе Mini App — отдельное окно, и tg.showPopup/showConfirm
    показывались «на другом экране».
    """
    # Ищем именно ВЫЗОВЫ: упоминания в комментариях объясняют, почему их нет.
    for native in ("tg.showPopup(", "tg.showConfirm(", "window.alert(", "window.confirm("):
        assert native not in HTML, f"{native} снова используется вместо showModal"
    assert 'id="modal"' in HTML
    assert "function showModal(" in HTML
    assert "function askConfirm(" in HTML


def test_icon_buttons_have_tooltips():
    """У кнопок-иконок видимая подсказка, а не только aria-label."""
    for button_id in ("admin-btn", "my-btn", "fin-btn", "skin-btn", "help-btn", "req-eye", "file-remove"):
        m = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', HTML, re.S)
        assert m, f"кнопка #{button_id} не найдена"
        tag = m.group(0)
        assert "data-tip=" in tag, f"#{button_id} без подсказки data-tip"
        assert "tip" in re.search(r'class="([^"]*)"', tag).group(1), (
            f"#{button_id} без класса tip"
        )


def test_hover_states_exist():
    """Наведение должно быть видно — и только на устройствах с курсором."""
    assert "@media (hover: hover)" in HTML
    for selector in (".add-btn:hover", ".gear:hover", ".chip:hover"):
        assert selector in HTML, f"нет состояния наведения для {selector}"
    assert "button:focus-visible" in HTML


# Поля заявки, которые рисует экран «Мои заявки»; ломаются молча при
# переименовании в api/routes._as_item.
MY_UI_FIELDS = {
    "id", "status", "sender", "counterparty", "amount", "currency", "article",
    "comment", "urgency", "planned_date", "created_at", "has_invoice",
    "requisites", "reason",
}


def test_my_requests_payload_matches_ui():
    from api.routes import _as_item

    assert MY_UI_FIELDS <= set(_as_item({}, ""))
    for field in MY_UI_FIELDS:
        assert f"it.{field}" in HTML, f"поле {field} не используется в Mini App"


def test_tooltip_class_keeps_absolute_icons():
    """Регрессия: .tip идёт по файлу позже .gear и при равной специфичности
    выбивал иконки шапки из абсолютной раскладки — они падали под заголовок."""
    m = re.search(r"\.gear\.tip[^{]*\{([^}]*)\}", HTML)
    assert m, "нет правила, возвращающего position спозиционированным иконкам"
    assert "position: absolute" in m.group(1)


def test_header_icons_are_laid_out_by_visibility():
    """Скрытая кнопка не должна оставлять дыру и лишний отступ заголовка."""
    assert "function layoutHeaderIcons(" in HTML
    for button_id in ("help-btn", "my-btn", "fin-btn", "admin-btn"):
        assert f'"{button_id}"' in HTML


def test_request_rows_open_details():
    """Нажатие на карточку заявки открывает подробности — в обоих списках."""
    assert "function showRequestDetail(" in HTML
    assert "function makeTappable(" in HTML
    assert HTML.count("makeTappable(row, it)") == 2, "не в обоих списках"
    # Реквизиты — отдельным действием, как спойлер в чате.
    assert "function showRequisites(" in HTML


def test_no_native_closing_confirmation():
    """Подтверждение закрытия Telegram рисует в СВОЁМ окне (другой монитор).

    Черновик и так сохраняется в localStorage, терять нечего.
    """
    assert "tg.enableClosingConfirmation()" not in HTML


def test_gear_press_does_not_rotate_tooltip():
    """Поворот шестерёнки крутил и подсказку — она превращалась в полоску."""
    m = re.search(r"\.gear:active\s*\{([^}]*)\}", HTML)
    assert m, "нет состояния нажатия у иконок шапки"
    assert "rotate" not in m.group(1)


def test_lists_offer_explicit_detail_and_delete():
    """Кроме нажатия на строку есть явные кнопки — так надёжнее."""
    assert "function detailButton(" in HTML
    assert "function deleteButton(" in HTML
    assert HTML.count("actions.appendChild(detailButton(it))") == 2
    assert "/api/requests/delete" in HTML


def test_tappable_rows_do_not_select_text():
    """Клик по карточке выделял текст вместо открытия подробностей."""
    m = re.search(r"\.my-item\.tappable\s*\{([^}]*)\}", HTML)
    assert m, "нет стилей кликабельной строки"
    assert "user-select: none" in m.group(1)
    assert "cursor: pointer" in m.group(1)


def test_no_duplicate_element_ids():
    """Дубль id ломает getElementById молча.

    Из-за пары одинаковых fin-list список заявок панели финансиста
    отрисовывался в скрытый контейнер админ-панели — на экране пусто.
    """
    import collections

    ids = re.findall(r'\sid="([^"]+)"', HTML)
    duplicates = [i for i, n in collections.Counter(ids).items() if n > 1]
    assert not duplicates, f"повторяющиеся id: {duplicates}"


def test_hints_can_be_dismissed_and_return_by_themselves():
    """Крестик есть и у контрагентов, и у статей расходов.

    Кнопки «вернуть скрытые» намеренно нет: подсказка возвращается сама,
    когда статьёй или контрагентом снова пользуются (unhideUsed).
    """
    assert "chip-x" in HTML          # контрагенты
    assert "seg-x" in HTML           # статьи расходов
    assert "invoice_hidden_cp_v1" in HTML
    assert "invoice_hidden_art_v1" in HTML
    assert "function unhideUsed(" in HTML
    assert "chip-restore" not in HTML


def test_panel_status_actions_mirror_server():
    """Кнопки статуса в панели — те же ключи, что REQUEST_STATUSES."""
    from bot.models import REQUEST_STATUSES

    m = re.search(r"var STATUS_ACTIONS = \[(.*?)\];", HTML, re.S)
    assert m, "STATUS_ACTIONS не найден"
    keys = set(re.findall(r'key:\s*"(\w+)"', m.group(1)))
    assert keys == set(REQUEST_STATUSES)
    statuses = set(re.findall(r'status:\s*"([^"]+)"', m.group(1)))
    assert statuses == {v for _, v in REQUEST_STATUSES.values()}
    # Причину спрашиваем ровно у тех же статусов, что и карточка в чате.
    assert "function showPrompt(" in HTML
    assert "/api/finance/status" in HTML


def test_second_skin_is_token_only_and_remembered():
    """Скин «Неон» — второй вариант оформления, выбор запоминается.

    Тема переопределяет ТОЛЬКО токены :root: вся вёрстка написана через них,
    поэтому дублировать правила не нужно и они не разъедутся.
    """
    assert ':root[data-skin="neon"]' in HTML
    for token in ("--bg:", "--bg2:", "--text:", "--accent:"):
        m = re.search(r':root\[data-skin="neon"\]\s*\{([^}]*)\}', HTML, re.S)
        assert token in m.group(1), f"скин не переопределяет {token}"
    assert "invoice_skin_v1" in HTML          # ключ хранения выбора
    assert "invoice_skin_anim_v1" in HTML     # живой фон отключается отдельно
    assert "function applySkin(" in HTML
    # Ранний скрипт в <head> ставит атрибут до отрисовки — иначе неон мигал бы
    # светлой темой при каждом открытии формы.
    head = HTML.split("</head>", 1)[0]
    assert "invoice_skin_v1" in head, "выбор скина применяется слишком поздно"


def test_skin_canvas_is_guarded_and_theme_aware():
    """Живой фон работает в обеих темах и берёт цвета у активной.

    Зашитый изумруд смотрелся бы грязью на светлой телеграмной теме,
    поэтому акцент и светлота фона читаются из CSS во время работы.
    """
    assert 'id="skin-field"' in HTML
    assert "canvas.getContext" in HTML          # без canvas форма не падает
    assert "function readTheme(" in HTML
    assert "--accent" in HTML and "0.2126" in HTML   # яркость фона считается
    # В телеграмной теме сеть тише: там уже есть свои цветные пятна.
    m = re.search(r'#skin-field\s*\{([^}]*)\}', HTML, re.S)
    assert m and "display: block" in m.group(1)
    assert ':root[data-skin="neon"] #skin-field { opacity: .75; }' in HTML


def test_skin_lives_in_its_own_settings_screen():
    """Тема выбирается на отдельном экране, а не кнопкой-переключателем."""
    assert 'id="skin-view"' in HTML
    assert 'data-skin-value="tg"' in HTML and 'data-skin-value="neon"' in HTML
    assert 'id="skin-anim-seg"' in HTML
    assert '$("skin-btn").addEventListener("click", openSkin);' in HTML


def test_neon_does_not_kill_selected_button_contrast():
    """Регрессия: правило скина было специфичнее .seg button.active и
    убивало заливку — почти чёрный текст оставался на тёмном фоне."""
    m = re.search(r':root\[data-skin="neon"\] \.seg button([^{]*)\{', HTML)
    assert m, "нет правила фона сегментов для неона"
    assert ":not(.active)" in m.group(1), "правило перекрывает выбранную кнопку"


def test_header_survives_five_icons():
    """С пятью иконками (админ + финансист) заголовок уезжал на вторую строку.

    Раскладка сжимает шаг и типографику по числу ВИДИМЫХ кнопок — проверено
    в браузере на 390 px: одна строка.
    """
    assert "header.icons-5 h1" in HTML
    assert "header.icons-4 .gear, header.icons-5 .gear" in HTML
    m = re.search(r"function layoutHeaderIcons\(\)\s*\{(.+?)\n  \}", HTML, re.S)
    assert m, "layoutHeaderIcons не найдена"
    body = m.group(1)
    assert "visible.length >= 4" in body, "шаг иконок не зависит от их числа"
    assert "icons-" in body, "класс плотности не выставляется"


def test_autofill_offer_is_editable_and_not_glued_to_buttons():
    """Предложение можно поправить прямо в нём, и оно не липнет к кнопкам.

    Регрессия: блок стоял вплотную к сегменту «Файл счёта / Реквизиты»
    (зазор 0 px) и читался как наложение.
    """
    m = re.search(r"\.autofill\s*\{([^}]*)\}", HTML, re.S)
    assert m and "margin-bottom" in m.group(1), "нет отступа снизу у предложения"
    assert "#file-warn:not(.hidden) { margin-bottom" in HTML

    # Правятся ровно те поля, что попадают в заявку.
    keys = re.findall(r'key:\s*"(\w+)"', HTML.split("AUTOFILL_FIELDS")[1][:400])
    assert set(keys) == {"amount", "counterparty", "requisites"}
    assert "function autofillValue(" in HTML
    assert 'id="autofill-skip">Отмена<' in HTML


def test_draft_can_be_cleared_from_two_places():
    """Сброс черновика: ссылка в плашке восстановления и строка внизу формы.

    Обе точки ведут в одно подтверждение — стереть введённое молча нельзя.
    """
    assert 'id="draft-clear"' in HTML      # там, где черновик только что подставился
    assert 'id="form-reset"' in HTML       # когда плашка уже исчезла
    assert "function resetForm(" in HTML
    assert "function askReset(" in HTML
    assert '$("form-reset").addEventListener("click", askReset);' in HTML
    assert '$("draft-clear").addEventListener("click", askReset);' in HTML
    # Сброс обязан снести и сохранённый черновик, иначе он вернётся при
    # следующем открытии формы.
    body = HTML[HTML.index("function resetForm("):]
    assert "clearDraft();" in body[:400]
