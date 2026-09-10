/* Сводки о заявках в общий чат: адрес задаёт админ, а не путь пользователя.
 *
 * До 08.09.2026 сводка попадала в группу, ТОЛЬКО если человек открыл форму по
 * ссылке из неё: подал того же бота из лички — в группе тихо. Получалось, что
 * увидит группа заявку или нет, решал маршрут пользователя, а не решение
 * админа. Здесь чат задаётся один раз и работает для всех заявок.
 *
 * Своим файлом, как остальные панели: app.js у своего потолка строк, и
 * очередная несвязанная тема там его переполняет.
 */
function buildGroupPanel(ctx) {
  var $ = ctx.$;
  if (!$("group-sum-seg")) return null;

  function fill(cfg) {
    ctx.setSeg("group-sum-seg", cfg.enabled ? "on" : "off");
    $("group-sum-input").value = cfg.chat_id ? String(cfg.chat_id) : "";
    var note = $("group-sum-note");
    // Три разных состояния, и молчать ни об одном нельзя: «выключено» и «чат
    // не задан» выглядят одинаково, а means разное — во втором случае
    // включение ничего не даст.
    if (!cfg.chat_id) {
      note.textContent = "Чат не задан — сводки никуда не идут.";
    } else if (cfg.enabled) {
      note.textContent = "Итог каждой заявки уходит в чат " + cfg.chat_id + ".";
    } else {
      note.textContent = "Выключено. Чат " + cfg.chat_id + " сохранён.";
    }
  }

  function save(body) {
    return fetch("/api/admin/group-summary", {
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
        fill(res.d.group_summary);
        if (ctx.tg && ctx.tg.HapticFeedback) {
          ctx.tg.HapticFeedback.notificationOccurred("success");
        }
      })
      .catch(function () { ctx.showError("Сеть недоступна."); });
  }

  ctx.bindSeg("group-sum-seg", function (v) { save({ enabled: v === "on" }); });
  $("group-sum-save").addEventListener("click", function () {
    save({ chat_id: $("group-sum-input").value.trim() });
  });

  // Состояние приезжает вместе с остальными настройками админа
  // (GET /api/admin/settings) — своего запроса панель не делает.
  return { fill: fill };
}
