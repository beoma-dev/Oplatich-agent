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


/** Заполняет всё обязательное: сумма, контрагент, статья, срок работ и
 *  реквизиты (вариант «счёта нет» — файл в тесте не приложить). */
async function fillRequired(page) {
  await page.evaluate(() => {
    const set = (id, value) => {
      const el = document.getElementById(id);
      el.value = value;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    };
    set("amount", "1000");
    set("counterparty", "ООО «Ромашка»");
    set("article-custom", "Аренда");
    set("work-deadline", "текущий месяц");
    document.querySelector('#invoice-seg button[data-value="0"]').click();
    set("requisites", "ИНН 7707083893");
  });
  await page.waitForTimeout(250);
}

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

test("подсказки статей и контрагентов одного размера", async () => {
  // Два списка подсказок рядом: разный кегль и высота читались как разнобой.
  for (const skin of ["neon", "tg"]) {
    const page = await openApp(browser, { skin, width: 430, routes: HINTS });
    const size = await page.evaluate(() => {
      const m = (sel) => {
        const el = document.querySelector(sel);
        const st = getComputedStyle(el);
        return [Math.round(el.getBoundingClientRect().height), st.fontSize,
                st.paddingTop, st.paddingLeft, st.borderTopLeftRadius];
      };
      return { chip: m("#cp-chips .chip"), article: m("#article-seg button"),
               crossChip: getComputedStyle(document.querySelector("#cp-chips .chip-x")).fontSize,
               crossArticle: getComputedStyle(document.querySelector("#article-seg .seg-x")).fontSize };
    });
    assert.deepEqual(size.article, size.chip, `${skin}: подсказки разного размера`);
    assert.equal(size.crossArticle, size.crossChip, `${skin}: крестики разного размера`);
    await page.close();
  }
});

test("родная кнопка серая на пустой форме и акцентная на заполненной", async () => {
  // Пустая форма — кнопка неготова: красим токеном подсказки. Гасить её нельзя,
  // погашенную не нажать, и человек не узнает, чего не хватает.
  const neon = await openApp(browser, { skin: "neon", routes: HINTS });
  assert.deepEqual((await neon.evaluate(() => window.__params))[0],
    { color: "#8fa09b", text_color: "#04140d" }, "пустая форма — серая кнопка");

  await fillRequired(neon);
  const params = await neon.evaluate(() => window.__params);
  assert.deepEqual(params[params.length - 1],
    { color: "#10b981", text_color: "#04140d" }, "заполненная — акцентная");
  await neon.close();
});

test("незаполненная форма объясняет, чего не хватает", async () => {
  const page = await openApp(browser, { skin: "neon", routes: HINTS });

  // Подсказка видна сразу, без нажатий, и перечисляет обязательные поля.
  const hint = await page.evaluate(() => {
    const box = document.getElementById("gaps-hint");
    return { hidden: box.classList.contains("hidden"), text: box.textContent };
  });
  assert.equal(hint.hidden, false, "подсказка должна быть видна на пустой форме");
  for (const label of ["Сумма", "Контрагент", "Статья расходов", "Срок исполнения"]) {
    assert.ok(hint.text.includes(label), `в подсказке нет «${label}»: ${hint.text}`);
  }

  // Кнопка-заглушка серая — тот же признак неготовности вне Telegram.
  assert.ok(await page.evaluate(() =>
    document.getElementById("submit-fallback").classList.contains("incomplete")));

  // Нажатие открывает окно со списком, а не молчит.
  await page.evaluate(() => document.getElementById("submit-fallback").click());
  await page.waitForTimeout(200);
  const modal = await page.evaluate(() => ({
    shown: document.getElementById("modal").classList.contains("shown"),
    title: document.getElementById("modal-title").textContent,
    items: [...document.querySelectorAll("#modal-text .modal-gaps li")]
      .map((li) => li.textContent),
  }));
  assert.equal(modal.shown, true, "окно со списком не открылось");
  assert.ok(modal.items.includes("Контрагент"), `в окне нет контрагента: ${modal.items}`);
  assert.ok(modal.items.length >= 4, `в окне мало пунктов: ${modal.items}`);

  await page.close();
});

test("заполненная форма прячет подсказку и красит кнопку", async () => {
  const page = await openApp(browser, { skin: "neon", routes: HINTS });
  await fillRequired(page);
  const state = await page.evaluate(() => ({
    hintHidden: document.getElementById("gaps-hint").classList.contains("hidden"),
    grey: document.getElementById("submit-fallback").classList.contains("incomplete"),
  }));
  assert.equal(state.hintHidden, true, "подсказка осталась на заполненной форме");
  assert.equal(state.grey, false, "кнопка осталась серой на заполненной форме");
  await page.close();
});

test("счёт стоит первым блоком формы", async () => {
  // Бета читает приложенный файл и предлагает заполнить сумму, контрагента
  // и реквизиты, но только ПУСТЫЕ поля. Стоя последним, блок счёта делал
  // распознавание бесполезным: к нему доходили с уже заполненной формой.
  const page = await openApp(browser, { skin: "neon", width: 430, routes: HINTS });
  const order = await page.evaluate(() =>
    [...document.querySelectorAll("#form-view .card .card-title")]
      .map((t) => t.textContent.trim().split(" ")[1]));
  assert.equal(order[0], "Счёт", `первым идёт «${order[0]}», а не счёт`);
  assert.deepEqual(order.slice(0, 3), ["Счёт", "Платёж", "Получатель"]);
  await page.close();
});

test("козырёк Оплатыча берёт цвет темы", async () => {
  // В телеграмной шкуре акцент — кнопочный цвет клиента (по умолчанию синий),
  // в неоне — изумруд. Марка должна следовать за ним, а не быть зашитой.
  const seen = {};
  for (const skin of ["tg", "neon"]) {
    const page = await openApp(browser, { skin, width: 430, routes: HINTS });
    seen[skin] = await page.evaluate(() => {
      const brim = document.querySelector("#brand-mark path[fill^='url(#mk-brim']");
      const check = document.querySelector("#brand-mark circle[stroke='var(--accent)']");
      const accent = getComputedStyle(document.documentElement)
        .getPropertyValue("--accent").trim();
      return { accent, brim: getComputedStyle(brim).stroke,
               check: getComputedStyle(check).stroke };
    });
    await page.close();
  }
  assert.notEqual(seen.tg.brim, seen.neon.brim, "козырёк одинаков в обеих шкурах");
  assert.notEqual(seen.tg.check, seen.neon.check);
  assert.match(seen.neon.accent, /10B981/i, "в неоне акцент — изумруд");
  await Promise.resolve();
});

test("марка в пустом списке рисуется полностью", async () => {
  // Клон берёт градиенты из общего блока: пока они лежали в скрытой шапке,
  // у персонажа пропадали и шерсть, и бумага.
  const page = await openApp(browser, { skin: "tg", width: 430, routes: {
    "/api/access": { allowed: true, financier: false, admin: false,
                     pending: false, has_admins: true },
    "/api/my-requests": { items: [] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(500);
  const art = await page.evaluate(() => {
    const el = document.querySelector("#my-list .empty-art");
    if (!el) return null;
    const box = el.getBoundingClientRect();
    return { width: Math.round(box.width), height: Math.round(box.height),
             fur: !!el.querySelector("[fill='url(#mk-fur)']") };
  });
  assert.ok(art, "персонажа в пустом списке нет");
  assert.ok(art.width > 40 && art.height > 40, "марка схлопнулась");
  assert.ok(art.fur, "клон потерял заливку шерсти");
  await page.close();
});
