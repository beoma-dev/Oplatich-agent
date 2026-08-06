/*
 * Доступ к подаче: форма прячется без него и оживает сама, когда админ
 * решил, — без перезапуска приложения.
 */
const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const { launch } = require("./helpers.cjs");

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
        body = { allowed: true, financier: window.__fin, pending: false, has_admins: true };
      }
      if (s.indexOf("/api/finance/access") !== -1) body = { ok: window.__fin };
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
        body = { allowed: true, financier: false, admin: window.__admin,
                 pending: false, has_admins: true };
      }
      if (s.indexOf("/api/admin/settings") !== -1) {
        body = { autofill: true, financiers: [], allowed: [], admins: [],
                 backup: {}, reminders: {}, registry_url: null, drive_url: null };
      }
      if (s.indexOf("/api/admin/users") !== -1) body = { whitelist_empty: false, users: [] };
      if (s.indexOf("/api/finance/access") !== -1) body = { ok: false };
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
  assert.deepEqual(await tabs(), ["skin"], "у не-админа админских вкладок быть не должно");

  await page.evaluate(() => { window.__admin = true; });
  await page.waitForTimeout(7500);
  // Админ — тоже получатель напоминаний, поэтому вкладка «fin» тоже его.
  assert.deepEqual(await tabs(), ["fin", "access", "data", "beta", "skin"],
    "назначили админом — вкладки не появились");

  // И обратно: права сняли, открытая админская вкладка не должна остаться.
  await page.click("#tab-data");
  await page.waitForTimeout(200);
  await page.evaluate(() => { window.__admin = false; });
  await page.waitForTimeout(7500);
  assert.deepEqual(await tabs(), ["skin"], "права сняли — вкладки остались");
  assert.deepEqual(await page.evaluate(() =>
    [...document.querySelectorAll("#admin-view .pane")]
      .filter((p) => !p.classList.contains("hidden")).map((p) => p.id)),
    ["pane-skin"], "остались на админской вкладке после снятия прав");
  await page.close();
});
