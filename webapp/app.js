/*
 * app.js — логика Mini App: форма заявки, «Мои заявки», панель финансиста,
 * настройки и оформление. Один IIFE: наружу ничего не торчит.
 *
 * Чистые функции формы — в form-lib.js, фон — в skin-field.js; оба
 * подключаются раньше.
 */
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var insideTelegram = !!(tg && tg.initData);

  // Держать в синхроне с bot/models.py (CURRENCIES, ARTICLES) и bot/validators.py.
  var CURRENCIES = ["RUB", "USD", "EUR", "KZT", "CNY"];
  var ARTICLES = [
    "Аренда", "Закупка товаров", "Услуги подрядчиков",
    "Хостинг и ПО", "Командировки", "Реклама и маркетинг", "Прочее"
  ];
  var MAX_FILE_SIZE = 20 * 1024 * 1024;
  var ALLOWED_EXT = ["pdf", "jpg", "jpeg", "png", "xlsx", "xls"];

  var state = {
    currency: "RUB",
    article: "",
    urgency: "NORMAL",
    hasInvoice: true,
    file: null,
    submitting: false,
    dirty: false
  };

  var $ = function (id) { return document.getElementById(id); };

  // --- Инициализация Telegram ----------------------------------------------
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("secondary_bg_color"); } catch (e) { /* старые клиенты */ }
    // Тема для фоновых «пятен»: в тёмной теме им нужна чуть большая яркость.
    var applyTheme = function () {
      document.body.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
    };
    applyTheme();
    try { tg.onEvent("themeChanged", applyTheme); } catch (e) { /* старые клиенты */ }
  }
  if (!insideTelegram) {
    $("fallback-note").style.display = "block";
    $("submit-fallback").style.display = "block"; // виден, но отправка вернёт 401
  }

  // Флаг «в форме есть ввод». Подтверждение закрытия у Telegram НЕ включаем:
  // оно рисуется в главном окне мессенджера (на десктопе — вообще на другом
  // мониторе), а смысла в нём мало — черновик и так сохраняется в
  // localStorage и восстанавливается при следующем открытии формы.
  function markDirty() {
    state.dirty = true;
  }

  // --- Сегментированные контролы -------------------------------------------
  function bindSeg(containerId, onChange) {
    var seg = $(containerId);
    seg.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn || btn.classList.contains("active")) return;
      seg.querySelectorAll("button").forEach(function (b) {
        b.classList.remove("active", "just-picked");
      });
      btn.classList.add("active", "just-picked");
      btn.addEventListener("animationend", function h() {
        btn.classList.remove("just-picked");
        btn.removeEventListener("animationend", h);
      });
      onChange(btn.dataset.value);
      markDirty();
      saveDraftSoon();
      if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
      refreshMainButton();
    });
  }

  var curSeg = $("currency-seg");
  CURRENCIES.forEach(function (code, i) {
    var b = document.createElement("button");
    b.type = "button";
    b.textContent = code;
    b.dataset.value = code;
    if (i === 0) b.classList.add("active");
    curSeg.appendChild(b);
  });
  bindSeg("currency-seg", function (v) { state.currency = v; });

  // Статьи расходов: чипс ИЛИ своё значение в текстовом поле.
  var artSeg = $("article-seg");
  // Скрытые подсказки живут на устройстве. Возвращаются сами, когда статьёй
  // или контрагентом снова пользуются (см. unhideUsed при отправке заявки), —
  // отдельной кнопки «вернуть» нет намеренно.
  var HIDDEN_ART_KEY = "invoice_hidden_art_v1";

  /** Горизонтальный список (подсказки, вкладки) прокручивается пальцем, но
   *  на десктопе прокрутить его было нечем: полоса спрятана ради вида, а
   *  вертикальное колесо горизонталь не крутит. Разворачиваем колесо вбок
   *  и подсвечиваем края, чтобы было видно продолжение. */
  function wireHScroll(box) {
    if (!box || box._hscroll) return;
    box._hscroll = true;
    box.classList.add("hscroll");
    box.addEventListener("wheel", function (ev) {
      if (box.scrollWidth <= box.clientWidth + 1) return;
      var d = Math.abs(ev.deltaX) > Math.abs(ev.deltaY) ? ev.deltaX : ev.deltaY;
      if (!d) return;
      ev.preventDefault();
      box.scrollLeft += d;
    }, { passive: false });
    box.addEventListener("scroll", function () { updateFades(box); });
    updateFades(box);
  }

  function updateFades(box) {
    if (!box) return;
    var max = box.scrollWidth - box.clientWidth;
    box.classList.toggle("fade-l", box.scrollLeft > 4);
    box.classList.toggle("fade-r", max > 4 && box.scrollLeft < max - 4);
  }

  window.addEventListener("resize", function () {
    [].forEach.call(document.querySelectorAll(".hscroll"), updateFades);
  });

  function readHidden(key) {
    try { return JSON.parse(localStorage.getItem(key) || "[]"); }
    catch (e) { return []; }
  }
  function writeHidden(key, list) {
    try { localStorage.setItem(key, JSON.stringify(list)); }
    catch (e) { /* localStorage может быть недоступен */ }
  }
  function hideValue(key, value, redraw) {
    var list = readHidden(key);
    if (list.indexOf(value) === -1) list.push(value);
    writeHidden(key, list);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    redraw();
  }

  /** Крестик внутри кнопки: вложенная кнопка недопустима, поэтому span. */
  function hideMark(label, onHide) {
    var x = document.createElement("span");
    x.className = "seg-x";
    x.setAttribute("role", "button");
    x.setAttribute("aria-label", "Убрать подсказку «" + label + "»");
    x.title = "Убрать подсказку";
    x.textContent = "✕";
    x.addEventListener("click", function (ev) {
      ev.stopPropagation();
      onHide();
    });
    return x;
  }

  function renderArticles() {
    var hidden = readHidden(HIDDEN_ART_KEY);
    artSeg.innerHTML = "";
    ARTICLES.filter(function (name) {
      return hidden.indexOf(name) === -1;
    }).forEach(function (name) {
      var b = document.createElement("button");
      b.type = "button";
      b.dataset.value = name;
      b.classList.toggle("active", state.article === name);
      b.appendChild(document.createTextNode(name));
      b.appendChild(hideMark(name, function () {
        // Скрыли выбранную — выбор сбрасываем, иначе он остался бы невидимым.
        if (state.article === name) state.article = "";
        hideValue(HIDDEN_ART_KEY, name, renderArticles);
      }));
      artSeg.appendChild(b);
    });
    wireHScroll(artSeg);
    revealActive(artSeg);
  }
  renderArticles();

  bindSeg("article-seg", function (v) {
    state.article = v;
    $("article-custom").value = "";
  });
  $("article-custom").addEventListener("input", function () {
    if (this.value.trim()) {
      state.article = "";
      artSeg.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
    }
    this.classList.remove("invalid");
    markDirty();
    saveDraftSoon();
    refreshMainButton();
  });
  function currentArticle() {
    var custom = $("article-custom").value.trim();
    return custom || state.article;
  }

  // Дата оплаты: «Срочно» → сегодня, «Обычная» → завтра,
  // «Настраиваемая» → выбор в календаре (не раньше сегодня).
  var plannedEl = $("planned");
  // isoOf/todayISO/nextBusinessISO/fmtRu — в form-lib.js
  function resolvedPlannedDate() {
    // Для авто-режимов дату считает СЕРВЕР (в таймзоне приложения) —
    // здесь только маркер; даты ниже в плашке — предпросмотр.
    return state.urgency === "CUSTOM" ? plannedEl.value : "auto";
  }
  function refreshPayDate() {
    var note = $("pay-date-note");
    var custom = state.urgency === "CUSTOM";
    $("planned-block").classList.toggle("hidden", !custom);
    if (custom) {
      note.textContent = "📅 Выберите дату оплаты в календаре";
    } else if (state.urgency === "URGENT") {
      note.textContent = "📅 Оплата: сегодня, " + fmtRu(todayISO());
    } else {
      note.textContent = "📅 Оплата: следующий рабочий день, " + fmtRu(nextBusinessISO());
    }
  }
  plannedEl.min = todayISO();
  plannedEl.addEventListener("change", function () {
    this.classList.remove("invalid");
    markDirty();
    saveDraftSoon();
    refreshMainButton();
  });

  bindSeg("urgency-seg", function (v) {
    state.urgency = v;
    refreshPayDate();
  });
  refreshPayDate();
  bindSeg("invoice-seg", function (v) {
    state.hasInvoice = v === "1";
    $("file-block").classList.toggle("hidden", !state.hasInvoice);
    $("requisites-block").classList.toggle("hidden", state.hasInvoice);
    if (!state.hasInvoice) hideFileWarn();
  });

  // --- Поля ------------------------------------------------------------------
  var amountEl = $("amount"), cpEl = $("counterparty"), commentEl = $("comment"),
      reqEl = $("requisites");

  amountEl.addEventListener("input", function () {
    // Только цифры, пробелы и разделители.
    this.value = this.value.replace(/[^\d\s.,]/g, "");
    this.classList.remove("invalid");
    markDirty();
    refreshMainButton();
  });
  amountEl.addEventListener("blur", function () {
    var v = parseAmount(this.value);
    if (v !== null) this.value = formatAmount(v);
  });
  [cpEl, commentEl, reqEl].forEach(function (el) {
    el.addEventListener("input", function () {
      el.classList.remove("invalid");
      markDirty();
      saveDraftSoon();
      refreshMainButton();
    });
  });
  amountEl.addEventListener("input", saveDraftSoon);

  // --- Чипсы частых контрагентов -----------------------------------------------
  // Вместе с именем подставляются последние известные реквизиты этого
  // контрагента — их не приходится набирать заново (источник опечаток).
  // Скрытые подсказки — на этом устройстве. Заявки в реестре не трогаем:
  // «подсказка больше не нужна» ≠ «удалить историю платежей».
  var HIDDEN_CP_KEY = "invoice_hidden_cp_v1";

  /** Использовали скрытую статью или контрагента — подсказка возвращается.
   *  Поэтому кнопки «вернуть скрытые» нет: список чинит себя сам. */
  function unhideUsed(counterparty, article) {
    [[HIDDEN_CP_KEY, counterparty], [HIDDEN_ART_KEY, article]].forEach(function (pair) {
      var key = pair[0], value = (pair[1] || "").trim();
      if (!value) return;
      var list = readHidden(key);
      var at = list.indexOf(value);
      if (at !== -1) {
        list.splice(at, 1);
        writeHidden(key, list);
      }
    });
  }

  /** Статья из черновика или повтора не должна остаться невидимой. */
  function ensureArticleVisible(name) {
    if (!name) return;
    var list = readHidden(HIDDEN_ART_KEY);
    var at = list.indexOf(name);
    if (at === -1) return;
    list.splice(at, 1);
    writeHidden(HIDDEN_ART_KEY, list);
    renderArticles();
  }

  function loadCounterparties() {
    if (!insideTelegram) return;
    fetch("/api/counterparties", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (d) {
        var all = (d && d.items) || [];
        var hidden = readHidden(HIDDEN_CP_KEY);
        var items = all.filter(function (it) {
          return it && it.name && hidden.indexOf(it.name) === -1;
        });
        var box = $("cp-chips");
        box.innerHTML = "";
        box.classList.toggle("hidden", !items.length && !hidden.length);
        if (!items.length && !hidden.length) return;
        items.forEach(function (it) {
          var name = it.name;
          var wrap = document.createElement("span");
          wrap.className = "chip-wrap";
          var c = document.createElement("button");
          c.type = "button";
          c.className = "chip";
          c.textContent = name;
          c.addEventListener("click", function () {
            cpEl.value = name;
            cpEl.classList.remove("invalid");
            // Реквизиты подставляем, только если человек ещё ничего не ввёл:
            // затирать набранное вручную нельзя.
            if (it.requisites && !reqEl.value.trim()) {
              reqEl.value = it.requisites;
              $("req-count").textContent = reqEl.value.length;
              setVeil(true);
              checkReqNow();
            }
            markDirty();
            saveDraftSoon();
            if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
            refreshMainButton();
          });

          var x = document.createElement("button");
          x.type = "button";
          x.className = "chip-x";
          x.setAttribute("aria-label", "Убрать подсказку «" + name + "»");
          x.title = "Убрать подсказку";
          x.textContent = "✕";
          x.addEventListener("click", function (ev) {
            ev.stopPropagation();
            hideValue(HIDDEN_CP_KEY, name, loadCounterparties);
          });

          wrap.appendChild(c);
          wrap.appendChild(x);
          box.appendChild(wrap);
        });
        box.classList.toggle("hidden", !items.length);
        wireHScroll(box);
        updateFades(box);
      })
      .catch(function () { /* подсказки — не критичны */ });
  }
  loadCounterparties();

  function bindCounter(el, counterId, limit) {
    el.addEventListener("input", function () {
      var c = $(counterId);
      c.textContent = el.value.length;
      c.parentNode.classList.toggle("warn", el.value.length > limit * 0.9);
    });
  }
  bindCounter(commentEl, "comment-count", 500);
  bindCounter(reqEl, "req-count", 1500);

  // innValid/bikValid/keyCheck/checkRequisites — в form-lib.js

  // Глазик: введённые реквизиты скрыты ПО УМОЛЧАНИЮ (как спойлер).
  // Фокус в поле открывает для редактирования, уход из поля — прячет снова.
  var reqVeiled = false;
  function setVeil(on) {
    reqVeiled = on;
    reqEl.classList.toggle("veiled", on);
    $("req-eye").textContent = on ? "🙈" : "👁️";
  }
  $("req-eye").addEventListener("click", function () {
    setVeil(!reqVeiled);
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  });
  reqEl.addEventListener("focus", function () { setVeil(false); });
  reqEl.addEventListener("blur", function () {
    if (reqEl.value.trim()) setVeil(true);
  });

  // --- Своё модальное окно ------------------------------------------------------
  // Нативный tg.showPopup рисуется в главном окне мессенджера, а Mini App на
  // десктопе открыт отдельным окном — подтверждение уезжало «на другой экран».
  var modalEl = $("modal");

  function closeModal() {
    modalEl.classList.remove("shown");
  }

  /** content — строка или готовый DOM-узел (подробности заявки).
   *  buttons: [{text, style: "primary"|"ghost"|"danger", onClick}] */
  function showModal(title, content, buttons) {
    $("modal-title").textContent = title;
    var textBox = $("modal-text");
    textBox.innerHTML = "";
    if (typeof content === "string") textBox.textContent = content;
    else if (content) textBox.appendChild(content);
    var box = $("modal-actions");
    box.innerHTML = "";
    (buttons || [{ text: "Понятно" }]).forEach(function (b) {
      var el = document.createElement("button");
      el.type = "button";
      el.className = "add-btn" +
        (b.style === "ghost" ? " btn-ghost" : b.style === "danger" ? " btn-danger" : "");
      el.textContent = b.text;
      el.addEventListener("click", function () {
        closeModal();
        if (b.onClick) b.onClick();
      });
      box.appendChild(el);
    });
    modalEl.classList.add("shown");
    var first = box.querySelector("button");
    if (first) first.focus();
  }

  /** Окно с полем ввода: onSubmit получает введённый текст (может быть пустым). */
  function showPrompt(title, message, placeholder, submitText, onSubmit) {
    var box = document.createElement("div");
    var hint = document.createElement("div");
    hint.className = "modal-text";
    hint.textContent = message;
    var input = document.createElement("textarea");
    input.rows = 3;
    input.maxLength = 300;
    input.placeholder = placeholder;
    input.style.marginTop = "12px";
    box.appendChild(hint);
    box.appendChild(input);
    showModal(title, box, [
      { text: "Отмена", style: "ghost" },
      { text: submitText, onClick: function () { onSubmit(input.value.trim()); } }
    ]);
    input.focus();
  }

  /** Вопрос «да/нет»: onYes вызывается только при подтверждении. */
  function askConfirm(title, message, yesText, onYes, danger) {
    showModal(title, message, [
      { text: "Отмена", style: "ghost" },
      { text: yesText, style: danger ? "danger" : "primary", onClick: onYes }
    ]);
  }

  // Клик по затемнению и Esc закрывают окно.
  modalEl.addEventListener("click", function (ev) {
    if (ev.target === modalEl) closeModal();
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && modalEl.classList.contains("shown")) closeModal();
  });

  // Подсказки «?» у настроек — в том же окне.
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest(".help-q");
    if (!btn) return;
    showModal(btn.dataset.helpTitle || "Подсказка", btn.dataset.help || "");
  });

  var reqCheckTimer = null;
  // Вызывается и по вводу (с задержкой), и при подстановке реквизитов
  // чипсом контрагента или повтором заявки — проверка одна и та же.
  function checkReqNow() {
    var warns = checkRequisites(reqEl.value);
    var box = $("req-warnings");
    if (warns.length) {
      box.textContent = "⚠️ " + warns.join("\n⚠️ ");
      box.classList.remove("hidden");
    } else {
      box.classList.add("hidden");
    }
  }
  reqEl.addEventListener("input", function () {
    clearTimeout(reqCheckTimer);
    reqCheckTimer = setTimeout(checkReqNow, 400);
  });

  // --- Файл -------------------------------------------------------------------
  var dropZone = $("drop-zone"), fileInput = $("file-input");
  dropZone.addEventListener("click", function () { fileInput.click(); });

  function clearFile() {
    state.file = null;
    fileInput.value = "";
    hideFileWarn();
    showAutofill(null);
    dropZone.classList.remove("has-file");
    $("drop-icon").textContent = "📄";
    $("drop-text").textContent = "Нажмите, чтобы выбрать файл";
    $("drop-hint").textContent = "PDF, JPG, PNG, XLSX · до 20 МБ";
  }

  $("file-remove").addEventListener("click", function (ev) {
    ev.stopPropagation();
    clearFile();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    refreshMainButton();
  });

  /** Полный сброс формы к исходному виду + удаление черновика. */
  function resetForm() {
    clearDraft();
    clearFile();

    amountEl.value = "";
    cpEl.value = "";
    commentEl.value = "";
    reqEl.value = "";
    $("article-custom").value = "";
    plannedEl.value = "";
    $("comment-count").textContent = "0";
    $("req-count").textContent = "0";
    $("req-warnings").classList.add("hidden");
    [amountEl, cpEl, reqEl].forEach(function (el) { el.classList.remove("invalid"); });

    state.currency = CURRENCIES[0];
    state.article = "";
    state.urgency = "NORMAL";
    state.hasInvoice = true;
    state.dirty = false;
    setSeg("currency-seg", state.currency);
    setSeg("article-seg", "");
    setSeg("urgency-seg", "NORMAL");
    setSeg("invoice-seg", "1");
    $("planned-block").classList.add("hidden");
    $("file-block").classList.remove("hidden");
    $("requisites-block").classList.add("hidden");

    hideError();
    $("draft-note").classList.add("hidden");
    refreshPayDate();
    refreshMainButton();
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
  }

  function askReset() {
    askConfirm(
      "Очистить форму",
      "Все введённые поля и сохранённый черновик будут удалены. "
      + "Уже поданные заявки это не затронет.",
      "Очистить",
      resetForm,
      true
    );
  }
  $("form-reset").addEventListener("click", askReset);
  $("draft-clear").addEventListener("click", askReset);

  fileInput.addEventListener("change", function () {
    var f = this.files[0];
    if (!f) return;
    var ext = (f.name.split(".").pop() || "").toLowerCase();
    if (ALLOWED_EXT.indexOf(ext) === -1) {
      showError("Неподдерживаемый формат файла. Нужен PDF, JPG, PNG или XLSX.");
      this.value = "";
      return;
    }
    if (f.size > MAX_FILE_SIZE) {
      showError("Файл больше 20 МБ.");
      this.value = "";
      return;
    }
    hideError();
    state.file = f;
    markDirty();
    dropZone.classList.remove("has-file");
    void dropZone.offsetWidth; // перезапуск анимации attach-bounce
    dropZone.classList.add("has-file");
    $("drop-icon").textContent = "✅";
    $("drop-text").innerHTML = '<span class="file-name"></span>';
    dropZone.querySelector(".file-name").textContent = f.name;
    $("drop-hint").textContent = (f.size / 1024 / 1024).toFixed(1) + " МБ · нажмите, чтобы заменить";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("light");
    precheckFile(f);
    refreshMainButton();
  });

  // Мгновенная автопроверка «похоже ли на счёт» — сразу при прикреплении.
  // Плашка живёт ВВЕРХУ блока «Счёт» — видна без прокрутки, прямо там,
  // где пользователь только что прикрепил файл.
  function hideFileWarn() {
    $("file-warn").className = "hidden";
  }
  // --- Бета: заполнение формы по распознанному счёту -------------------------
  // Ничего не подставляем сами: показываем, что нашли, и ждём нажатия.
  // Так фича не может испортить заявку, а выключить её можно в админ-панели.
  var autofillData = null;

  // Редактируемые поля предложения: ровно те, что реально попадут в форму.
  // Номер счёта показываем справочно — в заявку он пока не переносится.
  var AUTOFILL_FIELDS = [
    { key: "amount", label: "Сумма", type: "input" },
    { key: "counterparty", label: "Контрагент", type: "input" },
    { key: "requisites", label: "Реквизиты", type: "textarea" }
  ];

  function showAutofill(data) {
    autofillData = data && Object.keys(data).length ? data : null;
    var box = $("autofill");
    if (!autofillData) { box.classList.add("hidden"); return; }

    var list = $("autofill-list");
    list.innerHTML = "";

    AUTOFILL_FIELDS.forEach(function (f) {
      var value = autofillData[f.key];
      if (!value) return;
      var wrap = document.createElement("div");
      wrap.className = "autofill-field";

      var label = document.createElement("label");
      label.textContent = f.label;
      var id = "af-" + f.key;
      label.setAttribute("for", id);

      var field = document.createElement(f.type === "textarea" ? "textarea" : "input");
      field.id = id;
      field.dataset.af = f.key;
      if (f.type === "textarea") field.rows = 4;
      else field.type = "text";
      // Сумму показываем в привычном виде — так ошибку заметнее.
      if (f.key === "amount") {
        var parsed = parseAmount(value);
        field.value = parsed === null ? value : formatAmount(parsed);
        field.setAttribute("inputmode", "decimal");
      } else {
        field.value = value;
      }
      wrap.appendChild(label);
      wrap.appendChild(field);
      list.appendChild(wrap);
    });

    // Номер и дата счёта — справочно, одной строкой.
    if (autofillData.invoice_number) {
      var note = document.createElement("div");
      note.className = "autofill-note";
      note.innerHTML = "";
      var k = document.createElement("span");
      k.textContent = "Счёт №";
      var v = document.createElement("b");
      v.textContent = autofillData.invoice_number +
        (autofillData.invoice_date ? " от " + autofillData.invoice_date : "");
      note.appendChild(k);
      note.appendChild(v);
      list.appendChild(note);
    }

    box.classList.toggle("hidden", !list.children.length);
  }

  function autofillValue(key) {
    var field = document.querySelector('#autofill [data-af="' + key + '"]');
    return field ? field.value.trim() : "";
  }

  function applyAutofill() {
    if (!autofillData) return;
    var filled = 0;

    var amount = autofillValue("amount");
    if (amount && !amountEl.value.trim()) {
      var parsed = parseAmount(amount);
      amountEl.value = parsed === null ? amount : formatAmount(parsed);
      filled++;
    }
    var counterparty = autofillValue("counterparty");
    if (counterparty && !cpEl.value.trim()) {
      cpEl.value = counterparty;
      cpEl.classList.remove("invalid");
      filled++;
    }
    // Реквизиты нужны, только когда заявка идёт без счёта; но если человек
    // переключится на «Реквизиты», они уже будут на месте.
    var requisites = autofillValue("requisites");
    if (requisites && !reqEl.value.trim()) {
      reqEl.value = requisites;
      $("req-count").textContent = reqEl.value.length;
      setVeil(true);
      checkReqNow();
      filled++;
    }

    markDirty();
    saveDraftSoon();
    refreshMainButton();
    $("autofill").classList.add("hidden");
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
    if (!filled) {
      showModal("Поля уже заполнены",
                "Всё, что распознал бот, вы уже ввели сами — ничего не менял.");
    }
  }

  $("autofill-apply").addEventListener("click", applyAutofill);
  $("autofill-skip").addEventListener("click", function () {
    $("autofill").classList.add("hidden");
    autofillData = null;
  });

  function precheckFile(f) {
    var note = $("file-warn");
    if (!insideTelegram) { note.className = "hidden"; return; }
    note.className = "checking-note";
    note.textContent = "🔎 Проверяю, похож ли файл на счёт…";
    var fd = new FormData();
    fd.append("file", f);
    fd.append("amount", amountEl.value || "");
    fetch("/api/check-file", {
      method: "POST",
      headers: { "X-Telegram-Init-Data": tg.initData },
      body: fd
    })
      .then(function (r) { return r.ok ? r.json() : { warning: null }; })
      .then(function (d) {
        if (state.file !== f) return;  // файл уже заменили
        showAutofill(d && d.autofill);
        if (d.warning) {
          note.className = "req-warn";
          note.textContent = d.warning + " Заявку всё равно можно отправить.";
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("warning");
        } else {
          note.className = "hidden";
        }
      })
      .catch(function () { note.className = "hidden"; });
  }

  // parseAmount/formatAmount — в form-lib.js

  // --- Валидация формы целиком --------------------------------------------------
  function validationError(markFields) {
    var err = null, el = null;
    if (parseAmount(amountEl.value) === null) { err = "Введите корректную сумму."; el = amountEl; }
    else if (!cpEl.value.trim()) { err = "Укажите контрагента."; el = cpEl; }
    else if (!currentArticle()) { err = "Выберите статью расходов."; el = $("article-custom"); }
    else if (state.urgency === "CUSTOM" && !plannedEl.value) { err = "Выберите дату оплаты в календаре."; el = plannedEl; }
    else if (state.urgency === "CUSTOM" && plannedEl.value < todayISO()) { err = "Дата оплаты не может быть в прошлом."; el = plannedEl; }
    // Комментарий необязателен.
    else if (state.hasInvoice && !state.file) { err = "Прикрепите файл счёта."; }
    else if (!state.hasInvoice && !reqEl.value.trim()) { err = "Укажите реквизиты для оплаты."; el = reqEl; }
    if (markFields && el) el.classList.add("invalid");
    return err;
  }

  function refreshMainButton() {
    var ok = validationError(false) === null;
    if (tg && insideTelegram) {
      tg.MainButton.setText("Отправить заявку");
      if (ok && !state.submitting) tg.MainButton.enable(); else tg.MainButton.disable();
      if (!tg.MainButton.isVisible) tg.MainButton.show();
    }
  }

  /** Родная кнопка Telegram красится темой КЛИЕНТА, поэтому в неоновой шкуре
   *  «Отправить заявку» оставалась синей и выбивалась из изумруда. Берём цвет
   *  из тех же токенов, что и вся страница: в телеграмной шкуре --accent и
   *  есть кнопочный цвет темы, так что там ничего не меняется. */
  function paintMainButton() {
    if (!tg || !insideTelegram || !tg.MainButton || !tg.MainButton.setParams) return;
    var probe = document.createElement("span");
    probe.style.cssText = "position:absolute;left:-9999px;" +
      "color:var(--accent);background-color:var(--accent-text)";
    document.body.appendChild(probe);
    var st = getComputedStyle(probe);
    var color = cssHex(st.color);
    var textColor = cssHex(st.backgroundColor);
    document.body.removeChild(probe);
    if (!color || !textColor) return;
    // Клиенты до Bot API 6.1 метод не знают — там кнопка останется темовой.
    try { tg.MainButton.setParams({ color: color, text_color: textColor }); }
    catch (e) { /* старые клиенты */ }
  }

  /** «rgb(16, 185, 129)» → «#10b981»: setParams принимает только hex. */
  function cssHex(value) {
    var m = /^rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(value || "");
    if (!m) return null;
    return "#" + [m[1], m[2], m[3]].map(function (n) {
      return ("0" + (+n).toString(16)).slice(-2);
    }).join("");
  }

  // --- Ошибки ---------------------------------------------------------------------
  function showError(msg) {
    var b = $("error-banner");
    b.textContent = "⚠️ " + msg;
    b.classList.remove("shaking");
    void b.offsetWidth; // перезапуск анимации shake
    b.classList.add("shaking");
    b.style.display = "block";
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("error");
  }
  function hideError() {
    var b = $("error-banner");
    b.style.display = "none";
    b.classList.remove("shaking");
  }

  // --- Отправка ---------------------------------------------------------------------
  function askDuplicate(message) {
    askConfirm(
      "⚠️ Похоже на дубль",
      (message || "Похожая заявка уже подавалась.") + "\n\nОтправить ещё раз?",
      "Всё равно отправить",
      function () { submit(true); },
      true
    );
  }

  function submit(force) {
    if (state.submitting) return;
    var err = validationError(true);
    if (err) { showError(err); return; }
    hideError();
    state.submitting = true;
    if (tg && insideTelegram) { tg.MainButton.showProgress(); tg.MainButton.disable(); }
    var fb = $("submit-fallback");
    fb.disabled = true; fb.style.opacity = ".6";

    var fd = new FormData();
    fd.append("force", force ? "1" : "0");
    fd.append("amount", amountEl.value);
    fd.append("currency", state.currency);
    fd.append("counterparty", cpEl.value);
    fd.append("article", currentArticle());
    fd.append("planned_date", resolvedPlannedDate());
    fd.append("comment", commentEl.value);
    // «Настраиваемая» — это про дату, для реестра срочность обычная.
    fd.append("urgency", state.urgency === "URGENT" ? "URGENT" : "NORMAL");
    fd.append("has_invoice", state.hasInvoice ? "1" : "0");
    if (state.hasInvoice) fd.append("file", state.file);
    else fd.append("requisites", reqEl.value);

    // Возврат итога в группу/канал: либо ?return_chat= (web_app-кнопка в личке),
    // либо start_param прямой ссылки t.me/<бот>/<имя>?startapp=<chat_id>.
    // start_param «help» — ссылка «Инструкция», а не чат возврата.
    var returnChat = new URLSearchParams(location.search).get("return_chat");
    if (!returnChat && tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) {
      returnChat = tg.initDataUnsafe.start_param;
    }
    if (returnChat && returnChat !== "help") fd.append("return_chat", returnChat);

    fetch("/api/invoice", {
      method: "POST",
      headers: { "X-Telegram-Init-Data": tg ? tg.initData : "" },
      body: fd
    })
    .then(function (resp) {
      return resp.json().catch(function () { return {}; }).then(function (data) {
        if (resp.status === 409 && data.duplicate) {
          // Дедуп: похожая заявка уже подавалась — спрашиваем подтверждение.
          state.submitting = false;
          fb.disabled = false; fb.style.opacity = "";
          if (tg && insideTelegram) { tg.MainButton.hideProgress(); }
          refreshMainButton();
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("warning");
          askDuplicate(data.detail);
          return null;
        }
        if (!resp.ok) {
          throw new Error(data.detail || ("Ошибка сервера (" + resp.status + "). Попробуйте позже."));
        }
        return data;
      });
    })
    .then(function (data) {
      if (data === null) return;  // ждём решения по дублю
      if (tg && insideTelegram) {
        // На случай клиентов, где подтверждение включалось раньше.
        try { tg.disableClosingConfirmation(); } catch (e) { /* старые клиенты */ }
        tg.MainButton.hideProgress();
        tg.MainButton.hide();
        if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      }
      clearDraft();
      // Заявка ушла — значит, этими статьёй и контрагентом пользуются:
      // если подсказки были скрыты, возвращаем их.
      unhideUsed(cpEl.value.trim(), currentArticle());
      $("form-view").style.display = "none";
      $("success-id").textContent = "№ " + (data.request_id || "");
      if (data.planned_date) {
        var sd = $("success-date");
        sd.textContent = "📅 Оплатить до: " + data.planned_date;
        sd.classList.remove("hidden");
      }
      $("success").classList.add("shown");
      if (data.warning) {
        // Автопроверка файла: показываем пометку и НЕ закрываем окно сами.
        var w = $("success-warning");
        w.textContent = data.warning;
        w.classList.remove("hidden");
        var closeBtn = $("success-close");
        closeBtn.classList.remove("hidden");
        closeBtn.addEventListener("click", function () {
          if (tg && insideTelegram) tg.close();
        });
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("warning");
      } else if (tg && insideTelegram) {
        setTimeout(function () { tg.close(); }, 2200);
      }
    })
    .catch(function (e) {
      state.submitting = false;
      fb.disabled = false; fb.style.opacity = "";
      if (tg && insideTelegram) { tg.MainButton.hideProgress(); }
      showError(e.message);
      refreshMainButton();
    });
  }

  // --- Черновик в localStorage ----------------------------------------------------
  var DRAFT_KEY = "invoice_draft_v1";
  var draftTimer = null;
  function saveDraftSoon() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(saveDraft, 300);
  }
  function saveDraft() {
    try {
      localStorage.setItem(DRAFT_KEY, JSON.stringify({
        t: Date.now(), a: amountEl.value, cur: state.currency, cp: cpEl.value,
        art: state.article, artc: $("article-custom").value, pd: plannedEl.value,
        cm: commentEl.value, u: state.urgency, hi: state.hasInvoice, rq: reqEl.value
      }));
    } catch (e) { /* localStorage может быть недоступен */ }
  }
  function clearDraft() {
    try { localStorage.removeItem(DRAFT_KEY); } catch (e) { /* ignore */ }
  }
  function setSeg(containerId, value) {
    var box = $(containerId);
    box.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("active", b.dataset.value === value);
    });
    revealActive(box);
  }

  /** В листающемся ряду выбранное могло остаться за обрезом — например,
   *  после восстановления черновика или повтора заявки. Подтягиваем в кадр
   *  прокруткой самого ряда, не трогая прокрутку страницы. */
  function revealActive(box) {
    if (!box || !box.classList.contains("seg-scroll")) return;
    var active = box.querySelector("button.active");
    if (!active) { updateFades(box); return; }
    box.scrollLeft +=
      active.getBoundingClientRect().left - box.getBoundingClientRect().left - 12;
    updateFades(box);
  }
  function restoreDraft() {
    var d = null;
    try { d = JSON.parse(localStorage.getItem(DRAFT_KEY) || "null"); } catch (e) { return; }
    if (!d || !d.t || Date.now() - d.t > 24 * 3600 * 1000) return;
    if (!(d.a || d.cp || d.cm || d.rq)) return;
    amountEl.value = d.a || "";
    cpEl.value = d.cp || "";
    commentEl.value = d.cm || "";
    reqEl.value = d.rq || "";
    $("comment-count").textContent = commentEl.value.length;
    $("req-count").textContent = reqEl.value.length;
    if (d.cur && CURRENCIES.indexOf(d.cur) !== -1) { state.currency = d.cur; setSeg("currency-seg", d.cur); }
    if (d.art && ARTICLES.indexOf(d.art) !== -1) {
      state.article = d.art;
      ensureArticleVisible(d.art);   // черновик мог ссылаться на скрытую
      setSeg("article-seg", d.art);
    }
    if (d.artc) { $("article-custom").value = d.artc; }
    if (d.u === "NORMAL" || d.u === "URGENT" || d.u === "CUSTOM") {
      state.urgency = d.u;
      setSeg("urgency-seg", d.u);
    }
    if (d.pd && d.pd >= todayISO()) { plannedEl.value = d.pd; }
    if (reqEl.value.trim()) setVeil(true);  // восстановленные реквизиты — скрыты
    refreshPayDate();
    if (typeof d.hi === "boolean") {
      state.hasInvoice = d.hi;
      setSeg("invoice-seg", d.hi ? "1" : "0");
      $("file-block").classList.toggle("hidden", !d.hi);
      $("requisites-block").classList.toggle("hidden", d.hi);
    }
    var note = $("draft-note");
    note.classList.remove("hidden");
    // Дольше, чем раньше: в плашке теперь есть «Очистить», и четырёх секунд
    // на решение мало. Позже сбросить можно строкой в конце формы.
    setTimeout(function () {
      note.style.transition = "opacity .6s";
      note.style.opacity = "0";
      setTimeout(function () { note.classList.add("hidden"); }, 650);
    }, 12000);
  }

  // --- «Мои заявки» -----------------------------------------------------------------
  var STATUS_CLASS = {
    "Новая": "st-new",
    "Оплачена": "st-paid",
    "Отклонена": "st-rejected",
    "Отозвана": "",
    "Отложена": ""
  };
  var STATUS_ICON = {
    "Новая": "⏳", "Оплачена": "✅", "Отложена": "⏸",
    "Отклонена": "❌", "Отозвана": "🚫"
  };

  function showMyMsg(text, isErr) {
    var m = $("my-msg");
    m.textContent = (isErr ? "⚠️ " : "✓ ") + text;
    m.style.display = "block";
    m.style.color = isErr ? "var(--danger)" : "";
    clearTimeout(m._t);
    m._t = setTimeout(function () { m.style.display = "none"; }, 4500);
  }

  /** Заполняет форму данными прошлой заявки. Файл счёта не переносится:
   *  на новый платёж нужен свежий счёт. */
  function applyRepeat(item) {
    var amount = parseAmount(item.amount);
    amountEl.value = amount === null ? item.amount : formatAmount(amount);
    if (CURRENCIES.indexOf(item.currency) !== -1) {
      state.currency = item.currency;
      setSeg("currency-seg", item.currency);
    }
    cpEl.value = item.counterparty || "";
    if (ARTICLES.indexOf(item.article) !== -1) {
      state.article = item.article;
      ensureArticleVisible(item.article);   // могла быть скрыта подсказкой
      setSeg("article-seg", item.article);
      $("article-custom").value = "";
    } else if (item.article) {
      state.article = "";
      // Своя статья: ни одна кнопка не совпадёт — выделение снимается со всех.
      setSeg("article-seg", "");
      $("article-custom").value = item.article;
    }
    commentEl.value = item.comment || "";
    $("comment-count").textContent = commentEl.value.length;
    // Срочность переносим, а плановую дату считаем заново: прошлая — в прошлом.
    state.urgency = item.urgency === "Срочно" ? "URGENT" : "NORMAL";
    setSeg("urgency-seg", state.urgency);
    plannedEl.value = "";
    $("planned-block").classList.add("hidden");
    refreshPayDate();

    var hasInvoice = !!item.has_invoice;
    state.hasInvoice = hasInvoice;
    setSeg("invoice-seg", hasInvoice ? "1" : "0");
    $("file-block").classList.toggle("hidden", !hasInvoice);
    $("requisites-block").classList.toggle("hidden", hasInvoice);
    if (!hasInvoice && item.requisites) {
      reqEl.value = item.requisites;
      $("req-count").textContent = reqEl.value.length;
      setVeil(true);
      checkReqNow();
    }
    markDirty();
    saveDraft();
    refreshMainButton();
  }

  function withdrawItem(id, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/my/withdraw", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ request_id: id })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showMyMsg(d.message || d.detail || "Готово.", !(r.ok && d.ok));
          if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(r.ok && d.ok ? "success" : "error");
          }
          if (r.ok && d.ok) loadMy();
        });
      })
      .catch(function () { showMyMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  /** «Изменить»: отзываем прежнюю заявку и открываем форму с её полями. */
  function startEdit(item, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/my/withdraw", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ request_id: item.id })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!(r.ok && d.ok)) {
            // Заявку успели обработать — правка уже небезопасна.
            showModal("Изменить не получилось", d.message || d.detail || "Попробуйте позже.");
            loadMy();
            return;
          }
          applyRepeat(item);
          closeMy();
          hideError();
          var note = $("draft-note");
          note.textContent = "✏️ Прежняя заявка отозвана, поля перенесены";
          note.classList.remove("hidden");
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        });
      })
      .catch(function () { showMyMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  function renderMy(items) {
    var box = $("my-list");
    box.innerHTML = "";
    if (!items.length) {
      var art = document.createElement("img");
      art.className = "empty-art";
      art.src = "logo.svg";
      art.alt = "";
      art.setAttribute("aria-hidden", "true");
      box.appendChild(art);
      var empty = document.createElement("div");
      empty.className = "empty-note";
      empty.textContent = "Заявок пока нет — заполните форму, и они появятся здесь.";
      box.appendChild(empty);
      return;
    }
    items.forEach(function (it, i) {
      var row = document.createElement("div");
      row.className = "my-item";
      row.style.animationDelay = (i * 0.03) + "s";

      var top = document.createElement("div");
      top.className = "my-top";
      var st = document.createElement("span");
      st.className = "my-status " + (STATUS_CLASS[it.status] || "");
      st.textContent = (STATUS_ICON[it.status] || "•") + " " + it.status;
      var sum = document.createElement("span");
      sum.className = "my-sum";
      var parsed = parseAmount(it.amount);
      sum.textContent = (parsed === null ? it.amount : formatAmount(parsed)) +
        " " + (it.currency || "");
      top.appendChild(st);
      top.appendChild(sum);
      row.appendChild(top);

      var cp = document.createElement("div");
      cp.className = "my-cp";
      cp.textContent = it.counterparty || "—";
      row.appendChild(cp);

      var meta = document.createElement("div");
      meta.className = "my-meta";
      meta.textContent = "📂 " + (it.article || "—") +
        (it.urgency === "Срочно" ? " · 🔴 срочно" : "") +
        (it.has_invoice ? " · 📎 счёт" : " · ✍️ реквизиты");
      row.appendChild(meta);

      var dates = document.createElement("div");
      dates.className = "my-meta";
      dates.textContent = "📅 оплатить до " + (it.planned_date || "—") +
        (it.created_at ? " · подана " + it.created_at : "");
      row.appendChild(dates);

      if (it.comment) {
        var cm = document.createElement("div");
        cm.className = "my-meta my-comment";
        cm.textContent = "📝 " + it.comment;
        row.appendChild(cm);
      }

      var idEl = document.createElement("div");
      idEl.className = "my-id";
      idEl.textContent = it.id;
      row.appendChild(idEl);

      if (it.reason) {
        var reason = document.createElement("div");
        reason.className = "my-reason";
        reason.textContent = "💬 " + it.reason;
        row.appendChild(reason);
      }

      var actions = document.createElement("div");
      actions.className = "my-actions";
      actions.appendChild(detailButton(it));
      var repeat = document.createElement("button");
      repeat.type = "button";
      repeat.className = "add-btn btn-ghost";
      repeat.textContent = "↻ Повторить";
      repeat.addEventListener("click", function () {
        applyRepeat(it);
        closeMy();
        hideError();
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      });
      actions.appendChild(repeat);

      if (it.status === "Новая") {
        // Правка = отзыв прежней заявки + перенос полей в форму: менять
        // заявку, которую финансист уже видит, нельзя — он оплатит старую.
        var edit = document.createElement("button");
        edit.type = "button";
        edit.className = "add-btn btn-ghost";
        edit.textContent = "✏️ Изменить";
        edit.addEventListener("click", function () {
          askConfirm(
            "✏️ Изменить заявку",
            "Прежняя заявка на «" + (it.counterparty || "") + "» будет отозвана, " +
            "а её поля перенесены в форму. Проверьте, приложите счёт " +
            "и отправьте заново.",
            "Изменить",
            function () { startEdit(it, edit); }
          );
        });
        actions.appendChild(edit);

        var wd = document.createElement("button");
        wd.type = "button";
        wd.className = "add-btn btn-danger";
        wd.textContent = "🚫 Отозвать";
        wd.addEventListener("click", function () {
          askConfirm(
            "🚫 Отозвать заявку",
            "Заявка на «" + (it.counterparty || "") + "» будет отозвана. " +
            "Финансисты получат уведомление, оплатить её уже нельзя.",
            "Отозвать",
            function () { withdrawItem(it.id, wd); },
            true
          );
        });
        actions.appendChild(wd);
      }
      // Свою заявку автор удаляет только после отзыва; админ — любую
      // (право проверяет сервер, кнопку показываем по тому же правилу).
      if (it.status === "Отозвана" || isBotAdmin) {
        actions.appendChild(deleteButton(it, loadMy));
      }
      row.appendChild(actions);
      makeTappable(row, it);
      box.appendChild(row);
    });
  }

  function loadMy() {
    if (!insideTelegram) { renderMy([]); return; }
    fetch("/api/my-requests", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (d) { renderMy((d && d.items) || []); })
      .catch(function () { showMyMsg("Не удалось загрузить список заявок.", true); });
  }

  function openMy() {
    $("form-view").style.display = "none";
    $("my-view").classList.remove("hidden");
    window.scrollTo(0, 0);
    if (tg && insideTelegram) {
      tg.MainButton.hide();
      tg.BackButton.onClick(closeMy);
      tg.BackButton.show();
    }
    loadMy();
  }
  function closeMy() {
    $("my-view").classList.add("hidden");
    $("form-view").style.display = "";
    if (tg && insideTelegram) {
      tg.BackButton.hide();
      tg.BackButton.offClick(closeMy);
      refreshMainButton();
    }
  }
  $("my-btn").addEventListener("click", openMy);
  $("my-close").addEventListener("click", closeMy);

  /** Открытие по ссылке «↻ Повторить» из чата: ?repeat=<id> / startapp=repeat_<id>. */
  function applyRepeatById(requestId) {
    if (!insideTelegram) return;
    fetch("/api/my-requests?request_id=" + encodeURIComponent(requestId), {
      headers: { "X-Telegram-Init-Data": tg.initData }
    })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (d) {
        var items = (d && d.items) || [];
        if (!items.length) { showError("Заявка для повтора не найдена."); return; }
        applyRepeat(items[0]);
        var note = $("draft-note");
        note.textContent = "↻ Поля заполнены по прошлой заявке";
        note.classList.remove("hidden");
      })
      .catch(function () { /* повтор не критичен — форма просто останется пустой */ });
  }

  // --- Подробности заявки -------------------------------------------------------------
  // STATUS_CLASS/STATUS_ICON объявлены выше, в блоке «Мои заявки».
  // Права админа: определяются в tryAdmin(), решают, показывать ли «Удалить»
  // для чужих и необработанных заявок. Авторитет всё равно за сервером.
  var isBotAdmin = false;
  function amountText(item) {
    var parsed = parseAmount(item.amount);
    return (parsed === null ? item.amount : formatAmount(parsed)) +
      " " + (item.currency || "");
  }

  function detailRows(item) {
    var dl = document.createElement("dl");
    dl.className = "modal-rows";
    function add(label, value) {
      if (!value) return;
      var dt = document.createElement("dt");
      dt.textContent = label;
      var dd = document.createElement("dd");
      dd.textContent = value;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    add("Статус", (STATUS_ICON[item.status] || "") + " " + item.status);
    add("Контрагент", item.counterparty);
    add("Сумма", amountText(item));
    add("Статья", item.article);
    add("Срочность", item.urgency);
    add("Оплатить до", item.planned_date);
    add("Подана", item.created_at);
    add("Сотрудник", item.sender);
    add("Комментарий", item.comment);
    add("Счёт", item.has_invoice ? "📎 файл приложен" : "✍️ по реквизитам");
    add("Причина", item.reason);
    add("Номер", item.id);
    return dl;
  }

  /** Карточка заявки целиком — по нажатию на строку списка. */
  function showRequestDetail(item) {
    var buttons = [{ text: "Закрыть", style: "ghost" }];
    // Реквизиты — отдельным действием, как спойлер в чате: не мелькают
    // на экране, когда рядом кто-то стоит.
    if (item.requisites) {
      buttons.unshift({
        text: "👁 Реквизиты",
        onClick: function () { showRequisites(item); }
      });
    }
    showModal("🧾 " + (item.counterparty || "Заявка"), detailRows(item), buttons);
  }

  function showRequisites(item) {
    var box = document.createElement("div");
    var pre = document.createElement("div");
    pre.className = "modal-req";
    pre.textContent = item.requisites;
    box.appendChild(pre);
    showModal("✍️ Реквизиты", box, [
      { text: "← Назад", style: "ghost", onClick: function () { showRequestDetail(item); } },
      { text: "Закрыть" }
    ]);
  }

  /** Делает строку списка кликабельной, не мешая кнопкам внутри неё. */
  function makeTappable(row, item) {
    row.classList.add("tappable");
    row.addEventListener("click", function (ev) {
      if (ev.target.closest("button")) return;
      showRequestDetail(item);
    });
  }

  /** Кнопка «Подробнее» — на случай, если по строке нажать не догадались. */
  function detailButton(item) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "add-btn btn-ghost";
    btn.textContent = "ℹ️ Подробнее";
    btn.addEventListener("click", function () { showRequestDetail(item); });
    return btn;
  }

  /** Кнопка «Удалить»: необратимо, поэтому всегда через подтверждение. */
  function deleteButton(item, afterDelete) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "add-btn btn-danger";
    btn.textContent = "🗑 Удалить";
    btn.addEventListener("click", function () {
      askConfirm(
        "🗑 Удалить заявку",
        "Заявка на «" + (item.counterparty || "") + "» будет удалена из реестра " +
        "без возможности восстановления. Останется только запись в журнале аудита.",
        "Удалить",
        function () { deleteRequest(item, btn, afterDelete); },
        true
      );
    });
    return btn;
  }

  function deleteRequest(item, btn, afterDelete) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/requests/delete", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ request_id: item.id })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!(r.ok && d.ok)) {
            showModal("Удалить не получилось", d.message || d.detail || "Попробуйте позже.");
            return;
          }
          if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
          afterDelete();
        });
      })
      .catch(function () {
        showModal("Сеть недоступна", "Не удалось связаться с сервером.");
      })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  // --- Второй скин: «Неон» ------------------------------------------------------
  // Оформление — дело вкуса, поэтому не навязываем: два варианта и выбор
  // запоминается. Скин применяется переопределением токенов в :root,
  // поэтому переключение мгновенное и не требует перезагрузки.
  var SKIN_KEY = "invoice_skin_v1";
  var SKIN_ANIM_KEY = "invoice_skin_anim_v1";
  var skinField = null;
  // Живой фон можно выключить отдельно от темы — ради батареи и покоя.
  var skinAnimation = true;
  try { skinAnimation = localStorage.getItem(SKIN_ANIM_KEY) !== "0"; }
  catch (e) { /* приватный режим */ }

  function currentSkin() {
    return document.documentElement.getAttribute("data-skin") === "neon" ? "neon" : "tg";
  }

  function applySkin(name, remember) {
    if (name === "neon") document.documentElement.setAttribute("data-skin", "neon");
    else document.documentElement.removeAttribute("data-skin");
    if (remember) {
      try { localStorage.setItem(SKIN_KEY, name); } catch (e) { /* приватный режим */ }
    }
    if (skinField) {
      skinField.toggle(skinAnimation);
      skinField.refresh();
    }
    paintMainButton();
  }

  // Живое полотно фона живёт в skin-field.js — замкнутый модуль на канвасе.

  skinField = buildSkinField($("skin-field"));

  /** Экран «Оформление»: отдельный блок настроек, а не кнопка-переключатель —
   *  сюда же лягут будущие параметры вида. */
  function markSkinChoice() {
    var current = currentSkin();
    document.querySelectorAll(".skin-opt").forEach(function (opt) {
      opt.classList.toggle("active", opt.dataset.skinValue === current);
      opt.setAttribute("aria-pressed", String(opt.dataset.skinValue === current));
    });
    // Живой фон работает в обеих темах, притухать карточке незачем.
  }

  document.querySelectorAll(".skin-opt").forEach(function (opt) {
    opt.addEventListener("click", function () {
      applySkin(opt.dataset.skinValue, true);
      markSkinChoice();
      if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    });
  });

  bindFilterSeg("skin-anim-seg", function (v) {
    skinAnimation = v === "on";
    try { localStorage.setItem(SKIN_ANIM_KEY, skinAnimation ? "1" : "0"); }
    catch (e) { /* приватный режим */ }
    if (skinField) skinField.toggle(skinAnimation);
  });

  // Восстанавливаем выбор (атрибут уже мог поставить ранний скрипт в <head>).
  applySkin(currentSkin(), false);
  markSkinChoice();

  // --- Иконки шапки -----------------------------------------------------------------
  // Порядок справа налево. Панель финансиста и шестерёнка появляются не у
  // всех, поэтому места пересчитываем: иначе от скрытой кнопки оставалась
  // дыра, а от лишнего padding заголовок переносился на вторую строку.
  var HEADER_ICONS = ["help-btn", "my-btn", "fin-btn", "admin-btn"];

  function layoutHeaderIcons() {
    var header = $("help-btn").closest("header");
    if (!header) return;
    var visible = HEADER_ICONS.filter(function (id) {
      var el = $(id);
      return el && !el.classList.contains("hidden");
    });
    // Чем больше иконок, тем плотнее шаг: иначе заголовку не остаётся места.
    var pitch = visible.length >= 4 ? (window.innerWidth < 400 ? 34 : 38) : 46;
    visible.forEach(function (id, i) { $(id).style.right = (i * pitch) + "px"; });

    header.className = header.className.replace(/\s*icons-\d/g, "");
    if (visible.length >= 4) header.className += " icons-" + Math.min(visible.length, 5);
    header.style.paddingRight = (visible.length * pitch + 6) + "px";

    // Вертикаль считаем ПОСЛЕ класса плотности: он меняет кегль заголовка,
    // а с ним и высоту строки. Строка с именем выше кнопок (в ней марка),
    // поэтому прибитые к top: 0 кнопки висели над ней. В CSS эту высоту не
    // узнать, а transform у кнопок занят состоянием :active.
    var brand = header.querySelector(".brand");
    var size = visible.length ? $(visible[0]).getBoundingClientRect().height : 0;
    var top = brand && size
      ? Math.max(0, (brand.getBoundingClientRect().height - size) / 2) : 0;
    visible.forEach(function (id) { $(id).style.top = top.toFixed(2) + "px"; });
  }
  // Поворот экрана меняет доступную ширину — пересчитываем.
  window.addEventListener("resize", function () {
    clearTimeout(layoutHeaderIcons._t);
    layoutHeaderIcons._t = setTimeout(layoutHeaderIcons, 200);
  });

  // --- Панель финансиста: все заявки с фильтрами -------------------------------------
  var finFilters = { query: "", status: "", urgency: "", from: "", to: "" };

  // Кнопки смены статуса в панели: те же три, что на карточке в чате.
  // Держать в синхроне с REQUEST_STATUSES (bot/models.py).
  var STATUS_ACTIONS = [
    { key: "PAID", label: "✅ Оплачено", status: "Оплачена", reason: false },
    { key: "DEFERRED", label: "⏸ Отложить", status: "Отложена", reason: true },
    { key: "REJECTED", label: "❌ Отклонить", status: "Отклонена", reason: true }
  ];

  function sendStatus(item, act, reason, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/finance/status", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ request_id: item.id, key: act.key, reason: reason || "" })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showFinMsg(d.message || d.detail || "Готово.", !(r.ok && d.ok));
          if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(r.ok && d.ok ? "success" : "error");
          }
          if (r.ok && d.ok) loadFinance();
        });
      })
      .catch(function () { showFinMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  function statusButton(item, act) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "add-btn " + (act.key === "REJECTED" ? "btn-danger" : "btn-ghost");
    btn.textContent = act.label;
    btn.addEventListener("click", function () {
      if (!act.reason) {
        askConfirm(
          act.label,
          "Отметить заявку на «" + (item.counterparty || "") + "» как " +
          act.status.toLowerCase() + "? Автор получит уведомление.",
          "Подтвердить",
          function () { sendStatus(item, act, "", btn); }
        );
        return;
      }
      // «Отложить»/«Отклонить» — с причиной, как в чате: автор должен
      // понимать, что делать дальше.
      showPrompt(
        act.label,
        "Причина по заявке на «" + (item.counterparty || "") +
        "» — её увидит автор. Можно оставить пустым.",
        "например: нет бюджета до 15-го",
        "Применить",
        function (reason) { sendStatus(item, act, reason, btn); }
      );
    });
    return btn;
  }

  function showFinMsg(text, isErr) {
    var m = $("fin-msg");
    m.textContent = (isErr ? "⚠️ " : "✓ ") + text;
    m.style.display = "block";
    m.style.color = isErr ? "var(--danger)" : "";
    clearTimeout(m._t);
    m._t = setTimeout(function () { m.style.display = "none"; }, 4500);
  }

  function renderFinMeta(data) {
    var box = $("fin-meta");
    if (!data.total_found) {
      box.textContent = "";
      return;
    }
    var text = "Найдено: " + data.total_found;
    if (data.shown < data.total_found) text += " · показаны первые " + data.shown;
    // Честно предупреждаем, что фильтры прошли не по всему реестру.
    if (data.scanned >= data.scan_limit) {
      text += " · просмотрены последние " + data.scan_limit;
    }
    box.textContent = text;
  }

  function renderFinList(items) {
    var box = $("fin-req-list");
    box.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "empty-note";
      empty.textContent = "Заявок под эти фильтры нет.";
      box.appendChild(empty);
      return;
    }
    items.forEach(function (it, i) {
      var row = document.createElement("div");
      row.className = "my-item";
      row.style.animationDelay = Math.min(i * 0.02, 0.3) + "s";

      var top = document.createElement("div");
      top.className = "my-top";
      var st = document.createElement("span");
      st.className = "my-status " + (STATUS_CLASS[it.status] || "");
      st.textContent = (STATUS_ICON[it.status] || "•") + " " + it.status;
      var sum = document.createElement("span");
      sum.className = "my-sum";
      var parsed = parseAmount(it.amount);
      sum.textContent = (parsed === null ? it.amount : formatAmount(parsed)) +
        " " + (it.currency || "");
      top.appendChild(st);
      top.appendChild(sum);
      row.appendChild(top);

      var cp = document.createElement("div");
      cp.className = "my-cp";
      cp.textContent = it.counterparty || "—";
      row.appendChild(cp);

      var who = document.createElement("div");
      who.className = "my-meta";
      who.textContent = "👤 " + (it.sender || "—");
      row.appendChild(who);

      var meta = document.createElement("div");
      meta.className = "my-meta";
      meta.textContent = "📂 " + (it.article || "—") +
        (it.urgency === "Срочно" ? " · 🔴 срочно" : "") +
        (it.has_invoice ? " · 📎 счёт" : " · ✍️ реквизиты");
      row.appendChild(meta);

      var dates = document.createElement("div");
      dates.className = "my-meta";
      dates.textContent = "📅 оплатить до " + (it.planned_date || "—") +
        (it.created_at ? " · подана " + it.created_at : "");
      row.appendChild(dates);

      if (it.comment) {
        var cm = document.createElement("div");
        cm.className = "my-meta my-comment";
        cm.textContent = "📝 " + it.comment;
        row.appendChild(cm);
      }
      if (it.reason) {
        var reason = document.createElement("div");
        reason.className = "my-reason";
        reason.textContent = "💬 " + it.reason;
        row.appendChild(reason);
      }

      var idEl = document.createElement("div");
      idEl.className = "my-id";
      idEl.textContent = it.id;
      row.appendChild(idEl);

      var actions = document.createElement("div");
      actions.className = "my-actions";
      actions.appendChild(detailButton(it));
      // Статус меняется прямо здесь — то же действие, что кнопки на карточке
      // в чате. Отозванную заявку не трогаем: её автор забрал.
      if (it.status !== "Отозвана") {
        STATUS_ACTIONS.forEach(function (act) {
          if (it.status === act.status) return;   // уже в этом статусе
          actions.appendChild(statusButton(it, act));
        });
      }
      if (isBotAdmin) actions.appendChild(deleteButton(it, loadFinance));
      row.appendChild(actions);

      makeTappable(row, it);
      box.appendChild(row);
    });
  }

  function loadFinance() {
    if (!insideTelegram) return;
    var params = new URLSearchParams();
    if (finFilters.query) params.set("query", finFilters.query);
    if (finFilters.status) params.set("status", finFilters.status);
    if (finFilters.urgency) params.set("urgency", finFilters.urgency);
    if (finFilters.from) params.set("date_from", finFilters.from);
    if (finFilters.to) params.set("date_to", finFilters.to);
    $("fin-meta").textContent = "Загружаю…";

    fetch("/api/finance/requests?" + params.toString(), {
      headers: { "X-Telegram-Init-Data": tg.initData }
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) {
            showFinMsg(d.detail || "Не удалось загрузить заявки.", true);
            renderFinList([]);
            return;
          }
          renderFinMeta(d);
          renderFinList(d.items || []);
        });
      })
      .catch(function () { showFinMsg("Сеть недоступна.", true); });
  }

  // Свой обработчик, не bindSeg: тот помечает форму заявки изменённой
  // и сохраняет черновик — фильтры к форме отношения не имеют.
  function bindFilterSeg(containerId, onChange) {
    var seg = $(containerId);
    seg.addEventListener("click", function (ev) {
      var btn = ev.target.closest("button");
      if (!btn || btn.classList.contains("active")) return;
      seg.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
      btn.classList.add("active");
      if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
      onChange(btn.dataset.value || "");
    });
  }
  bindFilterSeg("fin-status-seg", function (v) { finFilters.status = v; loadFinance(); });
  bindFilterSeg("fin-urgency-seg", function (v) { finFilters.urgency = v; loadFinance(); });

  var finQueryTimer = null;
  $("fin-query").addEventListener("input", function () {
    finFilters.query = this.value.trim();
    clearTimeout(finQueryTimer);
    finQueryTimer = setTimeout(loadFinance, 350);
  });
  $("fin-apply").addEventListener("click", function () {
    finFilters.from = $("fin-from").value;
    finFilters.to = $("fin-to").value;
    loadFinance();
  });
  $("fin-reset").addEventListener("click", function () {
    finFilters = { query: "", status: "", urgency: "", from: "", to: "" };
    $("fin-query").value = "";
    $("fin-from").value = "";
    $("fin-to").value = "";
    setSeg("fin-status-seg", "");
    setSeg("fin-urgency-seg", "");
    loadFinance();
  });

  $("fin-toggle").addEventListener("click", function () {
    var hidden = $("fin-filters").classList.toggle("hidden");
    this.textContent = hidden ? "Фильтры" : "Скрыть фильтры";
  });

  /** Показывает ИЛИ прячет кнопку панели: права могли измениться (админ
   *  убрал финансиста) — иначе кнопка оставалась висеть до перезагрузки. */
  /** Whitelist работает fail-closed, и новый человек упирался в молчаливый
   *  отказ при отправке. Показываем это сразу и даём попросить доступ. */
  function checkAccess() {
    if (!insideTelegram) return;
    fetch("/api/access", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || d.allowed) return;
        var gate = $("access-gate");
        var btn = $("access-ask");
        gate.classList.remove("hidden");
        if (!d.has_admins) {
          $("access-note").textContent =
            "У бота не задан ни один админ — рассмотреть заявку некому. " +
            "Обратитесь к тому, кто его настраивал.";
          btn.classList.add("hidden");
        } else if (d.pending) {
          markAccessPending();
        }
      })
      .catch(function () { /* сеть — отдельная история, форма не ломается */ });
  }

  function markAccessPending() {
    $("access-note").textContent =
      "⏳ Заявка отправлена — админы её видят. Ответ придёт в чат с ботом.";
    $("access-ask").classList.add("hidden");
  }

  $("access-ask").addEventListener("click", function () {
    var btn = this;
    btn.disabled = true;
    fetch("/api/access/request", {
      method: "POST",
      headers: { "X-Telegram-Init-Data": tg.initData }
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; })
          .then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        if (!res.ok) { showError(res.d.detail || "Не удалось отправить заявку."); return; }
        markAccessPending();
        showModal("Заявка отправлена", res.d.message || "Админы получили запрос.",
          [{ text: "Понятно", style: "primary" }]);
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
      })
      .catch(function () { showError("Сеть недоступна."); })
      .then(function () { btn.disabled = false; });
  });
  checkAccess();

  function tryFinance() {
    if (!insideTelegram) return;
    fetch("/api/finance/access", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.ok ? r.json() : { ok: false }; })
      .then(function (d) {
        var allowed = !!(d && d.ok);
        $("fin-btn").classList.toggle("hidden", !allowed);
        if (!allowed && !$("fin-view").classList.contains("hidden")) closeFinance();
        layoutHeaderIcons();
      })
      .catch(function () { /* не финансист или сеть — кнопки просто нет */ });
  }

  function openFinance() {
    $("form-view").style.display = "none";
    $("fin-view").classList.remove("hidden");
    window.scrollTo(0, 0);
    if (tg && insideTelegram) {
      tg.MainButton.hide();
      tg.BackButton.onClick(closeFinance);
      tg.BackButton.show();
    }
    loadFinance();
  }
  function closeFinance() {
    $("fin-view").classList.add("hidden");
    $("form-view").style.display = "";
    if (tg && insideTelegram) {
      tg.BackButton.hide();
      tg.BackButton.offClick(closeFinance);
      refreshMainButton();
    }
  }
  $("fin-btn").addEventListener("click", openFinance);
  $("fin-close").addEventListener("click", closeFinance);

  // --- Админ-панель -----------------------------------------------------------------
  function tryAdmin() {
    if (!insideTelegram) return;
    fetch("/api/admin/settings", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) {
        isBotAdmin = r.ok;
        // Шестерёнка есть у всех — за ней хотя бы «Оформление». Админские
        // вкладки открываются только тем, у кого на них есть право.
        [].forEach.call($("admin-tabs").querySelectorAll(".tab[data-admin]"), function (t) {
          t.classList.toggle("hidden", !r.ok);
        });
      })
      .catch(function () { /* не админ или сеть — остаётся «Оформление» */ });
  }

  function showAdminMsg(text, isErr) {
    var m = $("admin-msg");
    m.textContent = (isErr ? "⚠️ " : "✓ ") + text;
    m.style.display = "block";
    m.style.color = isErr ? "var(--danger)" : "";
    clearTimeout(m._t);
    m._t = setTimeout(function () { m.style.display = "none"; }, 4500);
  }

  function renderList(boxId, items, kind) {
    var box = $(boxId);
    box.innerHTML = "";
    if (!items.length) {
      var d = document.createElement("div");
      d.className = "empty-note";
      d.textContent = kind === "fin"
        ? "Финансисты не заданы — карточки заявок никому не уходят."
        : kind === "adm"
        ? "Админов нет — задайте ADMIN_IDS в .env на сервере."
        : "Список пуст — подача закрыта для всех, кроме админов (fail-closed).";
      box.appendChild(d);
      return;
    }
    items.forEach(function (it, i) {
      var row = document.createElement("div");
      row.className = "row-item";
      row.style.animationDelay = (i * 0.03) + "s";
      var who = document.createElement("div");
      who.className = "who";
      var main = document.createElement("div");
      main.textContent = kind === "fin" ? it.entry : (it.username || ("id " + it.id));
      who.appendChild(main);
      if (kind !== "fin" && it.username) {
        var sub = document.createElement("div");
        sub.className = "sub";
        sub.textContent = "id " + it.id;
        who.appendChild(sub);
      }
      row.appendChild(who);
      if (it.source === "env") {
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = ".env";
        tag.title = "Задано на сервере";
        row.appendChild(tag);
      }
      // Удалять можно и записи из .env: сам файл бот не правит, но исключает
      // такую запись из эффективного списка — иначе «убрал, а человек всё
      // ещё проходит». Возврат — обычным «добавить». Исключение — админы:
      // владелец сервера должен оставаться владельцем.
      if (kind !== "adm" || it.source !== "env") {
        var del = document.createElement("button");
        del.type = "button";
        del.className = "row-del";
        del.setAttribute("aria-label", "Удалить");
        del.textContent = "✕";
        del.addEventListener("click", function () {
          adminAction(kind, "remove", kind === "fin" ? it.entry : String(it.id));
        });
        row.appendChild(del);
      }
      box.appendChild(row);
    });
  }

  /** Показывает кнопку, если ссылка пришла, и вешает открытие во внешнем
   *  браузере. Возвращает, видима ли кнопка. */
  function wireOpenLink(id, url) {
    var btn = $(id);
    if (!btn) return false;
    btn.classList.toggle("hidden", !url);
    if (!url) return false;
    btn.onclick = function () {
      if (tg && tg.openLink) tg.openLink(url);
      else window.open(url, "_blank");
    };
    return true;
  }

  /** Кто уже пользуется ботом: роли и открыт ли доступ к подаче.
   *
   *  Отдельный список от whitelist: там видно только тех, кого вписали, а
   *  здесь — ещё и всех, кто боту писал, но доступа не получил. */
  function loadUsers() {
    fetch("/api/admin/users", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) renderUsers(d.users || []); })
      .catch(function () { /* список не критичен */ });
  }

  // Список делится по ролям: в общей куче непонятно, кто здесь кто.
  var USER_GROUPS = [
    { key: "admin", title: "Админы", hint: "Настройки и все заявки" },
    { key: "financier", title: "Финансисты", hint: "Получают карточки заявок" },
    { key: "other", title: "Остальные", hint: "Писали боту" }
  ];

  // Все, кого бот знает, — источник подсказок для полей «@username или id».
  var knownUsers = [];

  /** Поиск по уже известным боту людям — как в поиске Telegram, но честнее:
   *  Bot API не умеет проверять существование произвольного @username, зато
   *  справочник содержит ровно тех, кого бот сможет резолвить в id. */
  function wireSuggest(inputId, boxId) {
    var input = $(inputId), box = $(boxId);
    if (!input || !box) return;

    function close() { box.innerHTML = ""; box.classList.add("hidden"); }

    function match(u, q) {
      var name = (u.username || "").replace(/^@/, "").toLowerCase();
      return name.indexOf(q) !== -1 || String(u.id).indexOf(q) === 0;
    }

    input.addEventListener("input", function () {
      var q = input.value.trim().replace(/^@/, "").toLowerCase();
      close();
      if (!q) return;
      var hits = knownUsers.filter(function (u) { return match(u, q); }).slice(0, 6);
      hits.forEach(function (u) {
        var row = document.createElement("button");
        row.type = "button";
        row.className = "suggest-row";
        row.appendChild(document.createTextNode(u.username || ("id " + u.id)));
        var sub = document.createElement("span");
        sub.className = "sub";
        var roles = [];
        if (u.admin) roles.push("админ");
        if (u.financier) roles.push("финансист");
        if (u.access) roles.push("есть доступ");
        sub.textContent = "id " + u.id + (roles.length ? " · " + roles.join(", ") : "");
        row.appendChild(sub);
        // mousedown, а не click: blur поля успел бы закрыть список раньше.
        row.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          input.value = u.username || String(u.id);
          close();
          input.focus();
        });
        box.appendChild(row);
      });
      box.classList.toggle("hidden", !hits.length);
    });
    input.addEventListener("blur", function () { setTimeout(close, 120); });
  }
  ["fin", "wl", "adm"].forEach(function (who) {
    wireSuggest(who + "-input", who + "-suggest");
  });

  function renderUsers(items) {
    knownUsers = items || [];
    var box = $("users-list");
    box.innerHTML = "";
    if (!items.length) {
      var none = document.createElement("div");
      none.className = "empty-note";
      none.textContent = "Боту ещё никто не писал.";
      box.appendChild(none);
      return;
    }
    USER_GROUPS.forEach(function (g) {
      var part = items.filter(function (it) {
        return g.key === "admin" ? it.admin
             : g.key === "financier" ? (it.financier && !it.admin)
             : (!it.admin && !it.financier);
      });
      if (!part.length) return;
      var head = document.createElement("div");
      head.className = "group-head";
      head.innerHTML = "";
      head.appendChild(document.createTextNode(g.title + " · " + part.length));
      var hint = document.createElement("span");
      hint.textContent = g.hint;
      head.appendChild(hint);
      box.appendChild(head);
      part.forEach(function (it, i) { box.appendChild(userRow(it, i)); });
    });
  }

  function userRow(it, i) {
    var row = document.createElement("div");
    row.className = "row-item";
    row.style.animationDelay = (i * 0.03) + "s";

    var who = document.createElement("div");
    who.className = "who";
    var main = document.createElement("div");
    main.textContent = it.username || ("id " + it.id);
    who.appendChild(main);
    var sub = document.createElement("div");
    sub.className = "sub";
    // Строка — про этого человека, и только. Общее «whitelist пуст» стояло
    // в каждой строке и читалось как их личный статус; сам факт и так виден
    // в карточке доступа выше, когда список пуст.
    var state = it.access ? "может подавать заявки"
      : it.admin ? "подаёт заявки как админ"
      : "не может подавать заявки";
    sub.textContent = (it.username ? "id " + it.id + " · " : "") + state;
    who.appendChild(sub);
    row.appendChild(who);

    if (it.access === "env" || it.admin_source === "env") {
      var tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = ".env";
      tag.title = "Задано на сервере; отзыв здесь действует как исключение";
      row.appendChild(tag);
    }
    return row;
  }
  $("users-reload").addEventListener("click", loadUsers);

  function loadAdminSettings() {
    fetch("/api/admin/settings", { headers: { "X-Telegram-Init-Data": tg.initData } })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        renderList("fin-list", d.financiers || [], "fin");
        renderList("adm-list", d.admins || [], "adm");
        renderList("wl-list", d.allowed || [], "wl");
        if (d.backup) fillBackup(d.backup);
        if (d.reminders) fillReminders(d.reminders);
        setSeg("autofill-seg", d.autofill === false ? "off" : "on");
        // Google-режим: прямые ссылки на живую таблицу и на папку Диска с
        // файлами счетов; локальный режим — обе кнопки прячем, остаётся /export.
        var hasSheet = wireOpenLink("open-sheet", d.registry_url);
        var hasDrive = wireOpenLink("open-drive", d.drive_url);
        $("registry-note").classList.toggle("hidden", hasSheet || hasDrive);
      })
      .catch(function () { showAdminMsg("Не удалось загрузить настройки.", true); });
  }

  // --- Настройки бэкапа --------------------------------------------------------
  var backupEnabled = true;
  function fillBackup(cfg) {
    backupEnabled = !!cfg.enabled;
    setSeg("backup-seg", backupEnabled ? "on" : "off");
    $("backup-time").value = cfg.time || "03:30";
    $("backup-keep").value = cfg.keep || 7;
    $("backup-opts").style.opacity = backupEnabled ? "" : ".45";
  }
  $("backup-seg").addEventListener("click", function (ev) {
    var btn = ev.target.closest("button");
    if (!btn) return;
    this.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    backupEnabled = btn.dataset.value === "on";
    $("backup-opts").style.opacity = backupEnabled ? "" : ".45";
    if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
  });
  function backupRequest(body, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/admin/backup", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showAdminMsg(d.message || d.detail || "Готово.", !r.ok);
          if (r.ok && d.backup) fillBackup(d.backup);
          if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "error");
          }
        });
      })
      .catch(function () { showAdminMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }
  // --- Настройки напоминаний ---------------------------------------------------
  var remEnabled = true;
  var remOverdue = true;
  var remTarget = "admins";

  function fillReminders(cfg) {
    remEnabled = !!cfg.enabled;
    remOverdue = !!cfg.overdue_enabled;
    remTarget = cfg.overdue_to || "admins";
    setSeg("rem-seg", remEnabled ? "on" : "off");
    setSeg("rem-overdue-seg", remOverdue ? "on" : "off");
    setSeg("rem-target-seg", remTarget);
    $("rem-time").value = cfg.time || "09:30";
    $("rem-days").value = cfg.days_before === undefined ? 1 : cfg.days_before;
    $("rem-opts").style.opacity = remEnabled ? "" : ".45";
  }

  bindFilterSeg("rem-seg", function (v) {
    remEnabled = v === "on";
    $("rem-opts").style.opacity = remEnabled ? "" : ".45";
  });
  bindFilterSeg("rem-overdue-seg", function (v) { remOverdue = v === "on"; });
  bindFilterSeg("rem-target-seg", function (v) { remTarget = v || "admins"; });

  function reminderRequest(body, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/admin/reminders", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showAdminMsg(d.message || d.detail || "Готово.", !r.ok);
          if (r.ok && d.reminders) fillReminders(d.reminders);
          if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "error");
          }
        });
      })
      .catch(function () { showAdminMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  bindFilterSeg("autofill-seg", function (v) {
    fetch("/api/admin/autofill", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ enabled: v === "on" })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showAdminMsg(d.message || d.detail || "Готово.", !r.ok);
        });
      })
      .catch(function () { showAdminMsg("Сеть недоступна.", true); });
  });

  $("rem-save").addEventListener("click", function () {
    reminderRequest({
      action: "save",
      enabled: remEnabled,
      time: $("rem-time").value,
      days_before: $("rem-days").value,
      overdue_enabled: remOverdue,
      overdue_to: remTarget
    }, this);
  });
  $("rem-run").addEventListener("click", function () {
    reminderRequest({ action: "run" }, this);
  });

  $("backup-save").addEventListener("click", function () {
    backupRequest({
      action: "save",
      enabled: backupEnabled,
      time: $("backup-time").value,
      keep: $("backup-keep").value
    }, this);
  });
  $("backup-run").addEventListener("click", function () {
    backupRequest({ action: "run" }, this);
  });

  function adminAction(kind, action, entry) {
    var url = kind === "fin" ? "/api/admin/financiers"
            : kind === "adm" ? "/api/admin/admins"
            : "/api/admin/allowed";
    fetch(url, {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": tg.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ action: action, entry: entry })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { ok: r.ok, d: d };
        });
      })
      .then(function (res) {
        if (!res.ok) { showAdminMsg(res.d.detail || "Ошибка запроса.", true); return; }
        showAdminMsg(res.d.message || "Готово.", !res.d.ok);
        if (tg && tg.HapticFeedback) {
          tg.HapticFeedback.notificationOccurred(res.d.ok ? "success" : "warning");
        }
        loadAdminSettings();
        loadUsers();
      })
      .catch(function () { showAdminMsg("Сеть недоступна.", true); });
  }

  function bindAdd(btnId, inputId, kind) {
    function doAdd() {
      var input = $(inputId);
      var v = input.value.trim();
      if (!v) return;
      input.value = "";
      adminAction(kind, "add", v);
    }
    $(btnId).addEventListener("click", doAdd);
    $(inputId).addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") { ev.preventDefault(); doAdd(); }
    });
  }
  bindAdd("fin-add", "fin-input", "fin");
  bindAdd("wl-add", "wl-input", "wl");
  bindAdd("adm-add", "adm-input", "adm");


  /** Вкладки настроек: раньше шесть карточек лежали одной простынёй и
   *  нужное приходилось искать прокруткой. Панели живут в разметке, тут —
   *  только переключение видимости и aria-состояние. */
  function showAdminTab(name) {
    var tabs = $("admin-tabs");
    if (!tabs) return;
    [].forEach.call(tabs.querySelectorAll(".tab"), function (t) {
      t.setAttribute("aria-selected", t.dataset.pane === name ? "true" : "false");
    });
    [].forEach.call(document.querySelectorAll("#admin-view .pane"), function (p) {
      p.classList.toggle("hidden", p.id !== "pane-" + name);
    });
    window.scrollTo(0, 0);
  }
  if ($("admin-tabs")) {
    $("admin-tabs").addEventListener("click", function (ev) {
      var t = ev.target.closest(".tab");
      if (!t) return;
      showAdminTab(t.dataset.pane);
      if (tg && tg.HapticFeedback) tg.HapticFeedback.selectionChanged();
    });
  }

  function openAdmin() {
    $("form-view").style.display = "none";
    $("admin-view").classList.remove("hidden");
    // Открываем первую доступную вкладку: админу — «Финансисты», всем
    // остальным — «Оформление», единственную, которая им видна.
    var first = $("admin-tabs").querySelector(".tab:not(.hidden)");
    showAdminTab(first ? first.dataset.pane : "skin");
    markSkinChoice();
    setSeg("skin-anim-seg", skinAnimation ? "on" : "off");
    if (tg && insideTelegram) {
      tg.MainButton.hide();
      tg.BackButton.onClick(closeAdmin);
      tg.BackButton.show();
    }
    // Не админ — сервер ответит 403, и незачем показывать ему ошибку.
    if (isBotAdmin) { loadAdminSettings(); loadUsers(); }
  }
  function closeAdmin() {
    $("admin-view").classList.add("hidden");
    $("form-view").style.display = "";
    if (tg && insideTelegram) {
      tg.BackButton.hide();
      tg.BackButton.offClick(closeAdmin);
      refreshMainButton();
    }
    // В настройках могли поменять список финансистов — в том числе себя.
    tryFinance();
  }
  $("admin-btn").addEventListener("click", openAdmin);

  function openHelp() {
    $("form-view").style.display = "none";
    $("help-view").classList.remove("hidden");
    window.scrollTo(0, 0);
    if (tg && insideTelegram) {
      tg.MainButton.hide();
      tg.BackButton.onClick(closeHelp);
      tg.BackButton.show();
    }
  }
  function closeHelp() {
    $("help-view").classList.add("hidden");
    $("form-view").style.display = "";
    if (tg && insideTelegram) {
      tg.BackButton.hide();
      tg.BackButton.offClick(closeHelp);
      refreshMainButton();
    }
  }
  $("help-btn").addEventListener("click", openHelp);
  $("help-close").addEventListener("click", closeHelp);

  if (tg && insideTelegram) tg.MainButton.onClick(function () { submit(false); });
  $("submit-fallback").addEventListener("click", function () { submit(false); });
  restoreDraft();
  layoutHeaderIcons();
  tryAdmin();
  tryFinance();
  refreshMainButton();

  // Открытие сразу на инструкции: кнопка «Инструкция» в группе/канале
  // (прямая ссылка ?startapp=help) или web_app-кнопка в личке (?help=1).
  var query = new URLSearchParams(location.search);
  var startParam = (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) || "";
  if (startParam === "help" || query.get("help") === "1") {
    openHelp();
  }

  // Кнопка «↻ Повторить» из списка /my в чате открывает форму с полями
  // прошлой заявки: ?repeat=<id> (web_app-кнопка в личке).
  var repeatId = query.get("repeat") ||
    (startParam.indexOf("repeat_") === 0 ? startParam.slice(7) : "");
  if (repeatId) applyRepeatById(repeatId);
})();
