/*
 * Экран «Аналитика»: виден только админу, показывает то, что прислал сервер,
 * и ничего не считает сам.
 */
const test = require("node:test");
const assert = require("node:assert");
const { launch, openApp } = require("./helpers.cjs");

let browser;
test.before(async () => { browser = await launch(); });
test.after(async () => { await browser.close(); });

const DATA = {
  days: 30,
  people: {
    authors_period: 4, authors_ever: 6,
    top: [
      { name: "@petya (Пётр)", count: 12, sums: { RUB: "412000.00" } },
      { name: "@masha (Мария)", count: 7, sums: { RUB: "88500.50" } },
    ],
    idle: ["@lena", "@vasya"],
  },
  flow: {
    period_count: 22, period_sums: { RUB: "552705.50", USD: "300.00" },
    total_count: 34, statuses: { "Новая": 9, "Оплачена": 21 },
    median_days: 2, paid_measured: 21,
    waiting_long: 4, waiting_after_days: 3,
    overdue_now: 3, overdue_sums: { RUB: "196910.00" },
    articles: [{ name: "Аренда", count: 9, sums: { RUB: "300000.00" } }],
  },
  docs: { no_docs: 2, paid_total: 21, paid_without_closing: 8 },
};

const ADMIN = {
  "/api/access": { allowed: true, pending: false, has_admins: true, admin: true },
  "/api/admin/analytics": DATA,
  "/api/admin/settings": { ok: true, financiers: [], allowed: [], admins: [] },
};

async function openStats(cfg) {
  const page = await openApp(browser, Object.assign(
    { skin: "light", width: 360, height: 1400, routes: ADMIN }, cfg));
  await page.waitForTimeout(450);
  await page.click("#stats-btn");
  await page.waitForTimeout(400);
  return page;
}

test("аналитика открывается у админа и показывает присланные числа", async () => {
  const page = await openStats();
  const text = await page.evaluate(() => document.getElementById("stats-body").textContent);
  // Люди — то, ради чего экран и просили.
  assert.match(text, /Авторов за период/);
  assert.match(text, /@petya \(Пётр\)/);
  assert.match(text, /@lena, @vasya/, "не видно тех, у кого доступ есть, а заявок нет");
  // Сроки.
  assert.match(text, /2 дн\./, "нет медианы «подача → оплата»");
  assert.match(text, /Ждут дольше 3 дн\./);
  // Суммы разбиты по валютам и отформатированы, а не «552705.50».
  assert.match(text, /552 705,50 RUB · 300 USD/);
  // Документы.
  assert.match(text, /Оплачено без акта \/ УПД/);
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("тревожные числа выделены, спокойные — нет", async () => {
  const page = await openStats();
  const marked = await page.evaluate(() =>
    [...document.querySelectorAll("#stats-body .st-stat")].map((el) => ({
      label: el.querySelector(".st-label").textContent,
      warn: el.classList.contains("st-warn"),
    })));
  const by = (re) => marked.find((m) => re.test(m.label));
  assert.ok(by(/Просрочено сейчас/).warn, "просрочка не выделена");
  assert.ok(by(/Ждут дольше/).warn, "залежавшиеся не выделены");
  assert.ok(!by(/Авторов за период/).warn, "обычное число выделено тревогой");
  await page.close();
});

test("смена периода перезапрашивает сводку", async () => {
  const page = await openStats();
  await page.click('#stats-days button[data-value="7"]');
  await page.waitForTimeout(300);
  const asked = await page.evaluate(() =>
    window.__gets.filter((u) => u.indexOf("/api/admin/analytics?days=7") !== -1).length);
  assert.equal(asked, 1, "период не ушёл на сервер");
  await page.close();
});

test("не админу кнопки аналитики нет", async () => {
  const page = await openApp(browser, { skin: "light", width: 360, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true, admin: false },
  } });
  await page.waitForTimeout(500);
  const hidden = await page.evaluate(() =>
    document.getElementById("stats-btn").classList.contains("hidden"));
  assert.ok(hidden, "кнопка аналитики видна не админу");
  await page.close();
});

test("отказ сервера виден, а не показан пустым экраном", async () => {
  // 403 бывает, если права сняли, пока экран был открыт: пустая сводка
  // читалась бы как «заявок нет».
  const page = await openStats({ routes: Object.assign({}, ADMIN, {
    "/api/admin/analytics": { __status: 403, detail: "Только для администраторов бота." },
  }) });
  const msg = await page.evaluate(() => document.getElementById("stats-msg").textContent);
  assert.match(msg, /Только для администраторов/);
  assert.equal(await page.evaluate(() =>
    document.getElementById("stats-body").children.length), 0);
  await page.close();
});

test("пустая сводка не ломает экран", async () => {
  // На боевом сейчас шесть заявок, а бывает и ноль.
  const empty = {
    days: 30,
    people: { authors_period: 0, authors_ever: 0, top: [], idle: [] },
    flow: { period_count: 0, period_sums: {}, total_count: 0, statuses: {},
      median_days: null, paid_measured: 0, waiting_long: 0, waiting_after_days: 3,
      overdue_now: 0, overdue_sums: {}, articles: [] },
    docs: { no_docs: 0, paid_total: 0, paid_without_closing: 0 },
  };
  const page = await openStats({ routes: Object.assign({}, ADMIN, {
    "/api/admin/analytics": empty,
  }) });
  const text = await page.evaluate(() => document.getElementById("stats-body").textContent);
  assert.match(text, /Ещё нечего мерить/, "медиана без данных показана числом");
  assert.match(text, /Заявок пока нет|За период заявок не было/);
  assert.deepEqual(page.errors, []);
  await page.close();
});
