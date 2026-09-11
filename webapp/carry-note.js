/* «Это тоже уйдёт»: что приложено и вписано, но спрятано переключателем.
 *
 * 11.09.2026 заявка ушла без счёта: человек приложил файл, распознавание
 * подставило из него реквизиты, он переключился на «Реквизиты» — и форма
 * файл не отправила, потому что смотрела на ВЫБОР, а не на вложение. Теперь
 * уходит всё приложенное и вписанное, и эта строка о скрытом говорит вслух:
 * молчание в обратную сторону было бы той же ошибкой.
 *
 * Файл следит за DOM САМ, без вызовов из app.js: мест, где меняются поле
 * реквизитов и режим, уже шесть (ввод, автозаполнение, черновик, повтор
 * заявки, сброс формы, снятие вуали), и седьмое однажды забыли бы
 * подключить — строка начала бы врать. Ориентиры выбраны те, что в форме
 * поддерживают всегда: класс блока счёта и счётчик символов реквизитов.
 */
(function () {
  function $(id) { return document.getElementById(id); }

  var box, fileInput, reqEl, fileBlock;

  function refresh() {
    var invoiceMode = !fileBlock.classList.contains("hidden");
    var file = (fileInput.files && fileInput.files[0]) || null;
    var text = null, label = null, act = null;

    if (!invoiceMode && file) {
      text = "📎 Файл «" + file.name + "» приложен и уйдёт вместе с реквизитами.";
      label = "Убрать файл";
      // Чистит app.js: там же гаснут предупреждение о файле и предложение
      // автозаполнения — своей копии этой уборки тут быть не должно.
      act = function () { $("file-remove").click(); };
    } else if (invoiceMode && reqEl.value.trim()) {
      text = "✍️ Реквизиты заполнены и уйдут вместе со счётом.";
      label = "Очистить";
      act = function () {
        reqEl.value = "";
        // Событием, а не тихой правкой: на нём висят счётчик символов,
        // проверка реквизитов и сохранение черновика.
        reqEl.dispatchEvent(new Event("input", { bubbles: true }));
      };
    }

    box.textContent = "";
    if (!text) { box.classList.add("hidden"); return; }
    box.appendChild(document.createTextNode(text));
    var btn = document.createElement("button");
    btn.type = "button";
    btn.id = "carry-undo";
    btn.textContent = label;
    // refresh после действия обязателен: очистка input-а из кода события
    // change не поднимает, и строка осталась бы висеть над пустотой.
    btn.addEventListener("click", function () { act(); refresh(); });
    box.appendChild(btn);
    box.classList.remove("hidden");
  }

  function start() {
    box = $("carry-note");
    fileInput = $("file-input");
    reqEl = $("requisites");
    fileBlock = $("file-block");
    if (!box || !fileInput || !reqEl || !fileBlock) return;

    fileInput.addEventListener("change", refresh);
    reqEl.addEventListener("input", refresh);
    // Режим виден по классу блока счёта — его переключают все шесть мест.
    new MutationObserver(refresh).observe(fileBlock, {
      attributes: true, attributeFilter: ["class"],
    });
    // А содержимое реквизитов — по счётчику символов: его в этой форме
    // обновляют везде, где меняют значение, и расхождение было бы видно
    // глазом, в отличие от забытого вызова.
    var count = $("req-count");
    if (count) {
      new MutationObserver(refresh).observe(count, {
        childList: true, characterData: true, subtree: true,
      });
    }
    refresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
