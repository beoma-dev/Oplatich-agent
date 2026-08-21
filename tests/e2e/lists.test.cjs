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

test("недоступный реестр не выглядит как «заявок нет»", async () => {
  // Пустой список здесь — ложь про чужие заявки: ровно та подмена, что
  // однажды съела напоминания.
  const page = await openApp(browser, { skin: "neon", width: 430, routes: {
    "/api/access": { allowed: true, financier: false, admin: false,
                     pending: false, has_admins: true },
    "/api/my-requests": { __status: 403, detail: "Реестр сейчас недоступен." },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(500);
  const text = await page.evaluate(() =>
    document.getElementById("my-list").textContent.trim());
  assert.match(text, /недоступен/, `на экране «${text}»`);
  assert.ok(!/Заявок пока нет/.test(text), "показали «заявок нет» вместо ошибки");
  await page.close();
});

test("в пустом списке герой расстроен и не занимает пол-экрана", async () => {
  // Копия марки теряла класс .mark, а .empty-art в CSS не существовал —
  // SVG растягивался по ширине карточки во весь экран.
  const page = await openApp(browser, { skin: "tg", width: 393, height: 852,
    routes: { ...ROUTES, "/api/my-requests": { items: [] } } });
  await page.evaluate(() => document.getElementById("my-btn").click());
  await page.waitForTimeout(600);
  const art = await page.evaluate(() => {
    const el = document.querySelector(".empty-art");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    const card = el.closest(".card").getBoundingClientRect();
    return {
      h: Math.round(r.height), fits: r.width <= card.width,
      anim: getComputedStyle(el).animationName,
      mouth: el.querySelector("path[d^='M234 402']") !== null,
      lookingDown: [...el.querySelectorAll(".pup")]
        .every((p) => (p.getAttribute("transform") || "").indexOf("translate") === 0),
      innerIds: el.querySelectorAll("[id]").length,
      // Две позы руки и закрывающийся глаз: без них жест не читается, а
      // сдвиг самой руки отрывал лапу от корпуса.
      armUp: getComputedStyle(el.querySelector(".sad-arm-up") || el).animationName === "mk-arm-up",
      armDown: getComputedStyle(el.querySelector(".sad-arm-down") || el).animationName === "mk-arm-down",
      lid: getComputedStyle(el.querySelector(".sad-lid") || el).animationName === "mk-lid",
      tears: el.querySelectorAll(".sad-tear").length,
      papersHidden: el.querySelectorAll('[display="none"]').length >= 4,
      // Рука не должна ездить: сдвиг группы уносит плечо.
      armMoves: [...document.styleSheets].some((sh) => {
        try {
          return [...sh.cssRules].some((r) => r.name === "mk-wipe");
        } catch (e) { return false; }
      }),
      tearScales: [...document.styleSheets].some((sh) => {
        try {
          return [...sh.cssRules].some((r) => r.name === "mk-tear"
            && [...r.cssRules].some((k) => /scale\(/.test(k.style.transform)));
        } catch (e) { return false; }
      }),
    };
  });
  assert.ok(art, "марки в пустом списке нет");
  assert.ok(art.h > 90 && art.h < 180, `герой ${art.h}px — не тот размер`);
  assert.equal(art.fits, true, "герой шире карточки");
  assert.equal(art.anim, "mk-sad", "нет анимации расстроенного вида");
  assert.equal(art.mouth, true, "уголки рта не опущены");
  assert.equal(art.lookingDown, true, "взгляд не опущен");
  assert.equal(art.armUp, true, "нет поднятой руки");
  assert.equal(art.armDown, true, "опущенная рука не гаснет");
  assert.equal(art.lid, true, "глаз под лапой не закрывается");
  assert.equal(art.tears, 2, `слёз ${art.tears}, а не две`);
  assert.equal(art.papersHidden, true, "бумаги в лапах остались — заявок-то нет");
  // scale() у слезы недопустим: у SVG-элемента без transform-origin масштаб
  // считается от точки (0,0) области просмотра, и капля уезжает в угол.
  assert.equal(art.tearScales, false, "в анимации слезы появился scale()");
  assert.equal(art.armMoves, false, "вернулся сдвиг руки — лапа оторвётся от корпуса");
  // Копия и шапка не должны делить одни id на документ.
  assert.equal(art.innerIds, 0, "в копии остались внутренние id");
  await page.close();
});
