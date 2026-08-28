/*
 * Экран «Аналитика»: доступ, содержимое, период.
 *
 * Считает всё сервер, поэтому здесь проверяется ровно то, за что отвечает
 * приложение: кому экран виден, что цифры доехали на экран не перепутанными
 * и что переключение периода спрашивает сервер заново.
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
      { name: "@masha (Маша)", count: 7, sums: { RUB: "88500.50" } },
    ],
    idle: ["@lena", "@vasya"],
  },
  flow: {
    period_count: 22, period_sums: { RUB: "552705.50", USD: "300.00" },
    total_count: 34,
    statuses: { "Новая": 9, "Оплачена": 21 },
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

test("значок аналитики есть только у админа", async () => {
  for (const admin of [true, false]) {
    const page = await openApp(browser, { skin: "light", width: 360, routes: {
      ...ADMIN,
      "/api/access": { allowed: true, pending: false, has_admins: true, admin },
    } });
    await page.waitForTimeout(500);
    assert.equal(
      await page.evaluate(() => !document.getElementById("stats-btn").classList.contains("hidden")),
      admin, admin ? "у админа нет значка" : "значок виден не админу");
    await page.close();
  }
});

test("права сняли на открытом экране — уводит на форму", async () => {
  // Иначе человек остался бы читать чужие суммы после того, как его разжаловали.
  const page = await openApp(browser, { skin: "light", width: 360, routes: ADMIN });
  await page.waitForTimeout(500);
  await page.click("#stats-btn");
  await page.waitForTimeout(400);
  assert.ok(await page.evaluate(() =>
    !document.getElementById("stats-view").classList.contains("hidden")));

  await page.evaluate(() => {
    const old = window.fetch;
    window.fetch = (u, o) => (String(u).indexOf("/api/access") !== -1
      ? Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(
          { allowed: true, pending: false, has_admins: true, admin: false }) })
      : old(u, o));
    window.dispatchEvent(new Event("focus"));
  });
  await page.waitForTimeout(600);
  assert.ok(await page.evaluate(() =>
    document.getElementById("stats-view").classList.contains("hidden")),
    "экран остался открытым после снятия прав");
  await page.close();
});

test("цифры сервера доезжают на экран", async () => {
  const page = await openApp(browser, { skin: "light", width: 360, height: 1400, routes: ADMIN });
  await page.waitForTimeout(500);
  await page.click("#stats-btn");
  await page.waitForTimeout(500);

  const text = await page.evaluate(() => document.getElementById("stats-body").textContent);
  const must = [
    "Авторов за период", "@petya (Пётр)",
    "Доступ есть, заявок нет", "@lena, @vasya",
    "2 дн.", "По 21 оплаченным.",
    "Ждут дольше 3 дн.", "Просрочено сейчас",
    "Аренда", "Оплачено без акта / УПД",
  ];
  must.forEach((s) => assert.ok(text.indexOf(s) !== -1, `на экране нет «${s}»`));
  // Суммы форматируются как везде в приложении и не смешивают валюты.
  assert.ok(text.indexOf("552 705,50 RUB · 300 USD") !== -1, text.slice(0, 300));
  assert.ok(text.indexOf("196 910 RUB") !== -1, "суммы просрочки нет");

  // То, что требует действия, выделено; спокойные числа — нет.
  const warns = await page.evaluate(() =>
    [...document.querySelectorAll("#stats-body .st-stat.st-warn .st-label")]
      .map((e) => e.textContent));
  assert.deepEqual(warns.sort(), [
    "Без счёта и реквизитов", "Ждут дольше 3 дн.",
    "Оплачено без акта / УПД", "Просрочено сейчас",
  ]);
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("нечего мерить — так и написано, а не ноль", async () => {
  // Ноль дней от подачи до оплаты — неправда: оплат ещё не было.
  const empty = JSON.parse(JSON.stringify(DATA));
  empty.flow.median_days = null;
  empty.flow.paid_measured = 0;
  empty.people.top = [];
  empty.people.idle = [];
  const page = await openApp(browser, { skin: "light", width: 360, height: 1200, routes: {
    ...ADMIN, "/api/admin/analytics": empty,
  } });
  await page.waitForTimeout(500);
  await page.click("#stats-btn");
  await page.waitForTimeout(500);
  const text = await page.evaluate(() => document.getElementById("stats-body").textContent);
  assert.ok(text.indexOf("Ещё нечего мерить") !== -1, "медиана показана нулём");
  assert.ok(text.indexOf("За период заявок не было.") !== -1, "пустой список молчит");
  await page.close();
});

test("переключение периода пересчитывает на сервере", async () => {
  const page = await openApp(browser, { skin: "light", width: 360, routes: ADMIN });
  await page.waitForTimeout(500);
  await page.click("#stats-btn");
  await page.waitForTimeout(400);
  await page.evaluate(() => {
    document.querySelector('#stats-days button[data-value="7"]').click();
  });
  await page.waitForTimeout(400);
  const asked = await page.evaluate(() =>
    window.__gets.filter((u) => u.indexOf("/api/admin/analytics?days=7") !== -1).length);
  assert.equal(asked, 1, "период не ушёл на сервер");
  assert.equal(
    await page.evaluate(() =>
      document.querySelectorAll("#stats-days button.active").length), 1,
    "активных кнопок периода не одна");
  await page.close();
});

test("отказ сервера виден, а не пустой экран", async () => {
  const page = await openApp(browser, { skin: "light", width: 360, routes: {
    ...ADMIN,
    "/api/admin/analytics": { __status: 403, detail: "Только для администраторов бота." },
  } });
  await page.waitForTimeout(500);
  await page.click("#stats-btn");
  await page.waitForTimeout(400);
  const msg = await page.evaluate(() => document.getElementById("stats-msg").textContent);
  assert.match(msg, /Только для администраторов/, `сообщение: «${msg}»`);
  await page.close();
});
