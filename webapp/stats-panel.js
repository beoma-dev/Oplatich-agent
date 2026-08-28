/* Сводка для админа: кто пользуется, где застревает, что с документами.
 *
 * Экран, а не вкладка настроек: вкладок там уже шесть и в строку они влезают
 * впритык, а тут не настройка, а отдельное чтение — как «Мои заявки».
 *
 * Считает всё СЕРВЕР (services/analytics): здесь только раскладка чисел.
 * Соблазн посчитать долю-другую на месте велик, но тогда одна и та же
 * метрика начинает жить в двух местах и однажды разойдётся.
 *
 * Полоски — статическая ширина в процентах, без анимации: это таблица,
 * а не спектакль, и на длинном списке анимация только мешает читать.
 */
function buildStatsPanel(ctx) {
  var $ = ctx.$;
  var btn = $("stats-btn");
  var view = $("stats-view");
  var body = $("stats-body");
  var days = 30;
  var busy = false;

  function money(sums) {
    var out = [];
    for (var cur in sums) {
      if (!Object.prototype.hasOwnProperty.call(sums, cur)) continue;
      var num = Number(sums[cur]);
      out.push((isFinite(num) ? formatAmount(num) : sums[cur]) + " " + cur);
    }
    return out.length ? out.join(" · ") : "—";
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function card(title) {
    var box = el("div", "card");
    box.appendChild(el("div", "card-title", title));
    body.appendChild(box);
    return box;
  }

  /** Крупное число с подписью. hint — пояснение мелким, если нужно. */
  function stat(box, label, value, hint) {
    var row = el("div", "st-stat");
    row.appendChild(el("span", "st-label", label));
    var right = el("span", "st-value", String(value));
    row.appendChild(right);
    box.appendChild(row);
    if (hint) box.appendChild(el("div", "st-hint", hint));
    return row;
  }

  /** Список с полосками: доля считается от самого частого. */
  function bars(box, items, empty) {
    if (!items.length) { box.appendChild(el("div", "st-hint", empty)); return; }
    var top = Math.max.apply(null, items.map(function (i) { return i.count; }));
    items.forEach(function (item) {
      var row = el("div", "st-bar");
      var head = el("div", "st-bar-head");
      head.appendChild(el("span", "st-bar-name", item.name));
      head.appendChild(el("b", null, String(item.count)));
      row.appendChild(head);
      var track = el("div", "st-track");
      var fill = el("i");
      fill.style.width = Math.round((item.count / top) * 100) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      if (item.sums) row.appendChild(el("div", "st-hint", money(item.sums)));
      box.appendChild(row);
    });
  }

  function render(d) {
    body.innerHTML = "";

    var who = card("👥 Кто пользуется");
    stat(who, "Авторов за период", d.people.authors_period);
    stat(who, "Авторов за всё время", d.people.authors_ever);
    who.appendChild(el("div", "st-sub", "Кто подаёт чаще"));
    bars(who, d.people.top, "За период заявок не было.");
    if (d.people.idle.length) {
      who.appendChild(el("div", "st-sub", "Доступ есть, заявок нет"));
      who.appendChild(el("div", "st-hint", d.people.idle.join(", ")));
    }

    var flow = card("⏱ Сроки и деньги");
    stat(flow, "Заявок за период", d.flow.period_count, money(d.flow.period_sums));
    stat(flow, "Всего в реестре", d.flow.total_count);
    stat(flow, "Подача → оплата, медиана",
      d.flow.median_days === null ? "—" : d.flow.median_days + " дн.",
      d.flow.median_days === null
        ? "Ещё нечего мерить: ни одной оплаты в журнале."
        : "По " + d.flow.paid_measured + " оплаченным.");
    var slow = stat(flow, "Ждут дольше " + d.flow.waiting_after_days + " дн.",
      d.flow.waiting_long);
    if (d.flow.waiting_long) slow.classList.add("st-warn");
    var late = stat(flow, "Просрочено сейчас", d.flow.overdue_now,
      d.flow.overdue_now ? money(d.flow.overdue_sums) : "");
    if (d.flow.overdue_now) late.classList.add("st-warn");

    var art = card("📂 Статьи расходов");
    bars(art, d.flow.articles, "За период заявок не было.");

    var docs = card("📎 Документы");
    var nd = stat(docs, "Без счёта и реквизитов", d.docs.no_docs);
    if (d.docs.no_docs) nd.classList.add("st-warn");
    var nc = stat(docs, "Оплачено без акта / УПД", d.docs.paid_without_closing,
      "Всего оплачено: " + d.docs.paid_total + ".");
    if (d.docs.paid_without_closing) nc.classList.add("st-warn");

    var st = card("🔖 Статусы");
    var rows = [];
    for (var name in d.flow.statuses) {
      if (Object.prototype.hasOwnProperty.call(d.flow.statuses, name)) {
        rows.push({ name: name, count: d.flow.statuses[name] });
      }
    }
    rows.sort(function (a, b) { return b.count - a.count; });
    bars(st, rows, "Заявок пока нет.");
  }

  function load() {
    if (busy) return;
    busy = true;
    $("stats-msg").textContent = "Считаю…";
    $("stats-msg").style.display = "block";
    fetch("/api/admin/analytics?days=" + days, {
      headers: { "X-Telegram-Init-Data": ctx.initData() }
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          if (!r.ok) {
            $("stats-msg").textContent = "⚠️ " + (d.detail || "Не удалось посчитать.");
            return;
          }
          $("stats-msg").style.display = "none";
          render(d);
        });
      })
      .catch(function () { $("stats-msg").textContent = "⚠️ Сеть недоступна."; })
      .then(function () { busy = false; });
  }

  function open() {
    $("form-view").style.display = "none";
    view.classList.remove("hidden");
    window.scrollTo(0, 0);
    if (ctx.tg() && ctx.inside()) {
      ctx.tg().MainButton.hide();
      ctx.tg().BackButton.onClick(close);
      ctx.tg().BackButton.show();
    }
    load();
  }

  function close() {
    view.classList.add("hidden");
    $("form-view").style.display = "";
    if (ctx.tg() && ctx.inside()) {
      ctx.tg().BackButton.hide();
      ctx.tg().BackButton.offClick(close);
      ctx.refreshMain();
    }
  }

  btn.addEventListener("click", open);
  $("stats-close").addEventListener("click", close);
  $("stats-reload").addEventListener("click", load);
  $("stats-days").addEventListener("click", function (ev) {
    var pick = ev.target.closest("button");
    if (!pick || pick.classList.contains("active")) return;
    [].forEach.call(this.querySelectorAll("button"), function (b) {
      b.classList.remove("active");
    });
    pick.classList.add("active");
    days = Number(pick.dataset.value) || 30;
    load();
  });

  return {
    setAdmin: function (isAdmin) {
      if (btn.classList.contains("hidden") === !isAdmin) return;
      btn.classList.toggle("hidden", !isAdmin);
      if (!isAdmin && !view.classList.contains("hidden")) close();
      ctx.layout();
    }
  };
}
