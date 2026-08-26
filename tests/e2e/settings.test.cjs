/*
 * Браузерные проверки настроек и доступа: вкладки, список пользователей,
 * подсказки по @username и плашка «нет доступа».
 */
const test = require("node:test");
const assert = require("node:assert");
const { launch, openApp } = require("./helpers.cjs");

const ADMIN = {
  "/api/access": { allowed: true, financier: false, admin: true,
                   pending: false, has_admins: true },
  "/api/admin/settings": {
    autofill: true, financiers: [], allowed: [],
    admins: [{ id: 42, source: "env", username: "@boss" }],
    backup: { enabled: false, time: "03:30", keep: 7 },
    reminders: { enabled: true, time: "09:30", days_before: 1, overdue: true, target: "admins" },
    registry_url: "https://docs.google.com/spreadsheets/d/SHEET",
    drive_url: "https://drive.google.com/drive/folders/FOLDER",
    alerts: { enabled: true, link_grace_min: 5, kinds: {
      storage: true, delivery: true, telegram: false, backup: true,
      error: true, moderation: true } },
    alert_kinds: [
      { key: "storage", title: "Заявка не сохранилась в реестр", critical: true },
      { key: "delivery", title: "Карточка не дошла финансисту", critical: false },
      { key: "telegram", title: "Пропадала связь с Telegram", critical: false },
      { key: "backup", title: "Сбой бэкапа", critical: false },
      { key: "error", title: "Внутренние ошибки бота", critical: false },
      { key: "moderation", title: "Мат в заявке", critical: false },
    ],
    health: { alive: true, last_ok_age: 12, down_for: null, grace_min: 5 },
    incidents: [{ kind: "telegram", title: "Связь с Telegram восстановлена",
                  ts: Math.floor(Date.now() / 1000) - 600, count: 2, sent: true }],
    incidents_day: 2,
  },
  "/api/admin/alerts": { ok: true, message: "Сохранено.", alerts: {
    enabled: true, link_grace_min: 7, kinds: {
      storage: true, delivery: false, telegram: false, backup: true,
      error: true, moderation: true } } },
  "/api/admin/users": { whitelist_empty: false, users: [
    { id: 42, username: "@boss", admin: true, admin_source: "env", financier: false, access: null },
    { id: 7, username: "@fin", admin: false, financier: true, access: "env" },
    { id: 8, username: "@vasya", admin: false, financier: false, access: "dynamic" },
    { id: 9, username: null, admin: false, financier: false, access: null },
  ] },
};

let browser;
test.before(async () => { browser = await launch(); });
test.after(async () => { await browser.close(); });

async function openSettings(width) {
  const page = await openApp(browser, { skin: "neon", width, routes: ADMIN });
  await page.click("#admin-btn");
  await page.waitForTimeout(350);
  return page;
}

test("вкладки настроек помещаются в одну строку на любой ширине", async () => {
  // Ширины взяты по краям медиазапросов: там запас минимален, и именно там
  // шестая вкладка (у админа) однажды не поместилась на два пикселя.
  for (const width of [320, 340, 360, 390, 400, 430, 460, 520, 560, 600, 720]) {
    const page = await openSettings(width);
    const info = await page.evaluate(() => {
      const strip = document.getElementById("admin-tabs");
      const tabs = [...strip.querySelectorAll(".tab")]
        .filter((t) => !t.classList.contains("hidden"));
      const box = strip.getBoundingClientRect();
      return {
        count: tabs.length,
        rows: new Set(tabs.map((t) => Math.round(t.getBoundingClientRect().top))).size,
        need: Math.round(tabs[tabs.length - 1].getBoundingClientRect().right - box.left),
        have: Math.round(box.width),
        clipped: tabs.filter((t) => t.scrollWidth > t.clientWidth + 1).length,
      };
    });
    assert.equal(info.count, 6, `${width}px: видно не все вкладки`);
    assert.equal(info.rows, 1, `${width}px: вкладки перенеслись на вторую строку`);
    // Не «впритык, но влезло»: чужой шрифт шире нашего, и мерить надо с
    // запасом, иначе поломка приедет уже к пользователю.
    assert.ok(info.have - info.need >= 8,
      `${width}px: вкладки без запаса (нужно ${info.need} из ${info.have})`);
    assert.equal(info.clipped, 0, `${width}px: подпись вкладки обрезана`);
    await page.close();
  }
});

