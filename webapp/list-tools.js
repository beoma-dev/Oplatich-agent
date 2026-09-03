/* Мелочи вокруг списков заявок: переход по ссылке из уведомления и отклик
 * кнопки обновления.
 *
 * Отдельным файлом по той же причине, что и панели: app.js у своего потолка.
 * Обе функции чистые в том смысле, что состояние приложения не трогают —
 * им передают элементы, они возвращают набор фильтров или крутят значок.
 */

/** Значок обновления крутится, пока идёт запрос. */
function spinReload(btn, done) {
  btn.classList.add("spinning");
  var stop = function () { btn.classList.remove("spinning"); };
  // Отдельный таймер, а не только колбэк: загрузка может и не позвать назад
  // (сеть отвалилась), а вечно крутящийся значок врёт про «ещё грузится».
  setTimeout(stop, 8000);
  try { done(stop); } catch (e) { stop(); }
}

/* Что бывает после «fin_» в ссылке из уведомления:
 *   INV-…            — одна заявка: карточка, напоминание, закрывающие
 *   overdue          — все просроченные: сводка «🚨 Просрочено: N»
 *   due_ISO_ISO      — окно плановых дат: сводка «⏰ Завтра к оплате»
 * Двоеточий и запятых в startapp Telegram не пропускает — только буквы,
 * цифры, «_» и «-», отсюда подчёркивания как разделители.
 *
 * Список заявок по номерам сюда НЕ передаём: сводка собрана в свой час, а
 * человек открывает её позже, и «просроченные на сейчас» — более честная
 * выборка, чем список из вчерашнего сообщения. Заодно у панели уже есть
 * ровно эти фильтры, и «Сбросить» работает как обычно.
 */
function finLinkFilters(param, els) {
  var f = { query: "", status: "", urgency: "", from: "", to: "" };
  var parts = String(param || "").split("_");

  if (parts[0] === "overdue") {
    f.status = "__overdue__";
  } else if (parts[0] === "due" && parts.length === 3) {
    f.from = parts[1];
    f.to = parts[2];
  } else {
    f.query = param;
  }

  // Показываем фильтр в самих полях: иначе непонятно, ПОЧЕМУ список короткий,
  // и «Сбросить» выглядит кнопкой без причины.
  if (els.query) els.query.value = f.query;
  if (els.from) els.from.value = f.from;
  if (els.to) els.to.value = f.to;
  if (els.statusSeg) {
    [].forEach.call(els.statusSeg.querySelectorAll("button"), function (b) {
      b.classList.toggle("active", (b.dataset.value || "") === f.status);
    });
  }
  // Окно дат раскрываем: фильтр стоит, а свёрнутый блок его прячет.
  if (els.filters && (f.status || f.from)) els.filters.classList.remove("hidden");
  return f;
}

/** Чем платить — ТРИ случая, а не два.
 *
 * «Счёта нет» ещё не значит «есть реквизиты»: с 26.08.2026 необязательны оба,
 * и заявка бывает пустой («оплатим по договору, документы позже»). Пока веток
 * было две, строка списка обещала финансисту реквизиты, которых в заявке нет,
 * — он открывал её и не находил ничего.
 *
 * long — для окна подробностей: там формулировки полнее.
 */
function sourceMark(item, long) {
  var key = item.payment_source ||
    (item.has_invoice ? "invoice" : (item.requisites ? "requisites" : "none"));
  var words = long
    ? { invoice: "📎 файл приложен", requisites: "✍️ по реквизитам",
        none: "⚠️ ни счёта, ни реквизитов" }
    : { invoice: "📎 счёт", requisites: "✍️ реквизиты",
        // Коротко: в строке списка полная формулировка переносилась и
        // съедала лишнюю строку у КАЖДОЙ карточки. Полностью — в окне.
        none: "⚠️ без документов" };
  return words[key] || words.none;
}

/** Строка «📂 статья · 🔴 срочно · 📎 счёт» ОДНОЙ строкой.
 *
 * Статья приходит от человека и бывает какой угодно длины, поэтому она
 * в отдельном span, который сжимается и обрывается многоточием, а флаги
 * справа — нет: пропасть должна статья, а не предупреждение об отсутствии
 * счёта. Раньше вся строка переносилась и съедала лишний ряд у КАЖДОЙ
 * карточки списка.
 */
function metaRow(item) {
  var box = document.createElement("div");
  box.className = "my-meta my-line";
  var main = document.createElement("span");
  main.className = "my-line-grow";
  main.textContent = "📂 " + (item.article || "—");
  var tail = document.createElement("span");
  tail.textContent = (item.urgency === "Срочно" ? " · 🔴 срочно" : "")
    + " · " + sourceMark(item);
  box.appendChild(main);
  box.appendChild(tail);
  return box;
}

/** Строка дат: «📅 до 27.08.2026 · подана 27.08».
 *
 * Срок работ по договору отсюда убран совсем, а дата подачи — у просроченных:
 * там строка идёт полужирной («⚠️ просрочено»), становится шире и на 294 px
 * уходит на второй ряд. Оба поля целиком показывает окно подробностей —
 * оно в одном касании по карточке.
 */
function datesLine(item) {
  if (item.overdue) return "⚠️ просрочено · 📅 до " + (item.planned_date || "—");
  var out = "📅 до " + (item.planned_date || "—");
  var made = String(item.created_at || "");
  // «2026-08-27 17:31» → «27.08»: год избыточен, а дд.мм совпадает
  // по формату с плановой датой рядом.
  if (/^\d{4}-\d{2}-\d{2}/.test(made)) {
    out += " · подана " + made.slice(8, 10) + "." + made.slice(5, 7);
  }
  return out;
}

/** Куда вести по ссылке, которой открыли приложение.
 *
 * Ссылок из чата накопилось четыре, и разбор их всех — это не логика формы,
 * а таблица «параметр → экран». Живёт здесь, чтобы app.js не рос.
 *
 *   help          — экран инструкции (кнопка в группе и «❓» в личке)
 *   repeat_<id>   — форма, заполненная прошлой заявкой
 *   my_<id>       — «Мои заявки», раскрытая на этой заявке (уведомление автору)
 *   fin_<что-то>  — панель финансиста с нужной выборкой (см. finLinkFilters)
 *
 * `on` — что делать в каждом случае; отсутствующий обработчик просто
 * пропускается, поэтому экран, которого ещё нет, ничего не ломает.
 */
function applyDeepLink(initData, search, on) {
  var query = new URLSearchParams(search);
  var param = startParamFrom(initData, search);

  function after(prefix, name) {
    return query.get(name) ||
      (param.indexOf(prefix) === 0 ? param.slice(prefix.length) : "");
  }

  if ((param === "help" || query.get("help") === "1") && on.help) on.help();
  var repeat = after("repeat_", "repeat");
  if (repeat && on.repeat) on.repeat(repeat);
  var mine = after("my_", "my");
  if (mine && on.my) on.my(mine);
  var fin = after("fin_", "fin");
  if (fin && on.fin) on.fin(fin);
}
