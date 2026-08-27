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
