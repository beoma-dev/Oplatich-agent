/* Чаты, куда уходит сводка о каждой заявке.
 *
 * До 08.09.2026 сводка попадала в группу, ТОЛЬКО если человек открыл форму по
 * ссылке из неё: подал того же бота из лички — в группе тихо. Получалось, что
 * увидит группа заявку или нет, решал маршрут пользователя, а не решение
 * админа. Здесь адреса задаются один раз и работают для всех заявок.
 *
 * Выключателя нет намеренно: пустой список и есть «не слать». Два способа
 * выключить одно и то же рано или поздно разойдутся — один выключен, второй
 * забыт, и почему тихо, непонятно.
 *
 * Своим файлом, как остальные панели: app.js у своего потолка строк.
 */
function buildGroupPanel(ctx) {
  var $ = ctx.$;
  if (!$("sum-list")) return null;

  function fill(chats) {
    var box = $("sum-list");
    box.textContent = "";
    if (!chats || !chats.length) {
      var empty = document.createElement("div");
      empty.className = "counter";
      empty.style.textAlign = "left";
      empty.textContent = "Список пуст — сводки никуда не идут.";
      box.appendChild(empty);
      return;
    }
    chats.forEach(function (chatId) {
      var row = document.createElement("div");
      row.className = "row-item";
      var name = document.createElement("span");
      name.style.flex = "1";
      name.textContent = String(chatId);
      var kill = document.createElement("button");
      kill.type = "button";
      kill.className = "add-btn btn-ghost";
      kill.textContent = "Убрать";
      kill.addEventListener("click", function () {
        save({ action: "remove", entry: String(chatId) });
      });
      row.appendChild(name);
      row.appendChild(kill);
      box.appendChild(row);
    });
  }

  function save(body) {
    return fetch("/api/admin/summary-chats", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": ctx.initData,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; })
          .then(function (d) { return { ok: r.ok, d: d }; });
      })
      .then(function (res) {
        if (!res.ok) {
          ctx.showError(res.d.detail || "Не удалось сохранить.");
          return;
        }
        fill(res.d.summary_chats);
        $("sum-input").value = "";
        if (ctx.tg && ctx.tg.HapticFeedback) {
          ctx.tg.HapticFeedback.notificationOccurred("success");
        }
      })
      .catch(function () { ctx.showError("Сеть недоступна."); });
  }

  $("sum-add").addEventListener("click", function () {
    var value = $("sum-input").value.trim();
    if (value) save({ action: "add", entry: value });
  });

  // Состояние приезжает вместе с остальными настройками админа
  // (GET /api/admin/settings) — своего запроса панель не делает.
  return { fill: fill };
}
