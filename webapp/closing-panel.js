/* Закрывающие документы: акт, УПД, накладная к УЖЕ поданной заявке.
 *
 * Приходят после оплаты, иногда через месяц, поэтому кнопка живёт в списке
 * «Мои заявки» — там, где человек находит свой платёж, — а не в форме новой
 * заявки. Документы дописываются в существующую строку реестра.
 *
 * Отдельным файлом по той же причине, что и прочие панели: app.js у своего
 * потолка. Наружу отдаёт одну функцию — «добавь кнопку в этот ряд действий».
 */
function closingButton(actions, item, ctx) {
  // Закрывающие приходят ПОСЛЕ оплаты, поэтому на неоплаченной заявке
  // кнопка только занимала место в ряду. Уже приложенные показываем при
  // любом статусе: иначе к ним не вернуться, если статус потом поменяли.
  if (item.status !== "Оплачена" && !Number(item.closing_count || 0)) return;

  var btn = document.createElement("button");
  btn.type = "button";
  // Главное действие карточки — заливкой: в ряду одинаковых
  // призрачных кнопок глазу не за что зацепиться.
  btn.className = "add-btn";
  var already = Number(item.closing_count || 0);
  btn.textContent = "📄 Акт / УПД" + (already ? " (" + already + ")" : "");
  btn.title = "Приложить акт, УПД или накладную к этой заявке";

  var input = document.createElement("input");
  input.type = "file";
  input.multiple = true;
  input.className = "hidden";
  input.accept = ".pdf,.jpg,.jpeg,.png,.xlsx,.xls,application/pdf,image/jpeg,image/png";

  btn.addEventListener("click", function () { input.click(); });
  input.addEventListener("change", function () {
    var picked = [].slice.call(this.files || []);
    this.value = "";
    if (!picked.length) return;

    var body = new FormData();
    body.append("request_id", item.id);
    picked.forEach(function (f) { body.append("files", f, f.name); });
    btn.disabled = true;
    btn.style.opacity = ".6";
    btn.textContent = "⏳ Отправляю…";
    fetch("/api/my/closing-docs", {
      method: "POST",
      headers: { "X-Telegram-Init-Data": ctx.initData },
      body: body
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          // detail у ошибок валидации — список объектов, а не строка.
          var text = typeof d.detail === "string" ? d.detail
            : (Array.isArray(d.detail) && d.detail.length
                ? d.detail.map(function (x) { return (x && x.msg) || String(x); }).join("; ")
                : null);
          ctx.showMsg(d.message || text || "Не удалось приложить документы.",
                      !(r.ok && d.ok));
          if (r.ok && d.ok && ctx.reload) ctx.reload();
        });
      })
      .catch(function () { ctx.showMsg("Сеть недоступна.", true); })
      .then(function () {
        btn.disabled = false;
        btn.style.opacity = "";
        btn.textContent = "📄 Акт / УПД" + (already ? " (" + already + ")" : "");
      });
  });

  actions.appendChild(btn);
  actions.appendChild(input);
}
