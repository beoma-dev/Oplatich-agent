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
  },
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
  for (const width of [320, 360, 390, 430, 520, 720]) {
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
    assert.equal(info.count, 5, `${width}px: видно не все вкладки`);
    assert.equal(info.rows, 1, `${width}px: вкладки перенеслись на вторую строку`);
    assert.ok(info.need <= info.have + 1,
      `${width}px: вкладки не помещаются (${info.need} из ${info.have})`);
    assert.equal(info.clipped, 0, `${width}px: подпись вкладки обрезана`);
    await page.close();
  }
});

test("каждая вкладка открывает свою панель, и только её", async () => {
  const page = await openSettings(430);
  for (const name of ["fin", "access", "data", "beta", "skin"]) {
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
  assert.match(cards[0], /^⏰ Мои напоминания/);

  await page.fill("#my-rem-time", "07:15");
  await page.fill("#my-rem-days", "3");
  // Только просрочка, и не по выходным — типичная настройка финансиста.
  await page.click("#my-rem-due-seg button[data-value='off']");
  await page.click("#my-rem-weekdays-seg button[data-value='on']");
  await page.click("#my-rem-save");
  await page.waitForTimeout(400);
  assert.deepEqual(await page.evaluate(() => window.__saved), {
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
