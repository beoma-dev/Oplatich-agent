/*
 * Браузерные проверки формы: шапка, подсказки и их крестики.
 *
 * Всё здесь — регрессии, которые НЕ ловились тестами по исходнику: каскад
 * CSS, фактическая раскладка и поведение при наведении.
 */
const test = require("node:test");
const assert = require("node:assert");
const { launch, openApp } = require("./helpers.cjs");

const HINTS = {
  "/api/counterparties": { items: [
    { name: "ООО «Ромашка»" }, { name: "ООО «Юрбюро»" }, { name: "124у12" },
    { name: "Alibaba Group" }, { name: "ИП Кузнецов" }, { name: "ООО «Ласточка»" },
  ] },
  "/api/access": { allowed: true, pending: false, has_admins: true },
};

let browser;
test.before(async () => { browser = await launch(); });
test.after(async () => { await browser.close(); });

test("шапка держится в одну строку при всех иконках", async () => {
  for (const width of [360, 390, 440, 520]) {
    const page = await openApp(browser, { skin: "neon", width, routes: HINTS });
    await page.evaluate(() => {
      ["fin-btn", "admin-btn"].forEach((id) =>
        document.getElementById(id).classList.remove("hidden"));
      window.dispatchEvent(new Event("resize"));
    });
    await page.waitForTimeout(300);
    const info = await page.evaluate(() => {
      const header = document.querySelector("#form-view header");
      const brand = header.querySelector(".brand");
      const name = brand.querySelector("span");
      const gears = [...header.querySelectorAll(".gear")]
        .filter((g) => !g.classList.contains("hidden"));
      const box = brand.getBoundingClientRect();
      const mid = box.top + box.height / 2;
      return {
        wrapped: name.getClientRects().length > 1,
        overlap: box.right > Math.min(...gears.map((g) => g.getBoundingClientRect().left)),
        offset: Math.max(...gears.map((g) => {
          const r = g.getBoundingClientRect();
          return Math.abs(r.top + r.height / 2 - mid);
        })),
      };
    });
    assert.equal(info.wrapped, false, `${width}px: имя перенеслось на вторую строку`);
    assert.equal(info.overlap, false, `${width}px: имя налезло на иконки`);
    assert.ok(info.offset <= 1, `${width}px: иконки не по центру строки (${info.offset}px)`);
    assert.deepEqual(page.errors, []);
    await page.close();
  }
});

test("подсказки контрагентов листаются колесом мыши", async () => {
  const page = await openApp(browser, { skin: "neon", width: 390, routes: HINTS });
  const before = await page.evaluate(() => {
    const box = document.getElementById("cp-chips");
    return { overflow: box.scrollWidth > box.clientWidth, left: box.scrollLeft,
             classes: box.className };
  });
  assert.ok(before.overflow, "список подсказок должен переполняться в этом тесте");
  assert.match(before.classes, /fade-r/, "нет намёка, что список продолжается");

  await page.hover("#cp-chips .chip");
  await page.mouse.wheel(0, 200);
  await page.waitForTimeout(250);
  const after = await page.evaluate(() => {
    const box = document.getElementById("cp-chips");
    return { left: box.scrollLeft, classes: box.className };
  });
  assert.ok(after.left > before.left, "вертикальное колесо не прокрутило список вбок");
  assert.match(after.classes, /fade-l/, "нет намёка, что список продолжается влево");
  await page.close();
});

test("статьи расходов — один ряд под полем, тоже листается", async () => {
  const page = await openApp(browser, { skin: "neon", width: 360, routes: HINTS });
  const info = await page.evaluate(() => {
    const seg = document.getElementById("article-seg");
    const input = document.getElementById("article-custom");
    const tops = new Set([...seg.children].map((b) =>
      Math.round(b.getBoundingClientRect().top)));
    return { below: seg.getBoundingClientRect().top > input.getBoundingClientRect().top,
             rows: tops.size, scrollable: seg.scrollWidth > seg.clientWidth };
  });
  assert.ok(info.below, "подсказки должны стоять под полем ввода");
  assert.equal(info.rows, 1, "ряд статей перенёсся на несколько строк");
  assert.ok(info.scrollable, "ряд статей должен листаться");
  await page.close();
});

test("крестики подсказок краснеют одинаково в обеих шкурах", async () => {
  for (const skin of ["neon", "tg"]) {
    const page = await openApp(browser, { skin, width: 430, routes: HINTS });
    const read = async (sel) => {
      await page.hover(sel);
      await page.waitForTimeout(700);
      return page.evaluate((s) => {
        const st = getComputedStyle(document.querySelector(s));
        return st.color;
      }, sel);
    };
    const chip = await read("#cp-chips .chip-x");
    const article = await read("#article-seg .seg-x");
    assert.equal(chip, article, `${skin}: крестики подсвечиваются по-разному`);
    await page.close();
  }
});

test("родная кнопка Telegram красится акцентом шкуры", async () => {
  const neon = await openApp(browser, { skin: "neon", routes: HINTS });
  assert.deepEqual(await neon.evaluate(() => window.__params),
    [{ color: "#10b981", text_color: "#04140d" }]);
  await neon.close();
});
