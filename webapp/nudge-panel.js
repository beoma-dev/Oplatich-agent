/* Напоминание финансистам о просрочке по конкретной заявке.
 *
 * Срок оплаты прошёл, денег нет — до сих пор автор мог только писать
 * финансисту лично, мимо бота. Кнопка живёт в «Моих заявках» рядом с той
 * заявкой, о которой речь, и появляется ТОЛЬКО у просроченной: у остальных
 * напоминать не о чем, а лишняя кнопка распирает ряд.
 *
 * Планировщик напоминаний это не дублирует: он шлёт сводку в назначенный
 * час по всем просроченным сразу, и одна заявка в списке теряется.
 *
 * Отдельным файлом по той же причине, что и прочие панели: app.js у своего
 * потолка. Наружу отдаёт одну функцию — «добавь кнопку в этот ряд».
 */
function nudgeButton(actions, item, ctx) {
  if (!item.overdue) return;

  var btn = document.createElement("button");
  btn.type = "button";
  // Главное действие карточки — заливкой: в ряду одинаковых
  // призрачных кнопок глазу не за что зацепиться.
  btn.className = "add-btn";
  btn.textContent = "⏰ Напомнить";
  btn.title = "Сообщить финансистам, что оплата по этой заявке просрочена";

  function send() {
    btn.disabled = true;
    btn.style.opacity = ".6";
    var was = btn.textContent;
    btn.textContent = "⏳ Отправляю…";
    fetch("/api/my/nudge", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": ctx.initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ request_id: item.id })
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          // detail у ошибок валидации — список объектов, а не строка.
          var text = typeof d.detail === "string" ? d.detail
            : (Array.isArray(d.detail) && d.detail.length
                ? d.detail.map(function (x) { return (x && x.msg) || String(x); }).join("; ")
                : null);
          ctx.showMsg(d.message || text || "Не удалось напомнить.", !(r.ok && d.ok));
        });
      })
      .catch(function () { ctx.showMsg("Сеть недоступна.", true); })
      .then(function () {
        btn.disabled = false;
        btn.style.opacity = "";
        btn.textContent = was;
      });
  }

  btn.addEventListener("click", function () {
    ctx.confirm(
      "⏰ Напомнить о просрочке",
      "Финансисты получат сообщение, что оплата по заявке на «" +
      (item.counterparty || "") + "» просрочена. Повторить можно будет " +
      "через шесть часов.",
      "Напомнить",
      send
    );
  });

  actions.appendChild(btn);
}
