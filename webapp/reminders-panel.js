/* Мои напоминания: личные настройки получателя.
 *
 * Вынесено из app.js по той же причине, что alerts-panel.js и
 * restore-panel.js: файл упёрся в свой потолок (2900 строк), и следующая
 * правка формы в него просто не влезала. Панель самодостаточна — берёт
 * из app.js только общие помощники, а наружу отдаёт загрузку настроек.
 */
function buildRemindersPanel(ctx) {
  var $ = ctx.$;
  var setSeg = ctx.setSeg;
  var bindFilterSeg = ctx.bindFilterSeg;
  var showAdminMsg = ctx.showMsg;
  var initData = ctx.initData;
  var bindSeg = ctx.bindSeg;
  var insideTelegram = ctx.insideTelegram;
  var tg = ctx.tg;
  if (!$("my-rem-seg")) return null;

  // --- Мои напоминания: настройки конкретного получателя ---------------------
  var myRem = { enabled: true, overdue: true };
  // Какие заявки присылать карточкой сразу после подачи. Это НЕ напоминания
  // по срокам: там свои настройки ниже. Срочные приходят всегда — отписаться
  // от них нельзя, поэтому выбор только между «все» и «только срочные».
  var myCards = "all";

  function fillMyReminders(cfg) {
    if (cfg.card_urgency) {
      myCards = cfg.card_urgency;
      setSeg("my-cards-seg", myCards);
    }
    myRem.enabled = !!cfg.enabled;
    myRem.overdue = !!cfg.overdue_enabled;
    setSeg("my-rem-seg", myRem.enabled ? "on" : "off");
    setSeg("my-rem-overdue-seg", myRem.overdue ? "on" : "off");
    $("my-rem-time").value = cfg.time || "09:30";
    $("my-rem-days").value = cfg.days_before === undefined ? 1 : cfg.days_before;
    $("my-rem-opts").style.opacity = myRem.enabled ? "" : ".45";
    // Финансист получает и «скоро к оплате», и просрочку; остальные — только
    // просрочку, если админ так настроил маршрут.
    $("my-rem-note").textContent = cfg.custom
      ? "Это ваши настройки — на других они не влияют."
      : "Пока действуют настройки по умолчанию. Измените — станут вашими.";
  }

  /** Личный выключатель чтения счёта. Доступен ВСЕМ: бета есть бета, и тот,
   *  кому распознавание мешает, отключает его себе сам. */
  function loadMyAutofill() {
    if (!insideTelegram) return;
    fetch("/api/autofill/me", { headers: { "X-Telegram-Init-Data": initData } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) fillMyAutofill(d); })
      .catch(function () { /* нет доступа — карточка останется как есть */ });
  }

  function fillMyAutofill(d) {
    setSeg("my-autofill-seg", d.enabled ? "on" : "off");
    var note = $("my-autofill-note");
    if (!note) return;
    // Общий выключатель главнее личного: выключенный, он гасит функцию у всех.
    note.textContent = d.available
      ? "По приложенному файлу бот распознаёт сумму, контрагента и реквизиты"
        + " и ПРЕДЛАГАЕТ ими заполнить пустые поля. Решение всегда за вами."
      : "Сейчас чтение счёта выключено администратором для всех — ваш выбор"
        + " начнёт действовать, когда его включат обратно.";
  }

  bindSeg("my-autofill-seg", function (value) {
    if (!insideTelegram) return;
    var body = new FormData();
    body.append("enabled", value === "on" ? "1" : "0");
    fetch("/api/autofill/me", {
      method: "POST", headers: { "X-Telegram-Init-Data": initData }, body: body
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) fillMyAutofill(d); })
      .catch(function () { /* сеть — настройка применится при следующем входе */ });
  });

  function loadMyReminders() {
    fetch("/api/reminders/me", { headers: { "X-Telegram-Init-Data": initData } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (d) fillMyReminders(d); })
      .catch(function () { /* не получатель — карточки всё равно не видно */ });
  }

  function myReminderRequest(body, btn) {
    btn.disabled = true;
    btn.style.opacity = ".6";
    fetch("/api/reminders/me", {
      method: "POST",
      headers: {
        "X-Telegram-Init-Data": initData,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (d) {
          showAdminMsg(d.message || d.detail || "Готово.", !r.ok);
          if (r.ok && d.reminders) fillMyReminders(d.reminders);
          if (tg && tg.HapticFeedback) {
            tg.HapticFeedback.notificationOccurred(r.ok ? "success" : "error");
          }
        });
      })
      .catch(function () { showAdminMsg("Сеть недоступна.", true); })
      .then(function () { btn.disabled = false; btn.style.opacity = ""; });
  }

  bindFilterSeg("my-cards-seg", function (v) { myCards = v; });
  bindFilterSeg("my-rem-seg", function (v) {
    myRem.enabled = v === "on";
    $("my-rem-opts").style.opacity = myRem.enabled ? "" : ".45";
  });
  bindFilterSeg("my-rem-overdue-seg", function (v) { myRem.overdue = v === "on"; });
  bindFilterSeg("my-rem-due-seg", function (v) { myRem.due = v === "on"; });
  bindFilterSeg("my-rem-weekdays-seg", function (v) { myRem.weekdays = v === "on"; });
  $("my-rem-save").addEventListener("click", function () {
    myReminderRequest({
      card_urgency: myCards,
      enabled: myRem.enabled,
      time: $("my-rem-time").value,
      days_before: $("my-rem-days").value.trim(),
      due_enabled: myRem.due,
      overdue_enabled: myRem.overdue,
      weekdays_only: myRem.weekdays
    }, this);
  });
  // Прогон на себе: приходит только нажавшему и по его настройкам.
  $("my-rem-test").addEventListener("click", function () {
    myReminderRequest({ action: "test" }, this);
  });
  $("my-rem-reset").addEventListener("click", function () {
    myReminderRequest({ action: "reset" }, this);
  });

  return { load: loadMyReminders, loadAutofill: loadMyAutofill };
}
