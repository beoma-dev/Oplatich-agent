/* Плашка «технические работы»: кнопка админа, которая вешает предупреждение
 * над формой у всех, кто её откроет.
 *
 * Своим файлом по той же причине, что и остальные панели: в alerts-panel.js
 * это была бы третья несвязанная тема (категории алертов, журнал инцидентов
 * и вдруг эксплуатация), а его собственный докстринг велит делить по смыслу.
 *
 * Подачу плашка НЕ блокирует — заявка всё равно уходит в реестр. Молча не
 * принять заполненную форму хуже, чем принять её во время работ.
 */
function buildMaintPanel(ctx) {
  var $ = ctx.$;
  var setSeg = ctx.setSeg;
  var bindFilterSeg = ctx.bindFilterSeg;
  var showMsg = ctx.showMsg;
  var initData = ctx.initData;
  if (!$("maint-seg")) return null;

  // --- Технические работы ---------------------------------------------------
  // Карточка живёт здесь, а не в app.js: вкладка «Здоровье» — эксплуатация,
  // и app.js у своего потолка. Состояние читаем той же ручкой, что и пишем.
  var maintOn = false;
  function fillMaint(cfg) {
    maintOn = !!(cfg && cfg.enabled);
    setSeg("maint-seg", maintOn ? "on" : "off");
    if (cfg && cfg.text) $("maint-text").value = cfg.text;
  }
  function maintRequest(body, btn) {
    if (btn) { btn.disabled = true; btn.style.opacity = ".6"; }
    return fetch("/api/admin/maintenance", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": initData(),
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .catch(function () { return { ok: false, d: { detail: "Сеть недоступна." } }; })
      .then(function (res) {
        if (btn) { btn.disabled = false; btn.style.opacity = ""; }
        if (res.d && res.d.maintenance) fillMaint(res.d.maintenance);
        return res;
      });
  }
  {
    bindFilterSeg("maint-seg", function (v) { maintOn = v === "on"; });
    $("maint-save").addEventListener("click", function () {
      maintRequest({ enabled: maintOn, text: $("maint-text").value }, this)
        .then(function (res) {
          showMsg(res.d.message || res.d.detail || "Не удалось.", !res.ok);
        });
    });
  }

  // Состояние приходит вместе с остальными настройками админа
  // (GET /api/admin/settings) — своего запроса панель не делает.
  return { fill: fillMaint };
}
