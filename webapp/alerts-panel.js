/*
 * alerts-panel.js — карточка «Здоровье бота» в настройках админа:
 * состояние связи с Telegram, выбор уведомлений о сбоях и журнал инцидентов.
 *
 * Замкнутый модуль: наружу нужен только контекст с $, setSeg, showMsg и
 * initData — всё остальное своё. Подключается ПЕРЕД app.js, который зовёт
 * buildAlertsPanel() и передаёт ему данные из /api/admin/settings.
 *
 * Смысл экрана: он открывается по HTTPS с нашего домена и работает даже
 * тогда, когда бот не достаёт до Telegram, — то есть ровно в тот момент,
 * когда уведомление о сбое прийти не может.
 */

/** Карточка уведомлений о сбоях. ctx: {$, setSeg, showMsg, initData, haptic}. */
function buildAlertsPanel(ctx) {
  var $ = ctx.$;
  var card = $("alerts-card");
  if (!card) return null;

  var state = { enabled: true, kinds: {}, catalog: [], grace: 5 };
  var REFRESH_MS = 60000;   // шаг пульса на сервере — чаще смысла нет

  // --- Строка состояния связи -------------------------------------------
  function renderState(health) {
    var box = $("alerts-state");
    var text = $("alerts-state-text");
    if (!health) { text.textContent = "Состояние связи неизвестно."; return; }
    var bad = !health.alive;
    box.classList.toggle("bad", bad);
    if (bad) {
      text.textContent = health.down_for
        ? "Связи с Telegram нет " + minutes(health.down_for) + " — уведомления не дойдут."
        : "Связи с Telegram нет — уведомления не дойдут.";
    } else if (health.last_ok_age === null || health.last_ok_age === undefined) {
      text.textContent = "Связь проверяется…";
    } else {
      text.textContent = "Связь с Telegram в норме · ответ " + ago(health.last_ok_age);
    }
  }

  /** Сверка реестра с xlsx-зеркалом. Текст готовит сервер: правило, что
   *  считать расхождением, должно быть одно и там же, где сама сверка. */
  function renderRegistry(reg) {
    var box = $("registry-state");
    var text = $("registry-state-text");
    if (!reg) { text.textContent = "Сверка реестра недоступна."; return; }
    box.classList.toggle("bad", !!reg.checked && !reg.ok);
    text.textContent = reg.text || "Сверка реестра недоступна.";
  }

  function minutes(sec) {
    var m = Math.max(1, Math.round(sec / 60));
    return m + " мин";
  }

  function ago(sec) {
    if (sec < 90) return Math.max(1, Math.round(sec)) + " с назад";
    return minutes(sec) + " назад";
  }

  /** «21:04» для сегодняшнего, «19.08, 21:04» для более старого. */
  function stamp(ts) {
    var d = new Date(ts * 1000);
    var hhmm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    var today = new Date();
    var sameDay = d.getDate() === today.getDate() && d.getMonth() === today.getMonth()
      && d.getFullYear() === today.getFullYear();
    if (sameDay) return hhmm;
    return ("0" + d.getDate()).slice(-2) + "." + ("0" + (d.getMonth() + 1)).slice(-2) + ", " + hhmm;
  }

  // --- Категории ----------------------------------------------------------
  function renderKinds() {
    var box = $("alerts-kinds");
    box.innerHTML = "";
    state.catalog.forEach(function (kind) {
      var row = document.createElement("div");
      row.className = "row-item";

      var who = document.createElement("div");
      who.className = "who";
      who.textContent = kind.title;
      row.appendChild(who);

      if (kind.critical) {
        // Потеря заявки не выключается: это молчание там, где пропали данные.
        var tag = document.createElement("span");
        tag.className = "tag";
        tag.textContent = "всегда";
        tag.title = "Критичное уведомление выключить нельзя";
        row.appendChild(tag);
        // Тумблер и здесь, но неактивный: иначе строка без переключателя
        // читается как «а эта выключена». Класс НЕ kind-toggle — по нему
        // отличают то, что можно нажать (см. tests/e2e/settings.test.cjs).
        var fixed = document.createElement("span");
        fixed.className = "kind-static on";
        fixed.title = "Критичное уведомление выключить нельзя";
        fixed.setAttribute("role", "img");
        fixed.setAttribute("aria-label", "Включено всегда");
        fixed.appendChild(makeKnob());
        row.appendChild(fixed);
      } else {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "kind-toggle";
        btn.setAttribute("role", "switch");
        btn.dataset.kind = kind.key;
        btn.appendChild(makeKnob());
        setToggle(btn, state.kinds[kind.key] !== false);
        btn.addEventListener("click", function () {
          var on = !(state.kinds[kind.key] !== false);
          state.kinds[kind.key] = on;
          setToggle(btn, on);
          if (ctx.haptic) ctx.haptic();
        });
        row.appendChild(btn);
      }
      box.appendChild(row);
    });
    dimKinds();
  }

  function makeKnob() {
    var knob = document.createElement("span");
    knob.className = "knob";
    return knob;
  }

  /** Состояние тумблера. Текст не пишем: ручку видно, а слово «Вкл» на
   *  кнопке спрашивающие читали как «нажми, чтобы включить». Подпись ушла
   *  в title и aria-label, где двусмысленности нет. */
  function setToggle(btn, on) {
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-checked", on ? "true" : "false");
    btn.title = on
      ? "Включено. Нажмите, чтобы выключить"
      : "Выключено. Нажмите, чтобы включить";
  }

  /** В режиме «только критичные» выбор категорий ни на что не влияет.
   *  Приглушить мало: приглушённое читается как «недоступно почему-то».
   *  Рядом появляется строка, объясняющая, почему тумблеры не действуют. */
  function dimKinds() {
    var muted = !state.enabled;
    $("alerts-kinds").style.opacity = muted ? ".45" : "";
    var note = $("alerts-kinds-note");
    if (note) note.classList.toggle("hidden", !muted);
  }

  function full(ts) {
    var d = new Date(ts * 1000);
    return d.toLocaleString("ru-RU", { dateStyle: "short", timeStyle: "medium" });
  }

  // Коды причин с сервера (services/alerts.py) — человеческими словами.
  var WHY = {
    "kind-off": "категория выключена в панели",
    "throttled": "повтор того же в пределах получаса",
    "no-admins": "некому — список админов пуст",
    "undelivered": "доставить не удалось, связи не было",
    "below-grace": "провал короче порога молчания"
  };

  // --- Журнал -------------------------------------------------------------
  function renderLog(items, perDay) {
    var box = $("alerts-log");
    box.innerHTML = "";
    if (!items || !items.length) {
      var note = document.createElement("div");
      note.className = "empty-note";
      note.textContent = "Сбоев не было — журнал пуст.";
      box.appendChild(note);
      return;
    }
    items.forEach(function (it) {
      var row = document.createElement("div");
      row.className = "row-item";
      var who = document.createElement("div");
      who.className = "who";
      var main = document.createElement("div");
      main.textContent = it.title + (it.count > 1 ? " ×" + it.count : "");
      who.appendChild(main);
      // Период вместо точки: «×18» без него не говорит, шло это минуту
      // или трое суток. Хвост ошибки — то, по чему видно, к кому идти.
      var span = it.first_ts && it.ts - it.first_ts > 60
        ? stamp(it.first_ts) + " → " + stamp(it.ts) : stamp(it.ts);
      var sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = span + (it.details ? " · " + it.details : "");
      if (it.details) sub.title = it.details;
      who.appendChild(sub);
      if (!it.sent) {
        // Раньше здесь стояло голое «уведомление не отправлялось», и отличить
        // «я сам выключил категорию» от «не смогли доставить» было нельзя.
        var why = document.createElement("div");
        why.className = "sub";
        why.textContent = "не отправлялось: "
          + (WHY[it.reason] || (it.reason ? it.reason : "запись старого формата"));
        who.appendChild(why);
      }
      // Строка раскрывается: в свёрнутом виде — то, что читают глазами,
      // в развёрнутом — всё, что записано, включая полный текст ошибки.
      var more = document.createElement("div");
      more.className = "sub hidden";
      more.style.whiteSpace = "pre-line";
      more.textContent = [
        "категория: " + (it.kind || "—"),
        "первый раз: " + full(it.first_ts || it.ts),
        "последний: " + full(it.ts),
        "повторов: " + (it.count || 1),
        it.details ? "подробности: " + it.details : "подробностей не записано"
      ].join("\n");
      who.appendChild(more);
      row.style.cursor = "pointer";
      row.title = "Нажмите, чтобы раскрыть";
      row.addEventListener("click", function () {
        more.classList.toggle("hidden");
        if (ctx.haptic) ctx.haptic();
      });
      row.appendChild(who);
      box.appendChild(row);
    });
    if (perDay) {
      var total = document.createElement("div");
      total.className = "fin-count";
      total.textContent = "За сутки: " + perDay;
      box.appendChild(total);
    }
  }

  // --- Обмен с сервером ---------------------------------------------------
  function post(body, btn) {
    if (btn) { btn.disabled = true; btn.style.opacity = ".6"; }
    return fetch("/api/admin/alerts", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": ctx.initData(),
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          return { ok: r.ok, data: d };
        });
      })
      .catch(function () { return { ok: false, data: {} }; })
      .then(function (res) {
        if (btn) { btn.disabled = false; btn.style.opacity = ""; }
        return res;
      });
  }

  function refresh() {
    // Пока карточка не на экране, дёргать сервер незачем.
    if (!card.offsetParent) return;
    post({ action: "status" }).then(function (res) {
      if (!res.ok) return;
      renderState(res.data.health);
      renderRegistry(res.data.registry);
      renderLog(res.data.incidents, res.data.incidents_day);
    });
  }

  // --- Заполнение из /api/admin/settings ---------------------------------
  function fill(d) {
    var cfg = d.alerts || {};
    state.enabled = cfg.enabled !== false;
    state.kinds = cfg.kinds || {};
    state.grace = cfg.link_grace_min || 5;
    state.catalog = d.alert_kinds || [];
    ctx.setSeg("alerts-mode", state.enabled ? "on" : "off");
    $("alerts-grace").value = state.grace;
    renderKinds();
    renderState(d.health);
    renderRegistry(d.registry);
    renderLog(d.incidents, d.incidents_day);
  }

  $("alerts-mode").addEventListener("click", function (ev) {
    var btn = ev.target.closest("button");
    if (!btn) return;
    this.querySelectorAll("button").forEach(function (b) { b.classList.remove("active"); });
    btn.classList.add("active");
    state.enabled = btn.dataset.value === "on";
    dimKinds();
    if (ctx.haptic) ctx.haptic();
  });

  $("alerts-save").addEventListener("click", function () {
    post({
      action: "save",
      enabled: state.enabled,
      kinds: state.kinds,
      link_grace_min: $("alerts-grace").value
    }, this).then(function (res) {
      ctx.showMsg(res.data.message || res.data.detail || "Не удалось сохранить.", !res.ok);
      if (res.ok && res.data.alerts) {
        state.kinds = res.data.alerts.kinds || state.kinds;
        state.grace = res.data.alerts.link_grace_min;
        $("alerts-grace").value = state.grace;
        renderKinds();
      }
    });
  });

  $("alerts-test").addEventListener("click", function () {
    post({ action: "test" }, this).then(function (res) {
      ctx.showMsg(
        res.data.message || res.data.detail || "Не удалось отправить проверку.",
        !res.ok
      );
    });
  });

  $("alerts-state").addEventListener("click", refresh);
  $("registry-state").addEventListener("click", refresh);
  setInterval(refresh, REFRESH_MS);

  return { fill: fill, refresh: refresh };
}
