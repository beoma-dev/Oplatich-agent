/*
 * helpers.cjs — общая обвязка браузерных тестов Mini App.
 *
 * Страница открывается по file:// и работает без сервера: настоящий
 * telegram-web-app.js блокируется, вместо него подставляется заглушка, а
 * fetch отвечает заранее заданными данными. Именно поэтому такие проверки
 * ловят то, что не видно из тестов по исходнику: каскад CSS, раскладку и
 * реальные обработчики.
 */
const path = require("path");
const { chromium } = require("playwright");

const PAGE_URL = "file://" + path.resolve(__dirname, "../../webapp/index.html");

/** Заглушка Telegram.WebApp и fetch. Выполняется В БРАУЗЕРЕ, поэтому всё
 *  нужное принимает аргументом: замыкания сюда не доезжают. */
function install(cfg) {
  if (cfg.skin) localStorage.setItem("invoice_skin_v1", cfg.skin);
  window.__posts = [];
  window.__params = [];
  window.__opened = null;
  window.Telegram = { WebApp: {
    initData: "signed", initDataUnsafe: {}, themeParams: {},
    colorScheme: cfg.skin === "neon" ? "dark" : "light",
    ready() {}, expand() {}, close() {},
    openLink(u) { window.__opened = u; },
    MainButton: {
      // isVisible ведём как настоящий клиент: скрытая кнопка должна быть
      // видна тестам именно скрытой.
      isVisible: false,
      show() { this.isVisible = true; },
      hide() { this.isVisible = false; },
      setText() {}, showProgress() {}, hideProgress() {},
      onClick() {}, offClick() {},
      setParams(p) { window.__params.push(p); }, enable() {}, disable() {},
    },
    BackButton: { show() {}, hide() {}, onClick() {}, offClick() {} },
    HapticFeedback: { selectionChanged() {}, impactOccurred() {}, notificationOccurred() {} },
    onEvent() {}, offEvent() {},
  } };
  window.fetch = (url, opt) => {
    const u = String(url);
    if (opt && opt.method === "POST") window.__posts.push([u, opt.body || null]);
    const key = Object.keys(cfg.routes || {}).find((k) => u.indexOf(k) !== -1);
    const body = key ? cfg.routes[key] : { ok: true, items: [] };
    // ok считаем как настоящий fetch — по коду, а не по одному 403. С прежней
    // проверкой любой 4xx кроме 403 приходил в приложение как успех, и ветку
    // «сервер отказал» нельзя было проверить вовсе. 409 у дедупа приложение
    // разбирает отдельно, до resp.ok, — на него это не влияет.
    const status = body.__status || 200;
    return Promise.resolve({ ok: status < 400, status,
      json: () => Promise.resolve(body) });
  };
}

/** Готовая страница приложения: заглушки установлены, скрипты отработали. */
async function openApp(browser, cfg = {}) {
  const page = await browser.newPage({
    viewport: { width: cfg.width || 430, height: cfg.height || 900 },
    deviceScaleFactor: 2,
  });
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  // Настоящий SDK Telegram перетёр бы заглушку: в тестах он не нужен.
  await page.route("**/telegram-web-app.js", (r) => r.abort());
  // cfg.noSdk — SDK Telegram не загрузился (внешний скрипт с telegram.org).
  // Заглушку тогда не ставим вовсе: initData должен прийти из хеша адреса.
  if (!cfg.noSdk) await page.addInitScript(install, cfg);
  await page.goto(PAGE_URL + (cfg.hash || ""), { waitUntil: "load" });
  await page.waitForTimeout(cfg.settle || 450);
  page.errors = errors;
  return page;
}

async function launch() {
  return chromium.launch();
}

module.exports = { launch, openApp, PAGE_URL };
