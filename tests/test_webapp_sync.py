"""Анти-дрейф: JS-зеркало формы синхронно с серверными константами.

Списки валют/статей и лимиты продублированы в webapp/index.html — этот тест
ловит рассинхронизацию при изменении только одной из сторон.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bot.models import ARTICLES, CURRENCIES
from bot.validators import MAX_EXTRA_FILES, MAX_FILE_SIZE_BYTES

WEBAPP = Path(__file__).resolve().parent.parent / "webapp"


def _read(name: str) -> str:
    return (WEBAPP / name).read_text(encoding="utf-8")


MARKUP = _read("index.html")
CSS = _read("app.css")
JS = _read("app.js")
FIELD = _read("skin-field.js")
LIB = _read("form-lib.js")
# Большинству проверок неважно, в каком файле лежит правило или функция, —
# важно, что они есть и согласованы между собой. Разбор по файлам делают
# те тесты, где это существенно (порядок правил, состав разметки).
#
# Берём ВСЕ файлы приложения, а не перечисленные пятеро: из app.js за день
# уехало полдюжины панелей, и проверки перестали их видеть — поле, которым
# пользуется вынесенный файл, выглядело неиспользуемым. Список пришлось бы
# дописывать при каждом выносе, и однажды бы забыли.
HTML = "\n".join(
    sorted(
        path.read_text(encoding="utf-8")
        for path in WEBAPP.iterdir()
        if path.suffix in {".html", ".css", ".js"}
    )
)


def _media_blocks(query: str) -> list[str]:
    """Все @media-блоки с таким условием — с телом, сбалансированным по скобкам."""
    blocks = []
    for m in re.finditer(rf"@media \({re.escape(query)}\) \{{", HTML):
        depth, i = 0, m.end() - 1
        while True:
            if HTML[i] == "{":
                depth += 1
            elif HTML[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        blocks.append(HTML[m.end():i])
    return blocks


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


def test_extra_files_limit_mirror():
    """Предел числа дополнительных документов зеркалится в JS.

    Разойдись они — форма примет шестой файл, а сервер откажет всей заявке
    после того, как человек её заполнил.
    """
    m = re.search(r"var MAX_EXTRA_FILES = (\d+);", HTML)
    assert m, "MAX_EXTRA_FILES не найден в JS формы"
    assert int(m.group(1)) == MAX_EXTRA_FILES


def test_field_limits_mirror():
    # Длины полей: counterparty 200, comment 500, requisites 1500,
    # article 100, срок исполнения работ 200.
    for field_id, limit in [
        ("counterparty", 200),
        ("comment", 500),
        ("requisites", 1500),
        ("article-custom", 100),
        ("work-deadline", 200),
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


def test_admin_rights_come_with_access_not_a_probe():
    """Отдельная проба админства писала отказ в аудит КАЖДОМУ не-админу.

    Приложение дёргало /admin/settings, чтобы понять, показывать ли админские
    вкладки, и получало 403 — то есть журнал безопасности, заведённый отвечать
    «кто пытался», забивался обычными открытиями формы: 147 записей из 200 на
    боевом за неделю. Права и так приезжают в /api/access.
    """
    assert "tryAdmin" not in JS, "проба админства вернулась"
    assert JS.count("applyAdmin(d.admin)") == 2, "права больше не берутся из /api/access"


def test_animated_help_shows_what_the_app_really_does():
    """Живая инструкция обещает ровно то, что есть в приложении.

    Сцены — это картинка интерфейса, нарисованная руками, и разъезжается она
    молча: кнопку переименовали, а тур показывает старое имя новичку, для
    которого он и сделан. Поэтому подписи в сценах сверяются с настоящими.
    """
    from services.notifier import OPEN_LABEL

    tour = _read("help-tour.js")
    # Кнопки, которые тур рисует, — те же строки, что в самих панелях.
    assert '"📄 Акт / УПД"' in _read("closing-panel.js")
    assert '"⏰ Напомнить"' in _read("nudge-panel.js")
    for label in ("📄 Акт / УПД", "⏰ Напомнить", OPEN_LABEL):
        assert label in tour, f"тур не показывает «{label}» — или показывает старое"
    # Статусы — из общего зеркала: тур рисует «Новая» и «Оплачена».
    from bot.models import REQUEST_STATUSES, STATUS_NEW

    assert STATUS_NEW in tour
    assert REQUEST_STATUSES["PAID"][1] in tour
    # Счёт и реквизиты необязательны с 26.08.2026 — тур обязан это показывать,
    # иначе он учит заполнять то, чего можно не заполнять.
    assert "ни счёта, ни реквизитов" in tour
    # Срочность подписана как в форме, а не «своими словами».
    for pill in ("🔴 Срочно", "🟢 Обычная"):
        assert pill in tour and pill in MARKUP, f"подпись срочности разошлась: {pill}"


def test_animated_help_stays_behind_its_flag():
    """Тур строится только по флагу и только если движение не отключено."""
    tour = _read("help-tour.js")
    assert "function helpTour(enabled)" in tour
    assert "if (!enabled" in tour, "тур строится без флага"
    assert "prefers-reduced-motion" in tour, "тур игнорирует «уменьшить движение»"
    # Флаг приезжает с сервера, а не зашит в приложение.
    assert "helpTour(d.animated_help)" in JS


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
    for button_id in ("admin-btn", "my-btn", "fin-btn", "help-btn", "req-eye", "file-remove"):
        m = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', HTML, re.S)
        assert m, f"кнопка #{button_id} не найдена"
        tag = m.group(0)
        assert "data-tip=" in tag, f"#{button_id} без подсказки data-tip"
        assert "tip" in re.search(r'class="([^"]*)"', tag).group(1), (
            f"#{button_id} без класса tip"
        )


def test_inputs_cannot_outgrow_their_card():
    """Поле ввода не должно вылезать за карточку ни на одном движке.

    На iPhone поле времени выходило за край карточки: WKWebView меряет
    нативный контрол date/time по содержимому и прибавляет padding снаружи.
    Ни Chromium, ни WebKit под Linux этого не повторяют — браузерные тесты
    такую поломку не увидят, поэтому страхуемся в исходнике: жёсткий предел
    ширины и обычная блочная модель вместо нативной.
    """
    rule = re.search(
        r"input\[type=text\], input\[type=date\], input\[type=time\], textarea \{([^}]*)\}",
        CSS,
    )
    assert rule, "правило для полей ввода потерялось"
    body = rule.group(1)
    for prop in ("box-sizing: border-box", "max-width: 100%", "min-width: 0"):
        assert prop in body, f"{prop} — без него поле снова уедет за карточку на iOS"
    native = re.search(r"input\[type=date\], input\[type=time\] \{([^}]*)\}", CSS)
    assert native and "appearance: none" in native.group(1)
    assert "-webkit-appearance: none" in native.group(1), "Safari смотрит на префикс"


def test_hover_states_exist():
    """Наведение должно быть видно — и только на устройствах с курсором."""
    assert "@media (hover: hover)" in HTML
    for selector in (".add-btn:hover", ".gear:hover", ".chip-wrap:hover"):
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
        # it.<поле> в списках, item.<поле> в панелях: туда элемент приходит
        # аргументом и зовётся полным словом.
        assert f"it.{field}" in HTML or f"item.{field}" in HTML, (
            f"поле {field} не используется в Mini App"
        )


def test_tooltip_class_keeps_header_icons_in_place():
    """Регрессия: .tip идёт по файлу позже .gear и при равной специфичности
    задаёт кнопкам position. С absolute кнопки складывались в стопку и клик по
    одной перехватывала соседняя; с relative подсказка крайней левой кнопки
    уезжала за левый край экрана. Верно static: раскладку держит flex, а
    подсказка якорится к панели. Глазку и «убрать файл» absolute нужен.
    """
    m = re.search(r"\.gear\.tip[^{]*\{([^}]*)\}", HTML)
    assert m, "нет правила, задающего position кнопкам шапки"
    # static, а не relative: подсказка тогда отсчитывается от панели. У крайней
    # левой кнопки она иначе уезжает за левый край экрана на узких телефонах.
    assert "position: static" in m.group(1)
    assert "max-width: calc(100vw" in HTML, "у подсказки нет предела ширины"
    m2 = re.search(r"\.eye-btn\.tip[^{]*\{([^}]*)\}", HTML)
    assert m2 and "position: absolute" in m2.group(1)


def test_header_icons_are_laid_out_by_visibility():
    """Скрытая кнопка не должна оставлять дыру и лишний отступ заголовка."""
    assert "function layoutHeaderIcons(" in HTML
    for button_id in ("help-btn", "my-btn", "fin-btn", "admin-btn"):
        assert f'"{button_id}"' in HTML


def test_request_rows_open_details():
    """Нажатие на карточку заявки открывает подробности — в обоих списках."""
    assert "function showRequestDetail(" in HTML
    assert "function makeTappable(" in HTML
    # В «Моих заявках» третьим аргументом идёт перезагрузка списка: туда
    # переехало админское удаление, и после него список надо обновить.
    calls = HTML.count("makeTappable(row, it,") + HTML.count("makeTappable(row, it)")
    assert calls == 2, "не в обоих списках"
    assert 'makeTappable(row, it, it.status === "Отозвана" ? null : loadMy)' in HTML
    assert "makeTappable(row, it, loadFinance)" in HTML
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


def test_details_open_by_tapping_the_row_only():
    """Подробности открывает сама строка — отдельной кнопки нет.

    «Подробнее» дублировало нажатие по карточке и занимало место в ряду
    действий, где каждая кнопка что-то МЕНЯЕТ.
    """
    assert "function makeTappable(" in HTML
    calls = HTML.count("makeTappable(row, it,") + HTML.count("makeTappable(row, it)")
    assert calls == 2, "не в обоих списках"
    assert "detailButton" not in JS, "кнопка «Подробнее» вернулась"
    # Подсказки «нажмите, чтобы открыть» больше нет: карточка и так
    # выглядит нажимаемой, а строка занимала место в шапке.
    assert "tap-hint" not in MARKUP and "tap-hint" not in CSS
    # Удаление — по-прежнему отдельной кнопкой: оно необратимо.
    assert "function deleteButton(" in HTML
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


def test_skin_lives_in_the_settings_screen():
    """Тема выбирается вкладкой настроек, а не отдельной кнопкой в шапке.

    Своя иконка в шапке съедала место у заголовка, а экран дублировал то,
    чем уже стали настройки.
    """
    assert 'id="pane-skin"' in HTML
    assert 'data-skin-value="tg"' in HTML and 'data-skin-value="neon"' in HTML
    assert 'id="skin-anim-seg"' in HTML
    # Старого экрана и кнопки не осталось — иначе будет два места с темой.
    assert 'id="skin-view"' not in HTML
    assert 'id="skin-btn"' not in HTML
    assert "openSkin" not in HTML
    # Оформление открывается вместе с настройками, до всякой проверки прав.
    opener = HTML[HTML.index("function openAdmin("):]
    assert "markSkinChoice();" in opener[:700]
    assert 'setSeg("skin-anim-seg"' in opener[:700]


def test_neon_does_not_kill_selected_button_contrast():
    """Регрессия: правило скина было специфичнее .seg button.active и
    убивало заливку — почти чёрный текст оставался на тёмном фоне."""
    m = re.search(r':root\[data-skin="neon"\] \.seg button([^{]*)\{', HTML)
    assert m, "нет правила фона сегментов для неона"
    assert ":not(.active)" in m.group(1), "правило перекрывает выбранную кнопку"


def test_header_survives_five_icons():
    """С пятью иконками (админ + финансист) шапка остаётся в одну строку.

    Кнопки собраны в одну панель, поэтому ширину под них меряем по факту, а
    тесноту гасим не кеглем надписи (её больше нет), а высотой марки.
    """
    assert '<div class="gears" id="header-icons">' in MARKUP
    assert "header.icons-5 .mark" in HTML, "при пяти иконках марка не ужимается"
    m = re.search(r"function layoutHeaderIcons\(\)\s*\{(.+?)\n  \}", HTML, re.S)
    assert m, "layoutHeaderIcons не найдена"
    body = m.group(1)
    assert "icons-" in body, "класс плотности не выставляется"
    assert "paddingRight" in body, "отступ под панель не считается"
    assert "panel.getBoundingClientRect()" in body and "box.width" in body, (
        "ширина панели зашита числом — она зависит от числа видимых кнопок"
    )


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


def test_local_assets_exist():
    """Все локальные файлы, на которые ссылается страница, лежат в webapp/.

    Mini App отдаётся StaticFiles из этого каталога: опечатка в пути или
    забытый файл превращаются в битую картинку уже в проде, а не в тестах.
    """
    webapp = Path(__file__).resolve().parent.parent / "webapp"
    refs = set(re.findall(r'(?:src|href)="(?!https?:|data:|#)([^"]+)"', HTML))
    refs |= set(re.findall(r'\.src = "(?!https?:|data:)([^"]+)"', HTML))
    assert refs, "ссылки на локальные файлы не найдены — сломался разбор"
    missing = sorted(r for r in refs if not (webapp / r).exists())
    assert not missing, f"нет файлов: {missing}"


def test_brand_is_oplatych():
    """Имя бота осталось заголовком экрана, даже когда его не видно.

    Надпись убрана из вида — её место занял персонаж, — но текст остаётся в
    разметке: без него у экрана нет заголовка для скринридера.
    """
    assert "<title>Оплатыч" in HTML
    assert '<link rel="icon" href="logo.svg"' in HTML
    assert '<span class="brand-name">Оплатыч</span>' in MARKUP
    assert '<svg id="brand-mark" class="mark"' in MARKUP
    # Скрыт визуально, но не от вспомогательных технологий: display:none
    # выкинул бы заголовок и из дерева доступности.
    assert ".brand-name {" in HTML
    assert "clip: rect(0 0 0 0)" in HTML
    assert "display: none" not in HTML[HTML.index(".brand-name {"):HTML.index(".brand-name {") + 260]
    # Пустой список «Моих заявок» показывает персонажа, а не голую строку.
    assert 'art.setAttribute("class", "empty-art");' in JS


def test_bot_speaks_of_itself_by_name():
    """Бот везде называет себя Оплатычем, а не «ботом»."""
    assert "Оплатыч прочитал счёт" in HTML
    assert "Бот прочитал счёт" not in HTML
    assert "Оплатыч заодно прочитает счёт" in HTML


def test_horizontal_lists_are_scrollable_by_mouse():
    """Подсказки листались только пальцем: полоса спрятана, колесо не крутит.

    Регрессия с десктопного Telegram — длинный список контрагентов упирался
    в край, и добраться до остальных было нечем.
    """
    assert "function wireHScroll(" in HTML
    assert "function updateFades(" in HTML
    # Колесо разворачиваем вбок — для этого нужен неленивый обработчик.
    body = HTML[HTML.index("function wireHScroll("):]
    assert '{ passive: false }' in body[:900], "wheel должен уметь preventDefault"
    assert "box.scrollLeft += d" in body[:900]
    # Список подсказок обязан быть подключён, иначе фикс ни на что не влияет.
    assert "wireHScroll(box);" in HTML
    # На десктопе возвращаем тонкую полосу, на телефоне она не нужна.
    assert "@media (pointer: fine)" in HTML
    assert ".hscroll::-webkit-scrollbar { display: block;" in HTML
    for cls in (".fade-r {", ".fade-l {", ".fade-l.fade-r {"):
        assert cls in HTML, f"нет растушёвки {cls}"


def test_main_button_follows_the_skin():
    """Родная кнопка Telegram красится темой клиента и в неоне была синей."""
    assert "function paintMainButton(" in HTML
    assert "tg.MainButton.setParams({ color: color, text_color: textColor })" in HTML
    # Цвет берём из тех же токенов, что и вся страница, и он ЗАВИСИТ от
    # готовности формы: незаполненная — серый токен подсказки, заполненная —
    # акцент. Гасить кнопку нельзя, погашенную не нажать и не спросить.
    assert 'ready === false ? "var(--hint)" : "var(--accent)"' in HTML
    assert 'color:" + bg + ";background-color:var(--accent-text)' in HTML
    assert "function cssHex(" in HTML, "setParams принимает только hex"
    # Перекрашивать надо при каждой смене шкуры, а не один раз на старте.
    skin = HTML[HTML.index("function applySkin("):]
    assert "paintMainButton();" in skin[:900]


def test_hard_text_rules_mirror():
    """Жёсткие правила текста продублированы в JS — иначе реакция запоздает.

    Сервер такое тоже отклонит, но человек узнавал бы об этом только после
    отправки: печатая «ннннннннн», он не видел ничего. Правила простые и
    детерминированные, поэтому зеркалятся; эвристика «похоже на мусор»
    намеренно осталась только на сервере — дублировать её значит развести
    два разных мнения о том, что считать мусором.
    """
    from bot.validators import _REPEAT_RE, looks_broken

    assert "function brokenReason(" in HTML
    # Порог повтора один и тот же с обеих сторон.
    assert "{5,}" in _REPEAT_RE.pattern
    assert "/(.)\\1{5,}/u" in HTML, "в JS другой порог повтора"
    # Буквы проверяются юникодным классом: \W в JS работает по ASCII, и
    # кириллица считалась бы «не буквой».
    assert "/\\p{L}/u" in HTML
    # Обе стороны согласны на конкретных значениях.
    assert looks_broken("н" * 22)
    assert looks_broken("12321432132")
    assert looks_broken("ООО «Ромашка»") is None


def test_registry_card_opens_sheet_and_drive():
    """В карточке реестра — и таблица, и папка Диска с файлами счетов."""
    assert 'id="open-sheet"' in HTML
    assert 'id="open-drive"' in HTML
    assert "function wireOpenLink(" in HTML
    assert 'wireOpenLink("open-sheet", d.registry_url)' in HTML
    assert 'wireOpenLink("open-drive", d.drive_url)' in HTML
    # Подсказка про /export остаётся только когда не видно ни одной кнопки.
    assert 'toggle("hidden", hasSheet || hasDrive)' in HTML


def test_admin_settings_are_split_into_tabs():
    """Настройки разложены по вкладкам, каждая кнопка ведёт в свою панель."""
    panes = re.findall(r'<button[^>]*class="tab[^"]*"[^>]*data-pane="(\w+)"', HTML)
    # «Бета» стоит ПОСЛЕ «Оформления» намеренно: обычный пользователь видит
    # обе, и открываться настройки должны на оформлении, а не на нишевом
    # выключателе распознавания.
    assert panes == ["fin", "access", "data", "health", "skin", "beta"], panes
    for name in panes:
        assert f'id="pane-{name}"' in HTML, f"нет панели для вкладки {name}"
        assert f'id="tab-{name}"' in HTML
    # Ровно одна панель открыта в разметке — остальные скрыты.
    open_panes = re.findall(r'<div class="pane" id="pane-(\w+)"', HTML)
    assert open_panes == ["skin"], open_panes
    assert "function showAdminTab(" in HTML
    # Вкладка получателя: только его собственные настройки.
    fin_pane = HTML[HTML.index('id="pane-fin"'):HTML.index('id="pane-access"')]
    # Карточка с 26.08 покрывает два потока: уведомление о новой заявке
    # (сразу) и напоминания по срокам (раз в сутки), — отсюда заголовок шире.
    assert "⏰ Мои уведомления" in fin_pane
    assert 'id="my-cards-seg"' in fin_pane, "выбор срочности не на вкладке получателя"
    assert "⏰ Напоминания по умолчанию" not in fin_pane, (
        "общая карточка вернулась — расписание снова стало общим"
    )
    assert fin_pane.count('<div class="card"') == 1, "во вкладке лишние карточки"
    # Состав финансистов — вопрос прав, поэтому он во вкладке доступа.
    access_pane = HTML[HTML.index('id="pane-access"'):HTML.index('id="pane-data"')]
    assert "💼 Финансисты" in access_pane
    # Здоровье бота — своя вкладка: это не данные и не доступ, а эксплуатация,
    # и смотрят её в другой момент — когда что-то сломалось.
    health_pane = HTML[HTML.index('id="pane-health"'):HTML.index('id="pane-beta"')]
    assert "🛟 Здоровье бота" in health_pane
    # Плашка «технические работы» — тоже эксплуатация и смотрится в тот же
    # момент, что и здоровье: когда что-то идёт не так. Поэтому карточки две.
    assert "🛠 Технические работы" in health_pane
    assert len(re.findall(r'<div class="card[ "]', health_pane)) == 2, "лишние карточки"
    assert "💾 Бэкап" not in health_pane, "бэкап — на вкладке данных"


def test_tabs_fit_one_row():
    """Вкладки должны стоять в одну строку — без переноса и без прокрутки."""
    m = re.search(r"\n  \.tabs \{([^}]*)\}", HTML)
    assert m, "нет стилей строки вкладок"
    assert "flex-wrap: nowrap" in m.group(1)
    assert "overflow-x" not in m.group(1), "прокрутка вместо того, чтобы поместиться"
    # Кнопки не сжимаются — сжатая молча режет подпись.
    tab = re.search(r"\n  \.tab \{([^}]*)\}", HTML)
    assert tab and "flex: 0 0 auto" in tab.group(1)
    # На узких экранах ужимаем кегль и поля, иначе шесть вкладок не влезут.
    for width in (460, 400, 340):
        assert any(".tab {" in block for block in _media_blocks(f"max-width: {width}px")), width
    # Самая длинная подпись разворачивается только там, где есть место.
    assert '<span class="t-long">Оформление</span>' in HTML
    assert '<span\n            class="t-short">Тема</span>' in HTML
    assert "@media (min-width: 561px)" in HTML


def test_settings_are_open_to_everyone_but_admin_tabs_are_not():
    """Шестерёнка есть у всех — за ней «Оформление»; остальное по правам."""
    m = re.search(r'<button[^>]*id="admin-btn"[^>]*>', HTML, re.S)
    assert m and "hidden" not in re.search(r'class="([^"]*)"', m.group(0)).group(1)
    # Права раздаются по трём уровням, и уровень виден прямо в разметке.
    admin_tabs = re.findall(r'data-pane="(\w+)" data-admin="1"', HTML)
    assert admin_tabs == ["access", "health"], admin_tabs
    # Получателю — финансисту или админу: свои напоминания и реестр, который
    # он открывает каждый день. Полная админ-панель ему при этом не положена.
    recipient_tabs = re.findall(r'data-pane="(\w+)" data-recipient="1"', HTML)
    assert recipient_tabs == ["fin", "data"], recipient_tabs
    # Бэкап лежит на вкладке получателя, но это архив ВСЕХ данных с рассылкой
    # админам — карточка обязана оставаться админской.
    # ЛЮБОЙ data-admin обязан иметь hidden и в разметке: applyAdmin при старте
    # ничего не переключает (значение не изменилось), и без этого карточка
    # мелькнёт у того, кому не положена. На этом я уже попался дважды.
    for tag in re.findall(r'<(?:div|button)[^>]*data-admin="1"[^>]*>', MARKUP):
        classes = re.search(r'class="([^"]*)"', tag)
        assert classes and "hidden" in classes.group(1), tag
    # Бета открыта всем: личный выключатель чтения счёта.
    assert 'data-pane="beta"' in HTML and 'data-pane="beta" data-admin' not in HTML
    assert 'id="my-autofill-seg"' in HTML
    # Напоминания настраивает получатель — финансист тоже, не только админ.
    assert 'data-pane="fin" data-recipient="1"' in MARKUP
    assert "function applyRecipientTab(" in JS
    assert '#admin-view [data-admin]' in JS, "видимость админского не управляется"
    # Настройки чужих заявок не тянем тому, кому сервер их всё равно не отдаст.
    opener = HTML[HTML.index("function openAdmin("):]
    assert "if (isBotAdmin) { loadAdminSettings(); loadUsers(); }" in opener[:900]


def test_article_hints_scroll_below_the_field():
    """Статьи расходов — такие же подсказки, как у контрагента: строкой под полем.

    Сетка с переносом занимала пол-экрана; теперь один ряд, который листается
    тем же механизмом, что и чипсы.
    """
    assert '<div class="seg seg-scroll" id="article-seg">' in HTML
    # Поле ввода стоит ВЫШЕ подсказок — как в карточке контрагента.
    card = HTML[HTML.index("📂 Статья расходов"):]
    card = card[:card.index("</div>\n\n  <div class=\"card\">")]
    assert card.index('id="article-custom"') < card.index('id="article-seg"')
    # Два класса в селекторе: правила .seg идут ниже и иначе перебьют перенос.
    m = re.search(r"\.seg\.seg-scroll \{([^}]*)\}", HTML)
    assert m, "нет стилей листающегося ряда статей"
    assert "flex-wrap: nowrap" in m.group(1) and "overflow-x: auto" in m.group(1)
    button = re.search(r"\.seg\.seg-scroll button \{([^}]*)\}", HTML)
    assert button and "flex: 0 0 auto" in button.group(1)
    # Размер — как у подсказок контрагента: один вид у обоих списков.
    chip = re.search(r"\n  \.chip \{([^}]*)\}", HTML).group(1)
    for prop in ("padding: 7px 12px", "border-radius: 999px", "font-size: 13px"):
        assert prop in chip, f"изменился эталон чипса: нет «{prop}»"
        assert prop in button.group(1), f"ряд статей разошёлся с чипсами: нет «{prop}»"
    assert "wireHScroll(artSeg);" in HTML
    # Выбранная статья могла остаться за обрезом — её подтягивают в кадр.
    assert "function revealActive(" in HTML
    assert "revealActive(box);" in HTML       # из setSeg: черновик и повтор
    assert "revealActive(artSeg);" in HTML    # из перерисовки списка


def test_scrollbar_is_hidden_on_the_row_itself():
    """Регрессия: у статей расходов не появлялась полоса прокрутки.

    Пряталась она правилами .chips и .seg.seg-scroll с разной
    специфичностью, и десктопное правило .hscroll перебивалось вторым.
    Теперь и прячет, и возвращает полосу один и тот же селектор.
    """
    assert ".hscroll { scrollbar-width: none; }" in HTML
    assert ".hscroll::-webkit-scrollbar { display: none; }" in HTML
    for gone in (".chips::-webkit-scrollbar", ".seg.seg-scroll::-webkit-scrollbar"):
        assert gone not in HTML, f"{gone} снова перебьёт правило для десктопа"
    fine = _media_blocks("pointer: fine")
    assert fine and ".hscroll::-webkit-scrollbar { display: block;" in fine[0]


def test_hint_crosses_highlight_the_same_way():
    """Крестик под курсором краснеет одинаково у контрагента и у статьи."""
    hover = [b for b in _media_blocks("hover: hover") if ".chip-x:hover" in b]
    assert hover, "подсветка крестиков вне @media (hover: hover) — залипнет на тач-экране"
    rule = re.search(
        r"\.chip-wrap \.chip-x:hover, \.seg button \.seg-x:hover \{([^}]*)\}", hover[0])
    assert rule, "крестики подсвечиваются разными правилами"
    assert "var(--danger)" in rule.group(1)
    # Правило пилюли (акцент на всю подсказку) идёт выше и при равной
    # специфичности перебивало бы красный — красное должно быть длиннее и позже.
    block = hover[0]
    assert block.index(".chip-wrap:hover .chip,") < block.index(".chip-wrap .chip-x:hover"), (
        "красное правило стоит раньше правила пилюли и будет перебито"
    )
    # Отдельного правила для .seg-x уже быть не должно — иначе разъедутся.
    assert HTML.count(".seg-x:hover") == 1


def test_access_tab_lists_who_uses_the_bot():
    """В «Доступе» виден список пользователей с ролями и отзывом доступа."""
    access = HTML[HTML.index('id="pane-access"'):HTML.index('id="pane-data"')]
    assert 'id="users-list"' in access
    assert 'id="users-reload"' in access
    assert "function loadUsers(" in HTML
    assert "function renderUsers(" in HTML
    assert '"/api/admin/users"' in HTML
    assert 'it.access === "env" || it.admin_source === "env"' in HTML
    # Карточка — только обзор: отзыв доступа живёт в «Доступе к подаче», и
    # две кнопки на одно действие в соседних блоках только путали.
    body = HTML[HTML.index("function userRow("):]
    body = body[:body.index("\n  }")]
    assert "row-del" not in body, "в обзоре снова появилось действие"
    assert "adminAction" not in body
    # Список обновляется после любой правки доступа.
    action = HTML[HTML.index("function adminAction("):]
    assert "loadUsers();" in action[:1200]


def test_env_admin_has_no_remove_button():
    """Админа из .env панель не разжалует — кнопки у него нет."""
    assert 'if (kind !== "adm" || it.source !== "env") {' in HTML


def test_user_list_is_grouped_by_role():
    """Роли вперемешку не читались — список разбит на секции."""
    groups = re.findall(r'\{ key: "(\w+)", title: "([^"]+)"', HTML)
    assert [g[0] for g in groups] == ["admin", "financier", "other"], groups
    assert "function userRow(" in HTML
    assert ".group-head {" in HTML
    # Финансист-админ не должен попасть в две секции сразу.
    assert "(it.financier && !it.admin)" in HTML
    # «Может подавать» не отвечало на вопрос «что?».
    assert '"может подавать заявки"' in HTML
    assert '"не может подавать заявки"' in HTML
    assert '"подаёт заявки как админ"' in HTML
    # Общее «whitelist пуст» стояло в КАЖДОЙ строке и читалось как личный
    # статус человека; сам факт виден в карточке доступа выше.
    assert '"подача закрыта всем — whitelist пуст"' not in JS


def test_admins_can_be_managed_from_the_access_tab():
    """Состав админов правится там же, где доступ, — своим списком."""
    access = HTML[HTML.index('id="pane-access"'):HTML.index('id="pane-data"')]
    for element in ('id="adm-list"', 'id="adm-input"', 'id="adm-add"'):
        assert element in access, element
    assert 'kind === "adm" ? "/api/admin/admins"' in HTML
    assert 'renderList("adm-list", d.admins || [], "adm");' in HTML
    assert 'bindAdd("adm-add", "adm-input", "adm");' in HTML


def test_header_icons_are_centred_by_layout_not_script():
    """Кнопки держит панель, а не пересчёт координат из скрипта.

    Раньше каждая кнопка ставилась по right/top вручную, и от скрытых
    оставались дыры. Панель на flex делает это сама — если правила вернуть
    в скрипт, порядок снова начнёт зависеть от того, кто когда скрылся.
    """
    assert ".gears {" in HTML and "display: flex" in HTML
    body = HTML[HTML.index("function layoutHeaderIcons("):]
    body = body[:body.index("\n  }")]
    assert "style.right" not in body, "кнопки снова расставляются по одной"
    # Вертикаль по-прежнему меряется скриптом, но у ПАНЕЛИ, а не у каждой
    # кнопки: из CSS высоту строки с маркой не узнать.
    assert "panel.style.top" in body, "панель не центрируется по строке с маркой"
    assert body.index("icons-") < body.index("panel.style.top"), (
        "вертикаль считается до того, как класс плотности ужал марку"
    )


def test_hint_hover_is_identical_everywhere():
    """Регрессия: чипс контрагента обводился акцентом, кнопка статьи — нет.

    Крестик — часть той же пилюли, поэтому подсвечивается вместе с ней,
    иначе контур обрывался на середине.
    """
    hover = [b for b in _media_blocks("hover: hover") if ".chip-wrap:hover" in b]
    assert hover, "подсветка подсказок вне @media (hover: hover)"
    rule = re.search(
        r"\.chip-wrap:hover \.chip, \.chip-wrap:hover \.chip-x,\s*"
        r"\.seg button:hover:not\(\.active\) \{([^}]*)\}", hover[0])
    assert rule, "подсказки подсвечиваются разными правилами"
    assert "border-color: var(--accent)" in rule.group(1)
    # Отдельного правила для чипса быть не должно — иначе снова разъедутся.
    assert ".chip:hover {" not in HTML
    # Правая граница чипса разрывала бы общий контур пилюли.
    assert "border-right: none" in re.search(r"\.chip-wrap \.chip \{([^}]*)\}", HTML).group(1)


def test_access_can_be_requested_from_the_form():
    """Отказ по whitelist — не тупик: есть кнопка попросить доступ."""
    assert 'id="access-gate"' in HTML
    assert 'id="access-ask"' in HTML
    assert "function checkAccess(" in HTML
    assert '"/api/access"' in HTML
    assert '"/api/access/request"' in HTML
    # Повторно просить нечего — кнопка прячется, пока заявка висит.
    assert "function markAccessPending(" in HTML
    assert "d.pending" in HTML
    # Без админов просьбу некому рассмотреть — говорим прямо, а не молчим.
    assert "d.has_admins" in HTML


def test_chip_halves_animate_together():
    """Регрессия: при наведении пилюля подсказки распадалась на две части.

    У крестика не было перехода border-color — его рамка вспыхивала сразу,
    а у текста проявлялась за .15s, и на это время шов был виден.
    """
    chip = re.search(r"\n  \.chip \{([^}]*)\}", HTML).group(1)
    cross = re.search(r"\n  \.chip-x \{([^}]*)\}", HTML).group(1)
    for part in ("background .16s", "border-color .16s", "color .16s"):
        assert part in chip, f"у чипса нет перехода {part}"
        assert part in cross, f"у крестика нет перехода {part}"


def test_username_inputs_suggest_known_people():
    """Ввод @username подсказывает из тех, кого бот уже знает.

    Bot API не умеет проверять существование произвольного @username, зато
    справочник содержит ровно тех, кого сервер сможет резолвить в id, —
    подсказки совпадают с тем, что реально примут.
    """
    for who in ("fin", "wl", "adm"):
        assert f'id="{who}-suggest"' in HTML, who
    assert "function wireSuggest(" in HTML
    assert 'wireSuggest(who + "-input", who + "-suggest");' in HTML
    # Список берём из уже загруженного реестра пользователей — без второй ручки.
    assert "knownUsers = items || [];" in HTML
    # mousedown, а не click: blur поля успевал закрыть список до выбора.
    body = HTML[HTML.index("function wireSuggest("):]
    assert 'addEventListener("mousedown"' in body[:2200]


def test_mini_app_is_split_into_files():
    """Разметка, стили и логика — в разных файлах, и все подключены.

    Единый index.html на четыре тысячи строк был главным источником правок
    вслепую: конфликты специфичности CSS находились только глазами.
    """
    assert '<link rel="stylesheet" href="app.css">' in MARKUP
    assert '<script src="form-lib.js"></script>' in MARKUP
    assert '<script src="skin-field.js"></script>' in MARKUP
    assert '<script src="alerts-panel.js"></script>' in MARKUP
    assert '<script src="app.js"></script>' in MARKUP
    # В разметке не должно остаться ни стилей, ни логики.
    assert "<style>" not in MARKUP
    assert MARKUP.count("<script>") == 1, "инлайн-скрипт только один — ранний выбор темы"
    assert "data-skin" in MARKUP, "ранний скрипт темы обязан остаться инлайном"
    # Порядок подключения значим: app.js зовёт buildSkinField и функции формы.
    assert MARKUP.index("skin-field.js") < MARKUP.index('src="app.js"')
    assert MARKUP.index("alerts-panel.js") < MARKUP.index('src="app.js"')
    assert MARKUP.index("restore-panel.js") < MARKUP.index('src="app.js"')
    assert MARKUP.index("reminders-panel.js") < MARKUP.index('src="app.js"')
    assert MARKUP.index("maint-panel.js") < MARKUP.index('src="app.js"')
    assert MARKUP.index("closing-panel.js") < MARKUP.index('src="app.js"')
    assert "function closingButton(" in _read("closing-panel.js")
    assert "closingButton(actions" in JS
    assert "buildMaintPanel(" in _read("maint-panel.js")
    assert "buildMaintPanel({" in JS
    assert "buildRemindersPanel(" in _read("reminders-panel.js")
    assert "buildRemindersPanel({" in JS
    assert "buildRestorePanel(" in _read("restore-panel.js")
    assert "buildRestorePanel({" in JS
    assert "buildSkinField(" in FIELD
    assert 'buildSkinField($("skin-field"))' in JS
    assert "buildAlertsPanel(" in _read("alerts-panel.js")
    assert "buildAlertsPanel({" in JS


def test_split_files_stay_reasonably_small():
    """Порог, чтобы файлы снова не срослись в одну простыню.

    app.js поднят с 2800 до 2900 ОСОЗНАННО и в долг. Чистые функции из него
    уже вынесены в form-lib.js; всё остальное трогает DOM, и тащить это в
    файл, заявленный как «чистые функции без DOM», значит сломать его смысл
    ради числа. Настоящее решение — вынести панели (финансиста, админа,
    «Мои заявки») в свой файл; это отдельная задача, а не правка порога.

    alerts-panel.js поднят с 300 до 330 (25.08.2026): журнал инцидентов
    научился раскрываться и показывать, что именно записано о сбое. Это не
    «файл распух», а панель обзавелась содержанием, ради которого её и
    выносили в отдельный файл. Следующий раз, когда упрётся, — делить по
    смыслу: настройки категорий отдельно от журнала.

    app.js поднят с 2900 до 2950 (27.08.2026) под кнопку «Закрывающие
    документы». Правильный шаг — вынести «Мои заявки»: это ~360 строк и
    ровно тот блок, где кнопка живёт. Не сделано СЕЙЧАС осознанно: блок
    держит STATUS_CLASS/STATUS_ICON, которыми пользуется модалка подробностей
    ниже, тянет isBotAdmin, applyRepeat и askConfirm, — и ломать самый
    ходовой экран ради пятидесяти строк запаса неправильно. Это следующая
    задача по интерфейсу, до неё порог больше не поднимать.

    index.html поднят с 990 до 1050 (27.08.2026): добавился экран «Аналитика».
    Это ЭКРАН, а не поле формы: разметки в нём мало (шапка, период, два
    контейнера), всё остальное строит stats-panel.js.

    index.html поднят с 960 до 990 (27.08.2026): экран «Инструкция» догнал
    то, что за день изменилось, — необязательные счёт и реквизиты,
    дополнительные документы, где теперь ставится статус, кнопка обновления
    списка. Это ТЕКСТ инструкции, а не разметка формы: расти ему тут и
    некуда больше, пока нет шага сборки.

    index.html поднят с 900 до 960 (26.08.2026): добавилось поле
    «Дополнительные документы». Здесь лекарство «поделить» не работает так,
    как с JS: это ОДИН документ, и вынести из него кусок можно только
    сборкой шаблонов — то есть тем самым шагом сборки, который уже числится
    в R3 отчёта 001. Пока его нет, порог растёт вместе с формой, и это
    осознанно.

    list-tools.js поднят со 100 до 160 (27.08.2026): туда переехало то, что
    рисует строку списка, — марка «счёт / реквизиты / ни того ни другого»
    и строка дат. Это разгрузка app.js, а не рост: сам app.js за ту же
    правку похудел на 14 строк, а обе функции нужны ОБОИМ спискам, и
    держать их в app.js значило бы снова его пухнуть.

    app.css поднят с 1600 до 1650 (26.08.2026) — третья правка подряд, где
    места не было. Делить его страшнее, чем app.js: в шапке файла записано,
    что порядок правил значим (скиновые переопределения после базовых, блок
    наведения последним), и вынос куска в отдельный файл сдвигает его в
    конец каскада.

    27.08.2026 долг начали отдавать: модальное окно уехало в modal.css
    (66 строк), и app.css вернулся к 1611 при том же пороге. Окно выбрано
    первым не случайно — скиновых переопределений у него нет, поэтому конец
    каскада ему безразличен. Порог 1650 оставлен как есть: следующий кусок
    (панель финансиста) должен уменьшить файл, а не занять освободившееся.
    """
    for name, limit in (("index.html", 1050), ("app.css", 1650), ("app.js", 2950),
                        ("modal.css", 120),
                        ("skin-field.js", 400), ("form-lib.js", 300),
                        ("alerts-panel.js", 330), ("restore-panel.js", 200),
                        ("reminders-panel.js", 200),
                        ("maint-panel.js", 150),
                        ("closing-panel.js", 150),
                        ("nudge-panel.js", 100),
                        ("list-tools.js", 160),
                        ("help-tour.js", 220), ("tour.css", 240),
                        ("stats-panel.js", 240), ("stats.css", 120)):
        length = len(_read(name).split("\n"))
        assert length <= limit, f"{name}: {length} строк — пора делить дальше"


def test_form_is_hidden_without_access_and_revives_by_itself():
    """Без доступа форма скрыта, а решение админа не требует перезапуска.

    Раньше поля были видны всегда, а после выдачи доступа приложение надо
    было закрыть и открыть заново — иначе оно оставалось в отказе.
    """
    assert re.search(r"#form-view\.no-access \.card[^}]*display: none", CSS, re.S)
    assert "function applyAccess(" in JS
    # Опрос идёт в ОБЕ стороны и не останавливается: права снимают так же
    # буднично, как выдают, и человек должен это увидеть сразу.
    assert "function watchAccess(" in JS and "function pollAccess(" in JS
    assert "stopAccessWatch" not in JS, "опрос снова выключается при доступе"
    # На телефоне приложение уходит в фон, а на десктопе Mini App — окно:
    # оно остаётся видимым и только теряет фокус. Нужны оба события.
    assert 'document.addEventListener("visibilitychange", refreshOnReturn);' in JS
    assert 'window.addEventListener("focus", refreshOnReturn);' in JS
    # Вернувшись из чата, админ должен видеть свежие списки.
    ret = JS[JS.index("function refreshOnReturn("):]
    ret = ret[:ret.index("\n  }")]
    assert "loadAdminSettings();" in ret and "loadUsers();" in ret
    # И не затирать поле, которое правят прямо сейчас.
    assert "document.activeElement" in ret
    # Свёрнутое приложение не опрашиваем.
    watch = JS[JS.index("function watchAccess("):]
    assert "document.hidden" in watch[:600]
    # Кнопка отправки без доступа не должна быть видна.
    assert "if (canSubmit === false) { tg.MainButton.hide(); return; }" in JS


def test_mark_takes_the_theme_accent():
    """Козырёк Оплатыча красится акцентом темы: в телеграмной шкуре — синий.

    Ради этого марка лежит инлайном: внешний svg внутри <img> к переменным
    страницы доступа не имеет, и цвет остался бы зашитым.
    """
    mark = MARKUP[MARKUP.index('<svg id="brand-mark"'):]
    mark = mark[:mark.index("</svg>")]
    assert "var(--accent)" in mark, "марка не берёт цвет из темы"
    assert "#10B981" not in mark, "изумруд снова зашит в разметку"
    # Градиенты — в общем блоке вне #form-view: из скрытого предка браузер
    # заливки не отдаёт, и клон в пустом списке терял и шерсть, и бумагу.
    assert '<svg class="svg-defs"' in MARKUP
    assert MARKUP.index('class="svg-defs"') < MARKUP.index('id="form-view"')
    for gradient in ("mk-band", "mk-brim", "mk-fur", "mk-paper"):
        assert f'id="{gradient}"' in MARKUP, gradient
    assert ".svg-defs {" in CSS
