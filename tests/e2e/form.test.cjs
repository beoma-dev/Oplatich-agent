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
      // Меряем САМОГО героя, а не рамку .brand: отступ под панель кнопок
      // теперь лежит внутри .brand, и её рамка по определению доходит до
      // иконок. Наложение — это когда на них налезает содержимое.
      const art = document.getElementById("brand-mark").getBoundingClientRect();
      return {
        wrapped: name.getClientRects().length > 1,
        overlap: art.right > Math.min(...gears.map((g) => g.getBoundingClientRect().left)) + 1,
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

test("брак в поле виден сразу, а не после отправки", async () => {
  // «ннннннннн» сервер отклонит и сам, но человек узнавал бы об этом только
  // после нажатия, а печатая — не видел ничего.
  const page = await openApp(browser, { skin: "tg", routes: HINTS });
  await fillRequired(page);
  const set = (id, v) => page.evaluate(([i, x]) => {
    const el = document.getElementById(i);
    el.value = x;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, [id, v]);
  const hint = () => page.evaluate(() => ({
    text: document.getElementById("gaps-hint").textContent,
    hidden: document.getElementById("gaps-hint").classList.contains("hidden"),
    grey: document.getElementById("submit-fallback").classList.contains("incomplete"),
  }));

  await set("counterparty", "н".repeat(22));
  await page.waitForTimeout(200);
  let state = await hint();
  assert.match(state.text, /повторяется/, `не назвали причину: ${state.text}`);
  assert.equal(state.grey, true, "кнопка должна погаснуть");

  await set("counterparty", "12321432132132132");
  await page.waitForTimeout(200);
  state = await hint();
  assert.match(state.text, /нет ни одной буквы/, `не назвали причину: ${state.text}`);

  // Кириллица — это буквы: \W в JS работает по ASCII и когда-то их не считал.
  await set("counterparty", "ООО «Ромашка»");
  await page.waitForTimeout(200);
  state = await hint();
  assert.equal(state.hidden, true, `подсказка осталась: ${state.text}`);
  assert.equal(state.grey, false, "кнопка осталась серой на верном значении");
  await page.close();
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

  // Счёт и реквизиты с 26.08.2026 необязательны, обязательных полей стало
  // четыре. Пятое добираем настраиваемой датой — иначе форма «полей» во
  // множественном числе не проверить, а склонение и есть смысл теста.
  await page.click('#urgency-seg button[data-value="CUSTOM"]');
  await page.waitForTimeout(150);
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

test("подписи экранов умещаются в одну строку", async () => {
  // Отступ под панель кнопок стоял на ВСЕЙ шапке, хотя панель занимает только
  // строку с героем (48…88) и подписи (118…159) не мешает. Из-за этого подписи
  // доставалось 128 px из 288, и любая осмысленная фраза ломалась надвое.
  const lines = (page, view) => page.evaluate((v) => {
    const p = document.querySelector("#" + v + " header p");
    const node = p.firstChild;
    const rng = document.createRange();
    const tops = new Set();
    for (let i = 0; i < node.length; i++) {
      rng.setStart(node, i); rng.setEnd(node, i + 1);
      const r = rng.getBoundingClientRect();
      if (r.width || r.height) tops.add(Math.round(r.top));
    }
    return { rows: tops.size, text: p.textContent.trim() };
  }, view);

  for (const width of [320, 360, 393, 430]) {
    const page = await openApp(browser, { skin: "tg", width, height: 800, routes: HINTS });
    const form = await lines(page, "form-view");
    assert.equal(form.rows, 1, `${width}px: «${form.text}» в ${form.rows} строки`);

    await page.evaluate(() => document.getElementById("my-btn").click());
    await page.waitForTimeout(300);
    const my = await lines(page, "my-view");
    assert.equal(my.rows, 1, `${width}px: «${my.text}» в ${my.rows} строки`);
    await page.close();
  }
});

test("панель кнопок не налезает на строку с героем", async () => {
  // Отступ переехал с шапки на .brand — проверяем, что он там и работает.
  const page = await openApp(browser, { skin: "tg", width: 320, routes: HINTS });
  await page.evaluate(() => ["fin-btn", "admin-btn"].forEach((id) =>
    document.getElementById(id).classList.remove("hidden")));
  await page.evaluate(() => window.dispatchEvent(new Event("resize")));
  await page.waitForTimeout(350);
  const ok = await page.evaluate(() => {
    const mark = document.getElementById("brand-mark").getBoundingClientRect();
    const panel = document.getElementById("header-icons").getBoundingClientRect();
    return mark.right <= panel.left + 1;
  });
  assert.equal(ok, true, "герой налез на панель кнопок");
  await page.close();
});

test("падение скрипта видно человеку и уходит админам", async () => {
  // Раньше исключение мимо catch оставляло застывшую форму и полную тишину.
  const page = await openApp(browser, { skin: "tg", routes: {
    "/api/access": { allowed: true, financier: false, admin: false,
                     pending: false, has_admins: true },
    "/api/client-error": { ok: true },
  } });
  await page.evaluate(() => {
    window.dispatchEvent(new ErrorEvent("error", {
      message: "TypeError: сломалось", filename: "app.js", lineno: 42 }));
  });
  await page.waitForTimeout(200);
  const seen = await page.evaluate(() => {
    const post = window.__posts.find((p) => p[0].indexOf("/api/client-error") !== -1);
    const banner = document.getElementById("error-banner");
    return {
      sent: post ? JSON.parse(post[1]) : null,
      shown: banner.style.display === "block",
      text: banner.textContent,
    };
  });
  assert.ok(seen.sent, "админам не сообщили");
  assert.match(seen.sent.message, /сломалось/);
  assert.match(seen.sent.where, /app\.js:42/);
  assert.ok(seen.shown, "человеку ничего не показали");
  assert.match(seen.text, /Закройте и откройте её заново/);

  // Повтор той же ошибки не должен слать второе сообщение с той же страницы.
  await page.evaluate(() => {
    window.dispatchEvent(new ErrorEvent("error", { message: "TypeError: сломалось" }));
  });
  await page.waitForTimeout(150);
  const count = await page.evaluate(() =>
    window.__posts.filter((p) => p[0].indexOf("/api/client-error") !== -1).length);
  assert.equal(count, 1, "одного сообщения о падении достаточно");
  await page.close();
});

test("плашка контура появляется только на стенде", async () => {
  // Два бота выглядят одинаково: спутать их — значит подать настоящую заявку
  // в пустоту или наоборот.
  const boevoy = await openApp(browser, { skin: "tg", routes: {
    "/api/access": { allowed: true, financier: false, admin: false,
                     pending: false, has_admins: true, env_label: "" },
  } });
  assert.ok(await boevoy.evaluate(() =>
    document.getElementById("env-banner").classList.contains("hidden")),
    "на боевом плашки быть не должно");
  await boevoy.close();

  const stend = await openApp(browser, { skin: "tg", routes: {
    "/api/access": { allowed: true, financier: false, admin: false,
                     pending: false, has_admins: true, env_label: "СТЕНД" },
  } });
  const shown = await stend.evaluate(() => {
    const b = document.getElementById("env-banner");
    const form = document.querySelector("#form-view header");
    return {
      hidden: b.classList.contains("hidden"),
      text: b.textContent,
      // Плашка обязана быть НАД формой, иначе её не заметят.
      aboveHeader: b.getBoundingClientRect().top < form.getBoundingClientRect().top,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  });
  assert.equal(shown.hidden, false, "на стенде плашки нет");
  assert.match(shown.text, /СТЕНД/);
  assert.match(shown.text, /никому не уходят/);
  assert.ok(shown.aboveHeader, "плашка ниже шапки — её не увидят");
  assert.equal(shown.overflow, 0, "плашка вызвала горизонтальную прокрутку");
  await stend.close();
});

test("SDK не загрузился, но авторизация есть — кнопка отправки на месте", async () => {
  // Регресс 26.08.2026: initData научились читать из хеша, и появилось
  // состояние «авторизован, но tg === null». Кнопка отправки висела на
  // insideTelegram вместе с предупреждением, поэтому в этом состоянии
  // человек видел заполненную форму и НИ ОДНОЙ кнопки отправки:
  // родную рисует SDK, которого нет, а запасную прятало условие.
  const hash =
    "#tgWebAppData=query_id%3DAAE%26user%3D%257B%2522id%2522%253A42%257D" +
    "%26auth_date%3D1787000000%26hash%3Ddeadbeef&tgWebAppVersion=7.0";
  const page = await openApp(browser, { noSdk: true, hash });
  const state = await page.evaluate(() => ({
    sdk: !!(window.Telegram && window.Telegram.WebApp),
    submit: getComputedStyle(document.getElementById("submit-fallback")).display,
    note: getComputedStyle(document.getElementById("fallback-note")).display,
  }));
  assert.equal(state.sdk, false, "заглушка SDK всё-таки установилась");
  assert.notEqual(state.submit, "none", "кнопки отправки нет — отправить нечем");
  assert.equal(state.note, "none", "предупреждение об авторизации показано зря");
  await page.close();
});

test("нет ни SDK, ни авторизации — и кнопка, и предупреждение", async () => {
  const page = await openApp(browser, { noSdk: true });
  const state = await page.evaluate(() => ({
    submit: getComputedStyle(document.getElementById("submit-fallback")).display,
    note: getComputedStyle(document.getElementById("fallback-note")).display,
  }));
  assert.notEqual(state.submit, "none");
  assert.notEqual(state.note, "none", "человека не предупредили, что отправка не пройдёт");
  await page.close();
});

test("дополнительные документы: несколько файлов, список и предел", async () => {
  const page = await openApp(browser, { routes: HINTS });
  const mk = (name) => ({ name, mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4") });

  await page.setInputFiles("#extra-input", [mk("dogovor.pdf"), mk("akt.pdf")]);
  await page.waitForTimeout(150);
  let shown = await page.evaluate(() => ({
    строк: document.querySelectorAll("#extra-list .extra-item").length,
    имена: [...document.querySelectorAll("#extra-name, .extra-name")].map((e) => e.textContent),
    кнопка: document.getElementById("extra-pick").textContent,
  }));
  assert.equal(shown.строк, 2, "приложились не оба файла");
  assert.ok(/dogovor\.pdf/.test(shown.имена.join(" ")));
  assert.match(shown.кнопка, /2 из 5/, "счётчик на кнопке не обновился");

  // Убрать один — список пересобирается.
  await page.click("#extra-list .extra-item .row-del");
  await page.waitForTimeout(100);
  assert.equal(
    await page.evaluate(() => document.querySelectorAll("#extra-list .extra-item").length), 1);

  // Шестой файл не принимается: предел зеркалит bot/validators.MAX_EXTRA_FILES.
  await page.setInputFiles("#extra-input",
    ["a", "b", "c", "d", "e"].map((n) => mk(n + ".pdf")));
  await page.waitForTimeout(150);
  shown = await page.evaluate(() => ({
    строк: document.querySelectorAll("#extra-list .extra-item").length,
    ошибка: (document.getElementById("error-banner") || {}).textContent || "",
  }));
  assert.equal(shown.строк, 5, "предел в 5 документов не удержан");
  assert.match(shown.ошибка, /Больше 5/, "человеку не сказали, почему файл не взяли");
  // Список на экране обязан совпадать с тем, что уйдёт на сервер: ранний
  // выход из обработчика однажды оставил принятые файлы невидимыми.
  assert.equal(
    await page.evaluate(() => document.querySelectorAll("#extra-list .extra-item").length),
    await page.evaluate(() => window.__extrasCount === undefined ? 5 : window.__extrasCount),
    "показано не то, что приложено");
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("чужой формат в дополнительных документах не принимается", async () => {
  const page = await openApp(browser, { routes: HINTS });
  await page.setInputFiles("#extra-input", [{
    name: "virus.exe", mimeType: "application/octet-stream", buffer: Buffer.from("MZ"),
  }]);
  await page.waitForTimeout(150);
  const r = await page.evaluate(() => ({
    строк: document.querySelectorAll("#extra-list .extra-item").length,
    ошибка: (document.getElementById("error-banner") || {}).textContent || "",
  }));
  assert.equal(r.строк, 0);
  assert.match(r.ошибка, /PDF, JPG, PNG или XLSX/);
  await page.close();
});

test("счёт и реквизиты необязательны: форма отправляется пустой", async () => {
  // Раньше требовалось одно из двух, и человек вписывал выдуманные
  // реквизиты, лишь бы форма пропустила. Пустое поле честнее.
  const page = await openApp(browser, { skin: "neon", routes: HINTS });
  const fill = (id, value) => page.evaluate(([i, v]) => {
    const el = document.getElementById(i);
    el.value = v;
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }, [id, value]);
  await fill("amount", "1000");
  await fill("counterparty", "ООО «Ромашка»");
  await fill("article-custom", "Аренда");
  await fill("work-deadline", "текущий месяц");
  await page.waitForTimeout(200);

  const state = await page.evaluate(() => ({
    подсказка: document.getElementById("gaps-hint").classList.contains("hidden"),
    текст: document.getElementById("gaps-hint").textContent,
  }));
  assert.equal(state.подсказка, true,
    `форма всё ещё чего-то требует: ${state.текст}`);

  // И «со счётом» без файла — тоже полная форма.
  await page.click('#invoice-seg button[data-value="1"]');
  await page.waitForTimeout(200);
  assert.ok(await page.evaluate(() =>
    document.getElementById("gaps-hint").classList.contains("hidden")),
    "выбран «файл счёта», файла нет — форма не должна этого требовать");
  await page.close();
});