test("каждая вкладка открывает свою панель, и только её", async () => {
  const page = await openSettings(430);
  for (const name of ["fin", "access", "data", "health", "beta", "skin"]) {
    await page.click(`#tab-${name}`);
    await page.waitForTimeout(150);
    const state = await page.evaluate(() => ({
      open: [...document.querySelectorAll("#admin-view .pane")]
        .filter((p) => !p.classList.contains("hidden")).map((p) => p.id),
      selected: [...document.querySelectorAll("#admin-tabs .tab")]
        .filter((t) => t.getAttribute("aria-selected") === "true").map((t) => t.dataset.pane),
    }));
    assert.deepEqual(state.open, [`pane-${name}`]);
    assert.deepEqual(state.selected, [name]);
  }
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("список пользователей разбит по ролям и без действий", async () => {
  const page = await openSettings(430);
  await page.click("#tab-access");
  await page.waitForTimeout(250);
  const info = await page.evaluate(() => ({
    groups: [...document.querySelectorAll("#users-list .group-head")]
      .map((g) => g.innerText.split("\n")[0].trim()),
    actions: document.querySelectorAll("#users-list .row-del").length,
    envAdminRemovable: !!document.querySelector("#adm-list .row-item .row-del"),
  }));
  assert.deepEqual(info.groups.map((g) => g.split(" ·")[0]),
    ["АДМИНЫ", "ФИНАНСИСТЫ", "ОСТАЛЬНЫЕ"]);
  assert.equal(info.actions, 0, "обзор должен быть только для чтения");
  assert.equal(info.envAdminRemovable, false, "админа из .env панель снимать не должна");
  await page.close();
});

test("реестр открывает и таблицу, и папку Диска", async () => {
  const page = await openSettings(430);
  await page.click("#tab-data");
  await page.waitForTimeout(250);
  const shown = await page.evaluate(() => ({
    sheet: !document.getElementById("open-sheet").classList.contains("hidden"),
    drive: !document.getElementById("open-drive").classList.contains("hidden"),
  }));
  assert.deepEqual(shown, { sheet: true, drive: true });
  await page.click("#open-drive");
  assert.equal(await page.evaluate(() => window.__opened),
    "https://drive.google.com/drive/folders/FOLDER");
  await page.close();
});

test("поле @username подсказывает уже известных боту людей", async () => {
  const page = await openSettings(430);
  await page.click("#tab-access");
  await page.waitForTimeout(250);
  await page.fill("#adm-input", "vas");
  await page.waitForTimeout(250);
  const hits = await page.evaluate(() =>
    [...document.querySelectorAll("#adm-suggest .suggest-row")]
      .map((r) => r.innerText.split("\n")[0]));
  assert.deepEqual(hits, ["@vasya"]);
  await page.locator("#adm-suggest .suggest-row").first().dispatchEvent("mousedown");
  await page.waitForTimeout(200);
  assert.equal(await page.inputValue("#adm-input"), "@vasya");
  assert.ok(await page.evaluate(() =>
    document.getElementById("adm-suggest").classList.contains("hidden")));
  await page.close();
});

test("без доступа форма предлагает его запросить", async () => {
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: false, pending: false, has_admins: true },
    "/api/access/request": { ok: true, message: "✅ Заявка отправлена админам." },
  } });
  assert.ok(await page.evaluate(() =>
    !document.getElementById("access-gate").classList.contains("hidden")),
    "плашка «нет доступа» не показана");
  await page.click("#access-ask");
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => ({
    posted: window.__posts.map((p) => p[0]),
    askHidden: document.getElementById("access-ask").classList.contains("hidden"),
  }));
  assert.deepEqual(after.posted, ["/api/access/request"]);
  assert.ok(after.askHidden, "после отправки кнопка должна прятаться");
  await page.close();
});

test("когда админов нет, честно сообщаем, что решать некому", async () => {
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: false, pending: false, has_admins: false },
  } });
  const state = await page.evaluate(() => ({
    note: document.getElementById("access-note").textContent,
    askHidden: document.getElementById("access-ask").classList.contains("hidden"),
  }));
  // Карточка с 26.08 покрывает два потока — уведомление о новой заявке
  // и напоминания по срокам, — поэтому заголовок шире прежнего.
  assert.match(state.note, /не задан ни один админ/);
  assert.ok(state.askHidden);
  await page.close();
});

