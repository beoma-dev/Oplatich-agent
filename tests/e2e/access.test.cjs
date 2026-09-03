/*
 * Доступ к подаче: форма прячется без него и оживает сама, когда админ
 * решил, — без перезапуска приложения.
 */
const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { launch, openApp } = require("./helpers.cjs");

const PAGE_URL = "file://" + path.resolve(__dirname, "../../webapp/index.html");

/** Заглушка с ПЕРЕКЛЮЧАЕМЫМ доступом: обычный helpers.cjs отдаёт фиксированные
 *  ответы, а здесь весь смысл в том, что ответ меняется по ходу. */
function install() {
  window.__allowed = false;
  window.__checks = 0;
  window.__cpLoads = 0;
  window.Telegram = { WebApp: {
    initData: "signed", initDataUnsafe: {}, themeParams: {}, colorScheme: "light",
    ready() {}, expand() {}, close() {}, openLink() {},
    MainButton: {
      isVisible: false, show() { this.isVisible = true; },
      hide() { this.isVisible = false; }, setText() {}, showProgress() {},
      hideProgress() {}, onClick() {}, offClick() {}, setParams() {},
      enable() {}, disable() {},
    },
    BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
    HapticFeedback: { selectionChanged() {}, impactOccurred() {}, notificationOccurred() {} },
    onEvent() {}, offEvent() {},
  } };
  window.fetch = (u) => {
    const s = String(u);
    if (s.endsWith("/api/access")) {
      window.__checks++;
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { allowed: window.__allowed, pending: true, has_admins: true }) });
    }
    if (s.indexOf("/api/counterparties") !== -1) {
      if (!window.__allowed) {
        return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) });
      }
      window.__cpLoads++;
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { items: [{ name: "ООО «Ромашка»" }] }) });
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, items: [] }) });
  };
}

const snapshot = () => ({
  gate: !document.getElementById("access-gate").classList.contains("hidden"),
  cards: [...document.querySelectorAll("#form-view .card")]
    .filter((c) => getComputedStyle(c).display !== "none").length,
  mainButton: window.Telegram.WebApp.MainButton.isVisible,
  chips: document.getElementById("cp-chips").children.length,
  modal: document.getElementById("modal").classList.contains("shown")
    ? document.getElementById("modal-title").textContent : null,
});

/** Сворачивание и возврат в приложение. */
function toggleVisibility(hidden) {
  Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
  document.dispatchEvent(new Event("visibilitychange"));
}

let browser;
test.before(async () => { browser = await launch(); });
test.after(async () => { await browser.close(); });

