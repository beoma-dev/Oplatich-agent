/*
 * busy-banner.js — полоса «сервис перегружен» поверх всех экранов.
 *
 * Лимит Google — 60 чтений в минуту НА ПРОЕКТ, общий на всех сразу. Упереться
 * в него может один человек (открытая аналитика, череда смен статуса), а отказ
 * увидят остальные — и по сообщению внутри одного списка этого не понять.
 * Поэтому полоса наверху: она видна на любом экране и уходит сама, как только
 * сервер снова ответил.
 *
 * Перехватываем fetch, а не правим вызовы поимённо: их два десятка, и
 * следующий добавят мимо. Ответ возвращается как есть — разбор ошибок у
 * вызывающих не меняется, полоса лишь объясняет, что происходит.
 *
 * Ставится сам: файл подключён ПОСЛЕ разметки и ДО app.js, так что первый же
 * запрос приложения идёт уже через обёртку.
 */
(function () {
  "use strict";

  var box = document.getElementById("busy-banner");
  if (!box || !window.fetch) return;

  var hideAt = 0;
  var timer = null;

  function paint() {
    var left = Math.ceil((hideAt - Date.now()) / 1000);
    if (left <= 0) {
      box.classList.add("hidden");
      if (timer !== null) { clearInterval(timer); timer = null; }
      return;
    }
    // Секунды, а не «подождите немного»: видно, что ожидание конечно.
    box.textContent = "\u23F3 Сервис перегружен — подождите " + left + " с";
  }

  function show(seconds) {
    hideAt = Math.max(hideAt, Date.now() + seconds * 1000);
    box.classList.remove("hidden");
    paint();
    if (timer === null) timer = setInterval(paint, 1000);
  }

  var real = window.fetch.bind(window);
  window.fetch = function (url, opt) {
    return real(url, opt).then(function (resp) {
      if (resp.status === 429 || resp.status === 503) {
        var head = resp.headers && resp.headers.get
          ? Number(resp.headers.get("Retry-After")) : 0;
        // Потолок 120 c: чужой Retry-After в минутах превратил бы полосу
        // в вечную. Пол — 5 c, иначе она мигнёт и человек её не заметит.
        show(Math.min(Math.max(head || 60, 5), 120));
      } else if (resp.ok && hideAt) {
        // Сервер снова отвечает — досиживать отсчёт незачем.
        hideAt = 0;
        paint();
      }
      return resp;
    });
  };
})();
