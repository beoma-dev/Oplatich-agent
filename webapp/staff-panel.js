/* Справочник сотрудников: ФИО ↔ Telegram-аккаунт.
 *
 * Список живёт в БД бота и правится здесь — найм и увольнение не должны
 * требовать доступа к серверу. Google-таблица только источник для импорта
 * пачкой: кнопка «Импорт» вливает её и НИЧЕГО не удаляет, а кого в таблице
 * нет — показывает списком, чтобы админ решил сам.
 *
 * Зачем всё это: в реестр попадает подтверждённое ФИО, а не имя из профиля
 * Telegram — его человек меняет когда захочет, и в отчёте вместо «Микула
 * Татьяна» оказывается «Танечка 🌸».
 */
function buildStaffPanel(ctx) {
  var $ = ctx.$;
  var box = $("staff-list");
  var note = $("staff-note");

  function say(text, isErr) {
    note.textContent = (isErr ? "⚠️ " : "") + text;
    note.style.display = text ? "block" : "none";
    note.classList.toggle("staff-err", !!isErr);
  }

  /** «Сотрудников: 22» — сколько всего в справочнике. Число полезно само
   *  по себе (столько же людей в компании?) и как проверка после импорта. */
  function count(items) {
    var el = $("staff-count");
    el.textContent = items.length ? "Сотрудников: " + items.length : "";
    // Признак «подавал» считает сервер по аудиту. Раньше здесь стояло
    // «есть ли числовой id», а он проставляется при подаче — и справочник
    // уверял, что не подавал никто, включая тех, у кого заявки есть.
    var idle = items.filter(function (i) { return !i.submitted; }).length;
    if (items.length && idle) {
      el.textContent += " · не подавали заявок: " + idle;
    }
  }

  function render(items) {
    count(items);
    box.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("div");
      empty.className = "empty-note";
      empty.textContent = "Справочник пуст — в реестр пойдут имена из профилей Telegram.";
      box.appendChild(empty);
      return;
    }
    items.forEach(function (it, i) {
      var row = document.createElement("div");
      row.className = "row-item";
      row.style.animationDelay = (i * 0.02) + "s";
      // Имя и аккаунт В ОДНУ строку: со списком в два десятка человек
      // двухэтажные строки растягивали карточку на полтора экрана.
      // Числовой id ушёл в подсказку — админу он нужен редко, а место
      // занимал у каждого. Сколько человек ещё не подавали заявок,
      // сказано числом над списком.
      var who = document.createElement("div");
      who.className = "who";
      var main = document.createElement("div");
      main.className = "staff-name";
      main.textContent = it.full_name;
      who.appendChild(main);
      var sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = it.username ? "@" + it.username : "без аккаунта";
      if (it.tg_id) sub.title = "id " + it.tg_id;
      who.appendChild(sub);
      row.appendChild(who);

      var del = document.createElement("button");
      del.type = "button";
      del.className = "row-del";
      del.textContent = "✕";
      del.title = "Убрать из справочника";
      del.addEventListener("click", function () {
        ctx.confirm(
          "🗑 Убрать из справочника",
          "«" + it.full_name + "» больше не будет подставляться в заявки. "
          + "Уже поданные заявки не меняются — там останется то ФИО, "
          + "под которым их подавали.",
          "Убрать",
          function () { send("/api/admin/staff/remove", { id: it.id }); },
          true
        );
      });
      row.appendChild(del);
      box.appendChild(row);
    });
  }

  function send(url, body) {
    say("Сохраняю…");
    fetch(url, {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": ctx.initData(),
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body || {})
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) { say(d.detail || "Не получилось.", true); return; }
          render(d.items || []);
          if (d.changed !== undefined) {
            say(d.changed
              ? "Обновлено строк реестра: " + d.changed
                + (d.examples.length ? " (" + d.examples.join(", ") + ")" : "")
              : "В реестре и так везде ФИО из справочника.");
          } else if (d.added !== undefined) {
            var tail = d.not_in_sheet && d.not_in_sheet.length
              ? " В таблице нет: " + d.not_in_sheet.join(", ") + " — удалить можно вручную."
              : "";
            say("Импорт: добавлено " + d.added + ", обновлено " + d.updated + "." + tail);
          } else {
            say("");
          }
        });
      })
      .catch(function () { say("Сеть недоступна.", true); });
  }

  function load() {
    fetch("/api/admin/staff", { headers: { "X-Telegram-Init-Data": ctx.initData() } })
      .then(function (r) { return r.ok ? r.json() : { items: [] }; })
      .then(function (d) {
        render(d.items || []);
        $("staff-import").classList.toggle("hidden", !d.source);
      })
      .catch(function () { /* не админ или сеть — карточка просто пустая */ });
  }

  $("staff-add").addEventListener("click", function () {
    var name = $("staff-name").value.trim();
    var user = $("staff-user").value.trim();
    if (!name || !user) { say("Нужны и ФИО, и аккаунт.", true); return; }
    send("/api/admin/staff", { full_name: name, username: user });
    $("staff-name").value = "";
    $("staff-user").value = "";
  });
  $("staff-fix").addEventListener("click", function () {
    ctx.confirm(
      "✍️ Обновить ФИО в реестре",
      "Заявки, поданные до появления справочника, подписаны именем из "
      + "профиля Telegram. Кому в справочнике задано ФИО — у того оно "
      + "будет проставлено и в уже записанных строках. Больше ничего "
      + "в реестре не меняется.",
      "Обновить",
      function () { send("/api/admin/staff/backfill"); }
    );
  });
  $("staff-import").addEventListener("click", function () {
    ctx.confirm(
      "📥 Импорт из таблицы",
      "Список из Google-таблицы будет влит в справочник: новые добавятся, "
      + "у знакомых обновится ФИО. Ничего не удалится.",
      "Импортировать",
      function () { send("/api/admin/staff/import"); }
    );
  });

  return { reload: load };
}