test("панель админа обновляется после решения в чате", async () => {
  // Админ уходит в чат нажать «Открыть доступ» и возвращается: списки на
  // экране должны быть уже новыми, без перезапуска приложения.
  const page = await browser.newPage({ viewport: { width: 430, height: 800 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(() => {
    window.__granted = false;
    window.Telegram = { WebApp: {
      initData: "signed", initDataUnsafe: {}, themeParams: {}, colorScheme: "light",
      ready() {}, expand() {}, close() {}, openLink() {},
      MainButton: { isVisible: false, show() { this.isVisible = true; },
        hide() { this.isVisible = false; }, setText() {}, showProgress() {},
        hideProgress() {}, onClick() {}, offClick() {}, setParams() {},
        enable() {}, disable() {} },
      BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
      HapticFeedback: { selectionChanged() {}, impactOccurred() {}, notificationOccurred() {} },
      onEvent() {}, offEvent() {},
    } };
    window.fetch = (u) => {
      const s = String(u);
      let body = { ok: true, items: [] };
      if (s.endsWith("/api/access")) {
        body = { allowed: true, financier: false, admin: true,
                 pending: false, has_admins: true };
      }
      if (s.indexOf("/api/admin/settings") !== -1) body = {
        autofill: true, financiers: [], backup: {}, reminders: {},
        admins: [{ id: 42, source: "env", username: "@boss" }],
        allowed: window.__granted
          ? [{ id: 7, source: "dynamic", username: "@newbie" }] : [],
        registry_url: null, drive_url: null };
      if (s.indexOf("/api/admin/users") !== -1) body = { whitelist_empty: !window.__granted,
        users: [{ id: 7, username: "@newbie", admin: false, financier: false,
                  access: window.__granted ? "dynamic" : null }] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    };
  });
  await page.goto(require("./helpers.cjs").PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(500);
  await page.click("#admin-btn");
  await page.waitForTimeout(400);
  await page.click("#tab-access");
  await page.waitForTimeout(300);

  const read = () => page.evaluate(() => ({
    whitelist: [...document.querySelectorAll("#wl-list .row-item .who")]
      .map((w) => w.innerText.split("\n")[0]),
    empty: !!document.querySelector("#wl-list .empty-note"),
  }));
  const before = await read();
  assert.deepEqual(before.whitelist, []);
  assert.ok(before.empty, "список должен быть пуст до решения");

  await page.evaluate(() => { window.__granted = true; });
  // Окно Mini App на десктопе не «скрывается» — только теряет фокус.
  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await page.waitForTimeout(600);

  const after = await read();
  assert.deepEqual(after.whitelist, ["@newbie"],
    "после возврата из чата список остался прежним");
  assert.equal(after.empty, false);
  await page.close();
});

test("финансист настраивает напоминания себе, общие ему не показываем", async () => {
  const page = await browser.newPage({ viewport: { width: 430, height: 820 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(() => {
    window.__saved = null;
    window.Telegram = { WebApp: {
      initData: "signed", initDataUnsafe: {}, themeParams: {}, colorScheme: "light",
      ready() {}, expand() {}, close() {}, openLink() {},
      MainButton: { isVisible: false, show() { this.isVisible = true; },
        hide() { this.isVisible = false; }, setText() {}, showProgress() {},
        hideProgress() {}, onClick() {}, offClick() {}, setParams() {},
        enable() {}, disable() {} },
      BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
      HapticFeedback: { selectionChanged() {}, impactOccurred() {}, notificationOccurred() {} },
      onEvent() {}, offEvent() {},
    } };
    window.fetch = (u, opt) => {
      const s = String(u);
      // Финансист, но не админ: /api/admin/* ему закрыт.
      if (s.indexOf("/api/admin/") !== -1) {
        return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) });
      }
      if (s.indexOf("/api/reminders/me") !== -1) {
        if (opt && opt.method === "POST") {
          window.__saved = JSON.parse(opt.body);
          return Promise.resolve({ ok: true, json: () => Promise.resolve({
            ok: true, message: "Настройки сохранены.",
            reminders: { enabled: true, time: "07:15", days_before: 3,
                         overdue_enabled: false, custom: true } }) });
        }
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          enabled: true, time: "09:30", days_before: 1, due_enabled: true,
          overdue_enabled: true, weekdays_only: false,
          custom: false, financier: true }) });
      }
      let body = { ok: true, items: [] };
      if (s.endsWith("/api/access")) {
        body = { allowed: true, financier: true, admin: false,
                 pending: false, has_admins: true };
      }
      if (s.indexOf("/api/finance/access") !== -1) body = { ok: true };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    };
  });
  await page.goto(require("./helpers.cjs").PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(600);
  await page.click("#admin-btn");
  await page.waitForTimeout(350);

  const tabs = await page.evaluate(() =>
    [...document.querySelectorAll("#admin-tabs .tab")]
      .filter((t) => !t.classList.contains("hidden")).map((t) => t.dataset.pane));
  // Финансисту, помимо своего: «Данные» — реестр и папка счетов, он открывает
  // их каждый день. Полной админ-панели («Доступ») у него по-прежнему нет.
  assert.deepEqual(tabs, ["fin", "data", "skin", "beta"],
    "финансисту видны только его вкладки");
  const backupVisible = await page.evaluate(() => {
    const card = [...document.querySelectorAll("#pane-data .card")]
      .find((c) => /Бэкап/.test(c.textContent));
    return card && !card.classList.contains("hidden");
  });
  assert.equal(backupVisible, false, "бэкап — админский, финансисту его не показываем");

  await page.click("#tab-fin");
  await page.waitForTimeout(250);
  const cards = await page.evaluate(() =>
    [...document.querySelectorAll("#pane-fin .card")]
      .filter((c) => getComputedStyle(c).display !== "none")
      .map((c) => c.querySelector(".card-title").textContent.trim()));
  assert.equal(cards.length, 1,
    "общие настройки не для финансиста — он их всё равно не сохранит");
  assert.match(cards[0], /^⏰ Мои уведомления/);

  await page.fill("#my-rem-time", "07:15");
  await page.fill("#my-rem-days", "3");
  // Только просрочка, и не по выходным — типичная настройка финансиста.
  await page.click("#my-rem-due-seg button[data-value='off']");
  await page.click("#my-rem-weekdays-seg button[data-value='on']");
  await page.click("#my-rem-save");
  await page.waitForTimeout(400);
  assert.deepEqual(await page.evaluate(() => window.__saved), {
    // card_urgency едет тем же «Сохранить»: это один экран настроек
    // получателя, и второй круг по каналу ради одного поля не нужен.
    card_urgency: "all",
    enabled: true, time: "07:15", days_before: "3",
    due_enabled: false, overdue_enabled: true, weekdays_only: true,
  });
  assert.match(await page.evaluate(() =>
    document.getElementById("my-rem-note").textContent), /ваши настройки/);
  // Прогон на себе: приходит только нажавшему и не ждёт расписания.
  await page.click("#my-rem-test");
  await page.waitForTimeout(300);
  assert.deepEqual(await page.evaluate(() => window.__saved), { action: "test" });
  await page.close();
});

test("на узких экранах ничего не вылезает из карточек настроек", async () => {
  // 320–375 px — это iPhone с включённым увеличением дисплея, а не экзотика.
  // Кнопка «⏰ Проверить на себе» не могла сжаться ниже своей надписи, ряд не
  // переносился, и она уезжала за карточку на 30 px.
  for (const width of [320, 360, 375, 393]) {
    const page = await openApp(browser, { skin: "tg", width, height: 800, routes: ADMIN });
    await page.evaluate(() => document.getElementById("admin-btn").click());
    await page.waitForTimeout(350);
    for (const pane of ["fin", "data", "health", "skin", "beta"]) {
      await page.evaluate((p) => {
        document.querySelectorAll("#admin-view [data-admin], #admin-view [data-recipient]")
          .forEach((el) => el.classList.remove("hidden"));
        document.getElementById("tab-" + p).click();
      }, pane);
      await page.waitForTimeout(180);
      const out = await page.evaluate((p) => {
        const bad = [];
        document.querySelectorAll("#pane-" + p + " .card").forEach((card) => {
          const cb = card.getBoundingClientRect();
          const pad = parseFloat(getComputedStyle(card).paddingLeft);
          card.querySelectorAll("*").forEach((el) => {
            const r = el.getBoundingClientRect();
            if (!r.width) return;
            if (r.right > cb.right - pad + 1 || r.left < cb.left + pad - 1) {
              bad.push((el.id || el.tagName) + " " + Math.round(r.left) + "…" + Math.round(r.right));
            }
          });
        });
        return bad;
      }, pane);
      assert.deepEqual(out, [], `${width}px, вкладка ${pane}: вылезло за карточку`);
    }
    const scrollW = await page.evaluate(() => document.documentElement.scrollWidth);
    assert.equal(scrollW, width, `${width}px: появилась горизонтальная прокрутка`);
    await page.close();
  }
});

test("ряды переключателей в настройках делятся ровно пополам", async () => {
  // При flex: 1 0 auto кнопки росли от своей надписи, и граница в каждом ряду
  // вставала по-своему (157|164, 196|125, 141|180) — столбик рядов шёл
  // зигзагом и читался как съехавший блок.
  const page = await openApp(browser, { skin: "tg", width: 393, routes: ADMIN });
  await page.evaluate(() => document.getElementById("admin-btn").click());
  await page.waitForTimeout(350);
  await page.evaluate(() => {
    document.querySelectorAll("#admin-view [data-admin], #admin-view [data-recipient]")
      .forEach((el) => el.classList.remove("hidden"));
    document.getElementById("tab-fin").click();
  });
  await page.waitForTimeout(300);
  const rows = await page.evaluate(() =>
    [...document.querySelectorAll("#pane-fin .seg.seg-even")].map((seg) => ({
      id: seg.id,
      widths: [...seg.children].map((b) => Math.round(b.getBoundingClientRect().width)),
    })));
  assert.ok(rows.length >= 4, `рядов с равными долями мало: ${rows.length}`);
  rows.forEach((r) => {
    const [a, b] = r.widths;
    assert.ok(Math.abs(a - b) <= 1, `${r.id}: доли не равны — ${r.widths}`);
  });
  await page.close();
});

test("равные доли не применяются там, где надпись длинная", async () => {
  // Равные трети обрезали «🗓 Настраиваемая» в форме — это было бы хуже
  // зигзага, поэтому класс ставится только на короткие ряды.
  const page = await openApp(browser, { skin: "tg", width: 393, routes: ADMIN });
  const bad = await page.evaluate(() =>
    [...document.querySelectorAll("#form-view .seg button")]
      .filter((b) => b.scrollWidth > b.clientWidth + 1)
      .map((b) => b.textContent.trim()));
  assert.deepEqual(bad, [], "надписи в форме обрезаны");
  await page.close();
});

test("здоровье бота: категории, критичное без выключателя и сохранение", async () => {
  const page = await openSettings(390);
  await page.click("#tab-health");
  await page.waitForTimeout(250);
  const shown = await page.evaluate(() => {
    const rows = [...document.querySelectorAll("#alerts-kinds .row-item")];
    return {
      titles: rows.map((r) => r.querySelector(".who").textContent),
      // У критичного — плашка «всегда» и НИ ОДНОЙ кнопки: выключить нельзя.
      criticalTag: rows[0].querySelector(".tag").textContent,
      criticalToggle: !!rows[0].querySelector(".kind-toggle"),
      offByServer: rows.filter((r) => {
        const t = r.querySelector(".kind-toggle");
        return t && !t.classList.contains("on");
      }).map((r) => r.querySelector(".who").textContent),
      state: document.getElementById("alerts-state-text").textContent,
      bad: document.getElementById("alerts-state").classList.contains("bad"),
      journal: document.querySelector("#alerts-log .row-item .who").innerText,
    };
  });
  assert.equal(shown.titles.length, 6);
  assert.equal(shown.criticalTag, "всегда");
  assert.equal(shown.criticalToggle, false, "критичное уведомление дали выключить");
  assert.deepEqual(shown.offByServer, ["Пропадала связь с Telegram"]);
  assert.match(shown.state, /в норме/);
  assert.equal(shown.bad, false);
  assert.match(shown.journal, /Связь с Telegram восстановлена ×2/);

  // Переключаем категорию и сохраняем — на сервер уходит именно она.
  await page.click('#alerts-kinds .kind-toggle[data-kind="delivery"]');
  await page.fill("#alerts-grace", "7");
  await page.click("#alerts-save");
  await page.waitForTimeout(250);
  const sent = await page.evaluate(() => {
    const post = window.__posts.filter((p) => p[0].indexOf("/api/admin/alerts") !== -1).pop();
    return { body: JSON.parse(post[1]), msg: document.getElementById("admin-msg").textContent };
  });
  assert.equal(sent.body.action, "save");
  assert.equal(sent.body.kinds.delivery, false);
  assert.equal(sent.body.link_grace_min, "7");
  assert.match(sent.msg, /Сохранено/);
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("пропавшая связь видна на экране, даже когда сообщение прийти не может", async () => {
  const routes = JSON.parse(JSON.stringify(ADMIN));
  routes["/api/admin/settings"].health = {
    alive: false, last_ok_age: 900, down_for: 900, grace_min: 5 };
  const page = await openApp(browser, { skin: "neon", width: 390, routes });
  await page.click("#admin-btn");
  await page.waitForTimeout(300);
  await page.click("#tab-health");
  await page.waitForTimeout(250);
  const state = await page.evaluate(() => {
    const line = document.getElementById("alerts-state");
    const card = document.getElementById("alerts-card");
    return {
      text: document.getElementById("alerts-state-text").textContent,
      bad: line.classList.contains("bad"),
      // Карточка обязана помещаться по ширине: ей жить на телефоне.
      overflow: card.scrollWidth - card.clientWidth,
    };
  });
  assert.match(state.text, /Связи с Telegram нет 15 мин/);
  assert.ok(state.bad, "провал связи должен быть заметен, а не просто написан");
  assert.ok(state.overflow <= 1, `карточка вылезает на ${state.overflow}px`);
  await page.close();
});

test("финансисту здоровье бота не показывают", async () => {
  const page = await openApp(browser, { skin: "neon", width: 390, routes: {
    "/api/access": { allowed: true, financier: true, admin: false,
                     pending: false, has_admins: true },
    "/api/admin/settings": { __status: 403 },
    "/api/registry/links": { registry_url: null, drive_url: null },
  } });
  await page.click("#admin-btn");
  await page.waitForTimeout(350);
  const visible = await page.evaluate(() => {
    const tab = document.getElementById("tab-health");
    const card = document.getElementById("alerts-card");
    return { tab: !tab.classList.contains("hidden"), card: card.offsetParent !== null };
  });
  assert.deepEqual(visible, { tab: false, card: false },
    "вкладка эксплуатации досталась не админу");
  await page.close();
});

test("получатель выбирает, о каких заявках его уведомлять сразу", async () => {
  // Обычных заявок в день бывает много, и уведомление о каждой перестаёт
  // читаться. Это НЕ напоминания по срокам — то первое сообщение, которое
  // приходит сразу после подачи.
  const page = await openSettings(430);
  await page.click("#tab-reminders").catch(() => {});
  await page.waitForTimeout(200);
  const shown = await page.evaluate(() => {
    const seg = document.getElementById("my-cards-seg");
    return {
      есть: !!seg,
      выбрано: seg && seg.querySelector("button.active").dataset.value,
      варианты: seg && [...seg.querySelectorAll("button")].map((b) => b.dataset.value),
    };
  });
  assert.equal(shown.есть, true, "переключателя нет");
  assert.deepEqual(shown.варианты, ["all", "urgent"]);
  assert.equal(shown.выбрано, "all", "по умолчанию должны приходить все");

  await page.click('#my-cards-seg button[data-value="urgent"]');
  await page.click("#my-rem-save");
  await page.waitForTimeout(250);
  const sent = await page.evaluate(() => {
    const post = window.__posts.filter((p) => p[0].indexOf("/api/reminders/me") !== -1).pop();
    return JSON.parse(post[1]);
  });
  assert.equal(sent.card_urgency, "urgent", "выбор не ушёл на сервер");
  assert.deepEqual(page.errors, []);
  await page.close();
});