test("без доступа форма скрыта, с доступом — оживает без перезапуска", async () => {
  const page = await browser.newPage({ viewport: { width: 430, height: 780 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(install);
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(600);

  const denied = await page.evaluate(snapshot);
  assert.ok(denied.gate, "плашка «нет доступа» не показана");
  assert.equal(denied.cards, 0, "поля формы видны, хотя отправить их некуда");
  assert.equal(denied.mainButton, false, "кнопка отправки видна без доступа");

  // Админ открыл доступ — приложение не трогаем.
  await page.evaluate(() => { window.__allowed = true; });
  await page.waitForTimeout(7500);
  const granted = await page.evaluate(snapshot);
  assert.equal(granted.gate, false, "плашка осталась после выдачи доступа");
  assert.ok(granted.cards > 0, "форма не появилась");
  assert.ok(granted.mainButton, "кнопка отправки не вернулась");
  assert.ok(granted.chips > 0, "подсказки контрагентов не догрузились");
  assert.equal(granted.modal, "Доступ открыт", "человеку не сказали, что доступ дали");
  assert.deepEqual(errors, []);
  await page.close();
});

test("отзыв доступа виден сам, без возврата в приложение", async () => {
  const page = await browser.newPage({ viewport: { width: 430, height: 780 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(install);
  await page.addInitScript(() => { window.__allowed = true; });
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(600);
  assert.ok((await page.evaluate(snapshot)).cards > 0, "форма должна быть видна");

  // Права снимают в чате у админа: приложение должно заметить это само.
  await page.evaluate(() => { window.__allowed = false; });
  await page.waitForTimeout(7500);

  const revoked = await page.evaluate(snapshot);
  assert.ok(revoked.gate, "плашка не вернулась после отзыва доступа");
  assert.equal(revoked.cards, 0, "форма осталась видна после отзыва");
  assert.equal(revoked.modal, "Доступ закрыт", "человеку не сказали, что доступ закрыли");
  await page.close();
});

test("свёрнутое приложение сервер не дёргает", async () => {
  const page = await browser.newPage({ viewport: { width: 430, height: 780 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(install);
  await page.addInitScript(() => { window.__allowed = true; });
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(600);

  await page.evaluate(toggleVisibility, true);
  const before = await page.evaluate(() => window.__checks);
  await page.waitForTimeout(7000);
  assert.equal(await page.evaluate(() => window.__checks), before,
    "опрос идёт, пока приложение свёрнуто");
  await page.close();
});

test("кнопка панели финансиста уходит вместе с правами", async () => {
  // Права снимают в чате: приложение должно убрать кнопку само и закрыть
  // уже открытую панель — чужие заявки в ней смотреть больше нельзя.
  const page = await browser.newPage({ viewport: { width: 430, height: 800 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(() => {
    window.__fin = true;
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
      if (s.indexOf("/api/admin/") !== -1) {
        return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) });
      }
      let body = { ok: true, items: [] };
      if (s.endsWith("/api/access")) {
        body = { allowed: true, requests: window.__fin, pending: false, has_admins: true };
      }
      if (s.indexOf("/api/finance/requests") !== -1) body = { items: [], total: 0 };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    };
  });
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(700);

  const read = () => page.evaluate(() => ({
    button: !document.getElementById("fin-btn").classList.contains("hidden"),
    panel: !document.getElementById("fin-view").classList.contains("hidden"),
  }));
  assert.deepEqual(await read(), { button: true, panel: false });
  await page.click("#fin-btn");
  await page.waitForTimeout(400);
  assert.deepEqual(await read(), { button: true, panel: true });

  await page.evaluate(() => { window.__fin = false; });
  await page.waitForTimeout(7500);
  assert.deepEqual(await read(), { button: false, panel: false },
    "кнопка панели осталась после отзыва прав финансиста");
  await page.close();
});

test("перегрузка: плашка приходит на 503 и уходит сама", async () => {
  // Лимит Google — 60 чтений в минуту НА ПРОЕКТ, общий на всех: упереться
  // может один, а отказ увидят остальные. Раньше 429 от Google уходил наружу
  // голым «Internal Server Error», и человек видел поломку там, где надо
  // подождать. Полоса ставится перехватом fetch — проверяем именно её.
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(() => {
    window.__busy = false;
    window.Telegram = { WebApp: {
      initData: "signed", initDataUnsafe: {}, themeParams: {}, colorScheme: "light",
      ready() {}, expand() {}, close() {}, openLink() {},
      MainButton: { isVisible: false, show() {}, hide() {}, setText() {},
        showProgress() {}, hideProgress() {}, onClick() {}, offClick() {},
        setParams() {}, enable() {}, disable() {} },
      BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
      HapticFeedback: { selectionChanged() {}, impactOccurred() {}, notificationOccurred() {} },
      onEvent() {}, offEvent() {},
    } };
    window.fetch = (u) => {
      if (window.__busy) {
        return Promise.resolve({ ok: false, status: 503,
          headers: { get: () => "6" },      // Retry-After
          json: () => Promise.resolve({ detail: "Превышен лимит обращений." }) });
      }
      return Promise.resolve({ ok: true, status: 200, headers: { get: () => null },
        json: () => Promise.resolve({ allowed: true, pending: false,
          has_admins: true, admin: false, requests: false, items: [] }) });
    };
  });
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(600);

  const banner = () => page.evaluate(() => {
    const b = document.getElementById("busy-banner");
    return { shown: !b.classList.contains("hidden"), text: b.textContent };
  });
  assert.equal((await banner()).shown, false, "полоса висит без повода");

  // Сервер захлебнулся: следующий же опрос доступа приносит 503.
  await page.evaluate(() => { window.__busy = true; });
  await page.waitForTimeout(7000);
  const busy = await banner();
  assert.equal(busy.shown, true, "перегрузку человеку не показали");
  assert.match(busy.text, /перегружен/i, `неожиданный текст: ${busy.text}`);
  assert.match(busy.text, /\d+ с/, "нет отсчёта — непонятно, сколько ждать");

  // Отпустило — полоса уходит на первом же успешном ответе, не досиживая.
  await page.evaluate(() => { window.__busy = false; });
  await page.waitForTimeout(7000);
  assert.equal((await banner()).shown, false, "полоса осталась после починки");
  await page.close();
});

test("админ не из финансистов: шапка не мигает на первом опросе", async () => {
  // 03.09.2026: на вопрос «показывать ли панель заявок» отвечали ДВЕ ручки, и
  // для админа, который финансистом не числится, они расходились. /finance/access
  // отвечала «да» (право = финансист ИЛИ админ), а /api/access отдавала своё
  // «финансист ли он» — «нет». Кнопка панели появлялась на открытии и пропадала
  // через шесть секунд, на первом же опросе, утаскивая за собой всю шапку:
  // значок аналитики уезжал на 36 px вбок.
  const page = await openApp(browser, { skin: "light", width: 390, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true,
                     admin: true, requests: true },
    "/api/admin/settings": { ok: true, financiers: [], allowed: [], admins: [] },
  } });
  const shot = () => page.evaluate(() => {
    const g = (id) => document.getElementById(id);
    return { fin: !g("fin-btn").classList.contains("hidden"),
             stats: !g("stats-btn").classList.contains("hidden"),
             statsX: Math.round(g("stats-btn").getBoundingClientRect().left) };
  });
  const before = await shot();
  assert.deepEqual({ fin: before.fin, stats: before.stats }, { fin: true, stats: true },
    "админу нужны обе кнопки: панель заявок и аналитика");
  await page.waitForTimeout(7500);          // круг опроса — здесь всё и ломалось
  assert.deepEqual(await shot(), before, "шапка переехала на первом же опросе");
  const probes = (await page.evaluate(() => window.__gets))
    .filter((u) => u.indexOf("/api/finance/access") !== -1);
  assert.equal(probes.length, 0,
    "у кнопки панели снова два источника правды — расхождение вернётся");
  await page.close();
});

test("права админа появляются и уходят без перезапуска", async () => {
  // Назначают и снимают их в чате — приложение узнаёт из того же опроса.
  const page = await browser.newPage({ viewport: { width: 430, height: 820 } });
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  await page.addInitScript(() => {
    window.__admin = false;
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
      if (s.indexOf("/api/admin/") !== -1 && !window.__admin) {
        return Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({}) });
      }
      let body = { ok: true, items: [] };
      if (s.endsWith("/api/access")) {
        body = { allowed: true, requests: false, admin: window.__admin,
                 pending: false, has_admins: true };
      }
      if (s.indexOf("/api/admin/settings") !== -1) {
        body = { autofill: true, financiers: [], allowed: [], admins: [],
                 backup: {}, reminders: {}, registry_url: null, drive_url: null };
      }
      if (s.indexOf("/api/admin/users") !== -1) body = { whitelist_empty: false, users: [] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    };
  });
  await page.goto(PAGE_URL, { waitUntil: "load" });
  await page.waitForTimeout(700);
  await page.click("#admin-btn");
  await page.waitForTimeout(350);

  const tabs = () => page.evaluate(() =>
    [...document.querySelectorAll("#admin-tabs .tab")]
      .filter((t) => !t.classList.contains("hidden")).map((t) => t.dataset.pane));
  // Обычному пользователю — «Бета» (личный выключатель чтения счёта) и
  // «Оформление». Ни «Доступа», ни «Данных»: это чужая зона ответственности.
  assert.deepEqual(await tabs(), ["skin", "beta"],
    "у не-админа админских вкладок быть не должно");

  await page.evaluate(() => { window.__admin = true; });
  await page.waitForTimeout(7500);
  // Админ — тоже получатель напоминаний, поэтому вкладка «fin» тоже его.
  assert.deepEqual(await tabs(), ["fin", "access", "data", "health", "skin", "beta"],
    "назначили админом — вкладки не появились");

  // И обратно: права сняли, открытая админская вкладка не должна остаться.
  // Уходим именно на «Здоровье»: она чисто админская, её обязано снести.
  await page.click("#tab-health");
  await page.waitForTimeout(200);
  await page.evaluate(() => { window.__admin = false; });
  await page.waitForTimeout(7500);
  assert.deepEqual(await tabs(), ["skin", "beta"], "права сняли — вкладки остались");
  assert.deepEqual(await page.evaluate(() =>
    [...document.querySelectorAll("#admin-view .pane")]
      .filter((p) => !p.classList.contains("hidden")).map((p) => p.id)),
    ["pane-skin"], "остались на админской вкладке после снятия прав");
  await page.close();
});
