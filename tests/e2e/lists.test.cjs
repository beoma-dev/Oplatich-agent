/*
 * Списки заявок: «Мои заявки» и панель финансиста.
 *
 * Подробности открывает сама строка — проверяем именно поведение, а не
 * наличие обработчика в исходнике.
 */
const test = require("node:test");
const assert = require("node:assert");
const { launch, openApp } = require("./helpers.cjs");

const ITEM = {
  id: "INV-20260806-042807-9562", status: "Новая", sender: "Pavel Elipashev",
  counterparty: "ООО «Ромашка»", amount: "174 387,21", currency: "RUB",
  article: "Хостинг и ПО", comment: "тест", urgency: "NORMAL",
  planned_date: "07.08.2026", created_at: "2026-08-06 04:28",
  has_invoice: true, requisites: "", reason: "",
};

const ROUTES = {
  "/api/access": { allowed: true, financier: true, admin: true,
                   pending: false, has_admins: true },
  "/api/my-requests": { items: [ITEM] },
  "/api/finance/access": { ok: true },
  "/api/finance/requests": {
    items: [Object.assign({}, ITEM, { sender_username: "@tester" })], total: 1,
  },
};

let browser;
test.before(async () => { browser = await launch(); });
test.after(async () => { await browser.close(); });

for (const [name, button, list] of [
  ["мои заявки", "#my-btn", "#my-list"],
  ["панель финансиста", "#fin-btn", "#fin-req-list"],
]) {
  test(`${name}: подробности открывает строка, отдельной кнопки нет`, async () => {
    const page = await openApp(browser, { skin: "neon", width: 430, routes: ROUTES });
    await page.evaluate((id) => document.querySelector(id).classList.remove("hidden"), button);
    await page.click(button);
    await page.waitForTimeout(500);

    const row = await page.evaluate((sel) => {
      const el = document.querySelector(sel + " .my-item");
      return el && {
        tappable: el.classList.contains("tappable"),
        buttons: [...el.querySelectorAll("button")].map((b) => b.textContent.trim()),
      };
    }, list);
    assert.ok(row, `${name}: строка заявки не отрисовалась`);
    assert.ok(row.tappable, `${name}: строка не кликабельна`);
    assert.ok(!row.buttons.some((b) => b.includes("Подробнее")),
      `${name}: вернулась кнопка «Подробнее» — она дублирует нажатие по строке`);
    // Действия в ряду что-то МЕНЯЮТ, поэтому удаление осталось кнопкой.
    assert.ok(row.buttons.some((b) => b.includes("Удалить")), `${name}: нет удаления`);

    await page.locator(list + " .my-item").first().click();
    await page.waitForTimeout(400);
    const modal = await page.evaluate(() => ({
      shown: document.getElementById("modal").classList.contains("shown"),
      title: document.getElementById("modal-title").textContent,
    }));
    assert.ok(modal.shown, `${name}: нажатие по строке не открыло подробности`);
    assert.match(modal.title, /Ромашка/);
    assert.deepEqual(page.errors, []);
    await page.close();
  });
}
