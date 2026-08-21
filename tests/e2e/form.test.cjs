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

test("подозрительный текст спрашивает подтверждение, а не отказывает", async () => {
  // Сервер отвечает 409 «похоже на набор символов»: эвристика иногда
  // ошибётся, и сорванная оплата дороже одной кривой строки.
  const page = await openApp(browser, {
    skin: "tg",
    routes: {
      ...HINTS,
      "/api/invoice": {
        __status: 409, suspicious: true, fields: ["Контрагент"],
        detail: "Похоже на случайный набор символов: Контрагент",
      },
    },
  });
  await fillRequired(page);
  await page.evaluate(() => document.getElementById("submit-fallback").click());
  await page.waitForTimeout(400);

  const modal = await page.evaluate(() => ({
    shown: document.getElementById("modal").classList.contains("shown"),
    title: document.getElementById("modal-title").textContent,
    text: document.getElementById("modal-text").textContent,
    marked: document.getElementById("counterparty").classList.contains("invalid"),
    actions: [...document.querySelectorAll("#modal-actions button")].map((b) => b.textContent),
  }));
  assert.equal(modal.shown, true, "окно подтверждения не открылось");
  assert.match(modal.title, /Проверьте/, `неожиданный заголовок: ${modal.title}`);
  assert.match(modal.text, /Контрагент/, "в окне не назвали поле");
  assert.equal(modal.marked, true, "поле не подсвечено");
  assert.ok(modal.actions.some((t) => /Всё равно отправить/.test(t)),
    `нет кнопки подтверждения: ${modal.actions}`);

  // Подтверждение уходит отдельным флагом — общий force заодно отключал бы
  // проверку на дубль.
  const before = await page.evaluate(() => window.__posts.length);
  await page.evaluate(() => [...document.querySelectorAll("#modal-actions button")]
    .find((b) => /Всё равно отправить/.test(b.textContent)).click());
  await page.waitForTimeout(400);
  const sent = await page.evaluate((n) => {
    const body = window.__posts[n][1];
    return { confirm: body.get("confirm_text"), force: body.get("force") };
  }, before);
  assert.equal(sent.confirm, "1", "подтверждение текста не ушло");
  assert.equal(sent.force, "0", "подтверждение текста не должно снимать проверку дубля");
  await page.close();
});

test("герой остаётся на месте после возврата с вкладки", async () => {
  // Пересчёт шапки прилетает асинхронно (ответ доступа, опрос финансиста).
  // Попав на скрытую форму, он мерил нули: отступ схлопывался, и герой
  // уезжал на 80 с лишним пикселей, налезая на панель кнопок.
  const page = await openApp(browser, { skin: "tg", routes: HINTS });
  const geom = () => page.evaluate(() => {
    const h = document.querySelector("#form-view header");
    return { pad: getComputedStyle(h).paddingRight,
             left: Math.round(document.getElementById("brand-mark").getBoundingClientRect().left) };
  });
  const before = await geom();
  await page.evaluate(() => document.getElementById("my-btn").click());
  await page.waitForTimeout(250);
  // пересчёт на скрытой форме
  await page.evaluate(() => {
    document.getElementById("fin-btn").classList.remove("hidden");
    window.dispatchEvent(new Event("resize"));
  });
  await page.waitForTimeout(350);
  await page.evaluate(() => document.getElementById("my-close").click());
  await page.waitForTimeout(350);
  const after = await geom();
  assert.equal(after.left, before.left,
    `герой съехал: было ${before.left}px (${before.pad}), стало ${after.left}px (${after.pad})`);
  await page.close();
});

test("кнопки отправки нет на инструкции даже после пересчёта", async () => {
  const page = await openApp(browser, { skin: "tg", routes: HINTS });
  const visible = () => page.evaluate(() => window.Telegram.WebApp.MainButton.isVisible);
  assert.equal(await visible(), true, "на форме кнопка должна быть");

  await page.evaluate(() => document.getElementById("help-btn").click());
  await page.waitForTimeout(250);
  assert.equal(await visible(), false, "инструкция открылась, а кнопка осталась");

  // Асинхронный пересчёт возвращал кнопку поверх чужого экрана.
  await page.evaluate(() => {
    const el = document.getElementById("counterparty");
    el.value = "ООО «Ромашка»";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.waitForTimeout(300);
  assert.equal(await visible(), false, "кнопка всплыла на инструкции после пересчёта");

  await page.evaluate(() => document.getElementById("help-close").click());
  await page.waitForTimeout(300);
  assert.equal(await visible(), true, "вернулись на форму, а кнопки нет");
  await page.close();
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
  // «Осталось заполнить 5:» — пять чего? Число без существительного не читается.
  assert.match(hint.text, /Осталось заполнить \d+ (поле|поля|полей):/,
    `у числа нет существительного: ${hint.text}`);
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
    inView: (() => {
      const r = document.querySelector("#modal .modal-box").getBoundingClientRect();
      return r.top >= 0 && r.bottom <= window.innerHeight;
    })(),
    box: (() => {
      const r = document.querySelector("#modal .modal-box").getBoundingClientRect();
      return { top: Math.round(r.top), bottom: Math.round(r.bottom) };
    })(),
  }));
  assert.equal(modal.shown, true, "окно со списком не открылось");
  // Окно, до которого надо доскроллить, — не окно: панель обязана попадать
  // в экран целиком, иначе список остаётся непрочитанным.
  assert.equal(modal.inView, true, `панель окна вне экрана: ${JSON.stringify(modal.box)}`);
  assert.ok(modal.items.includes("Контрагент"), `в окне нет контрагента: ${modal.items}`);
  assert.ok(modal.items.length >= 4, `в окне мало пунктов: ${modal.items}`);

  await page.close();
});

test("число незаполненного склоняется по-русски", async () => {
  const page = await openApp(browser, { skin: "neon", routes: HINTS });
  const read = () => page.evaluate(() =>
    document.getElementById("gaps-hint").textContent);
  const fill = (id, value) => page.evaluate(([i, v]) => {
    const el = document.getElementById(i);
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, [id, value]);

  assert.match(await read(), /5 полей:/, "на пяти ждём «полей»");
  await fill("amount", "1000");
  await page.waitForTimeout(150);
  assert.match(await read(), /4 поля:/, "на четырёх ждём «поля»");
  await fill("counterparty", "ООО «Ромашка»");
  await fill("article-custom", "Аренда");
  await page.waitForTimeout(150);
  assert.match(await read(), /2 поля:/, "на двух ждём «поля»");
  await fill("work-deadline", "текущий месяц");
  await page.waitForTimeout(150);
  assert.match(await read(), /одно поле:/, "на одном ждём «одно поле»");
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
