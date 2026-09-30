/* Перетаскивание файлов в форму: из проводника, с рабочего стола, из почты.
 *
 * Файл НЕ кладётся в state напрямую — он попадает в тот же <input type=file>,
 * что и при выборе через диалог, а дальше работает штатный обработчик change
 * в app.js: формат, размер, автопроверка «похоже ли на счёт», черновик,
 * кнопка отправки. Своей копии этих проверок здесь нет и быть не должно —
 * она разошлась бы с диалоговой, а зеркал в форме и так хватает.
 *
 * Отдельная причина именно так: carry-note.js смотрит в fileInput.files, а не
 * в state. Положи мы файл мимо input — строка «это тоже уйдёт» замолчала бы
 * ровно в том случае, ради которого её писали (11.09.2026).
 *
 * Модуль самодостаточен и разговаривает только с DOM: app.js наружу не отдаёт
 * ничего, и заводить ради перетаскивания первый общий объект — плохой размен.
 * Свою подсказку он рисует СВОИМ элементом: #drop-text и #drop-hint переписывает
 * app.js при каждом сбросе, и дописанное в них не пережило бы первое же
 * «Очистить форму».
 */
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var zone, fileInput, extraBlock, extraInput, badge, badgeTimer;
  var BADGE_IDLE = "или перетащите файл сюда";
  // dragleave прилетает и при переходе на дочерний элемент: без счётчика
  // вложенности подсветка мигала бы на каждой внутренней границе.
  var depth = 0;

  function dragsFiles(dt) {
    var types = (dt && dt.types) || [];
    for (var i = 0; i < types.length; i++) {
      if (types[i] === "Files") return true;
    }
    return false;
  }

  function filesOf(dt) {
    var out = [], list = (dt && dt.files) || [];
    for (var i = 0; i < list.length; i++) out.push(list[i]);
    return out;
  }

  /** Кладёт файлы в input и поднимает change — дальше всё делает app.js. */
  function handOver(input, files) {
    if (!input || !files.length) return;
    var dt;
    try {
      dt = new DataTransfer();
    } catch (err) {
      return;                      // древний WebView: остаётся выбор диалогом
    }
    for (var i = 0; i < files.length; i++) dt.items.add(files[i]);
    input.files = dt.files;
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  /** Сказать в своей подсказке и вернуть её прежний текст. */
  function say(text) {
    if (!badge) return;
    clearTimeout(badgeTimer);
    badge.textContent = text;
    badge.classList.add("drop-drag-warn");
    badgeTimer = setTimeout(function () {
      badge.textContent = BADGE_IDLE;
      badge.classList.remove("drop-drag-warn");
    }, 4000);
  }

  function paint(target) {
    var toExtras = !!(extraBlock && target && extraBlock.contains(target));
    zone.classList.toggle("drag-over", !toExtras);
    if (extraBlock) extraBlock.classList.toggle("drag-over", toExtras);
  }

  function unpaint() {
    depth = 0;
    zone.classList.remove("drag-over");
    if (extraBlock) extraBlock.classList.remove("drag-over");
  }

  /** Поле, куда человек может ронять текст, — не наше дело. */
  function editable(el) {
    return !!(el && el.closest
      && el.closest("input:not([type=file]), textarea, [contenteditable=true]"));
  }

  function onDrop(ev) {
    // Текст, брошенный в поле реквизитов, отдаём браузеру: это его работа.
    if (editable(ev.target) && !dragsFiles(ev.dataTransfer)) { unpaint(); return; }
    // А всё остальное перехватываем ВСЕГДА, даже когда это не файл: без
    // preventDefault браузер уходит по брошенной ссылке или открывает файл
    // вместо страницы, и заполненная форма исчезает вместе с черновиком.
    ev.preventDefault();
    unpaint();
    var files = filesOf(ev.dataTransfer);
    if (!files.length) {
      say("Это не файл: перетащите его из проводника, а не из окна браузера.");
      return;
    }
    if (extraBlock && ev.target && extraBlock.contains(ev.target)) {
      handOver(extraInput, files);
      return;
    }
    // Пачку раскладываем так, как её обычно и тащат: первый файл — счёт,
    // остальные — дополнительные документы. Оба списка на экране рядом,
    // поэтому куда что попало, человек видит, а не додумывает.
    handOver(fileInput, files.slice(0, 1));
    if (files.length > 1) handOver(extraInput, files.slice(1));
  }

  function start() {
    zone = $("drop-zone");
    fileInput = $("file-input");
    extraBlock = $("extra-block");
    extraInput = $("extra-input");
    if (!zone || !fileInput) return;

    // Подсказка — только там, где перетаскивание вообще возможно: на телефоне
    // из файлового менеджера в Mini App не перетащить, и строка была бы
    // обещанием, которого форма не выполнит.
    var fine = window.matchMedia
      && window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    var inner = zone.querySelector(".drop-inner");
    if (fine && inner) {
      badge = document.createElement("small");
      badge.className = "drop-drag";
      badge.textContent = BADGE_IDLE;
      inner.appendChild(badge);
    }

    window.addEventListener("dragenter", function (ev) {
      if (!dragsFiles(ev.dataTransfer)) return;
      ev.preventDefault();
      depth++;
      paint(ev.target);
    });
    window.addEventListener("dragover", function (ev) {
      if (!dragsFiles(ev.dataTransfer)) return;
      // Без preventDefault на dragover браузер не разрешает drop вовсе.
      ev.preventDefault();
      try { ev.dataTransfer.dropEffect = "copy"; } catch (err) { /* не везде */ }
      paint(ev.target);
    });
    window.addEventListener("dragleave", function () {
      if (depth > 0 && --depth === 0) unpaint();
    });
    window.addEventListener("drop", onDrop);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
