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

for (const [name, button, list, deleteInRow] of [
  // Админское удаление в обоих списках переехало в подробности: ряд не
  // тянул четвёртую кнопку. В «Моих заявках» рядом с «Отозвать» две красные
  // вдобавок читались как одно и то же, а в панели у новой заявки к трём
  // статусам добавлялась четвёртая и ряд ломался на 360px.
  ["мои заявки", "#my-btn", "#my-list", false],
  ["панель финансиста", "#fin-btn", "#fin-req-list", false],
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
    assert.equal(row.buttons.some((b) => b.includes("Удалить")), deleteInRow,
      `${name}: удаление в ряду — ${row.buttons.join("/")}`);

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
    };
  });
  assert.ok(art, "марки в пустом списке нет");
  assert.ok(art.h > 90 && art.h < 180, `герой ${art.h}px — не тот размер`);
  assert.equal(art.fits, true, "герой шире карточки");
  assert.equal(art.anim, "mk-sad", "нет анимации расстроенного вида");
  assert.equal(art.mouth, true, "уголки рта не опущены");
  assert.equal(art.lookingDown, true, "взгляд не опущен");
  // Копия и шапка не должны делить одни id на документ.
  assert.equal(art.innerIds, 0, "в копии остались внутренние id");
  await page.close();
});

test("к своей заявке можно приложить закрывающие документы", async () => {
  // Приходят после оплаты, иногда через месяц, поэтому кнопка живёт там,
  // где человек находит свой платёж, — в «Моих заявках».
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [{
      id: "INV-20260701-120000-0001", status: "Оплачена", counterparty: "ООО «Ромашка»",
      amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
      planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
      payment_source: "invoice", closing_count: 0, overdue: false,
    }] },
    "/api/my/closing-docs": { ok: true, count: 1, message: "Готово: документов у заявки — 1." },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);

  const btn = await page.evaluate(() => {
    const b = [...document.querySelectorAll("#my-list .my-actions button")]
      .find((x) => /Акт \/ УПД/.test(x.textContent));
    return b ? b.textContent.trim() : null;
  });
  assert.ok(btn, "кнопки закрывающих документов нет");

  await page.setInputFiles("#my-list input[type=file]", [{
    name: "akt.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.4"),
  }]);
  await page.waitForTimeout(400);
  const sent = await page.evaluate(() => {
    const post = window.__posts.filter((p) => p[0].indexOf("/api/my/closing-docs") !== -1).pop();
    return post ? [...post[1].keys()] : null;
  });
  assert.ok(sent, "запрос не ушёл");
  assert.ok(sent.includes("request_id") && sent.includes("files"), sent.join(","));
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("на кнопке видно, сколько документов уже приложено", async () => {
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [{
      id: "INV-20260701-120000-0002", status: "Оплачена", counterparty: "ООО «Ромашка»",
      amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
      planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
      payment_source: "invoice", closing_count: 2, overdue: false,
    }] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);
  const label = await page.evaluate(() => {
    const b = [...document.querySelectorAll("#my-list .my-actions button")]
      .find((x) => /Акт \/ УПД/.test(x.textContent));
    return b ? b.textContent.trim() : null;
  });
  assert.match(label || "", /\(2\)/, "счётчик на кнопке не показан");
  await page.close();
});

test("ряд кнопок заявки помещается и не обрезается", async () => {
  // Раньше кнопки делились на равные доли: длинная надпись переносилась
  // внутри кнопки, а одинокая кнопка на второй строке растягивалась на всю
  // ширину — 294px при экране в 360. Проверяем оба ряда, которые бывают:
  // просроченная «Новая» — самый длинный (три кнопки), «Оплачена» — та,
  // где живут закрывающие.
  const base = {
    counterparty: "ООО «Ромашка»", amount: "1000.00", currency: "RUB",
    article: "Аренда", urgency: "Обычная", planned_date: "05.07.2026",
    created_at: "2026-07-01 12:00", has_invoice: true,
    payment_source: "invoice", closing_count: 0,
  };
  const cases = [
    { item: { ...base, id: "INV-20260701-120000-0003", status: "Новая", overdue: true },
      expect: /Напомнить/ },
    { item: { ...base, id: "INV-20260701-120000-0004", status: "Оплачена", overdue: false },
      expect: /Акт \/ УПД/ },
  ];
  for (const { item, expect } of cases) {
    for (const width of [360, 390, 430]) {
      // Админом: у него в ряду была ЧЕТВЁРТАЯ кнопка, и именно этот случай
      // видит владелец бота — а мерили раньше без неё.
      const page = await openApp(browser, { skin: "neon", width, routes: {
        "/api/access": { allowed: true, pending: false, has_admins: true, admin: true },
        "/api/my-requests": { items: [item] },
      } });
      await page.click("#my-btn");
      await page.waitForTimeout(350);
      const r = await page.evaluate(() => {
        const btns = [...document.querySelectorAll("#my-list .my-actions button")];
        return {
          labels: btns.map((b) => b.textContent.trim()),
          clipped: btns.filter((b) => b.scrollWidth > b.clientWidth + 1)
            .map((b) => b.textContent.trim()),
          widths: btns.map((b) => Math.round(b.getBoundingClientRect().width)),
          multiline: btns.filter((b) => b.getBoundingClientRect().height > 32)
            .map((b) => b.textContent.trim()),
          rows: new Set(btns.map((b) => Math.round(b.getBoundingClientRect().top))).size,
        };
      });
      const where = `${item.status}/${width}px`;
      assert.deepEqual(r.clipped, [], `на ${where} надписи обрезаны`);
      assert.ok(r.labels.some((x) => expect.test(x)),
        `на ${where} нет ожидаемой кнопки: ${r.labels.join("/")}`);
      // Кнопки по ширине надписи, а не по равным долям ряда.
      assert.ok(r.widths.every((x) => x < width * 0.6),
        `на ${where} кнопка растянулась: ${r.widths.join("/")}`);
      assert.deepEqual(r.multiline, [], `на ${where} надпись перенеслась внутри кнопки`);
      // Один ряд на любом ходовом экране — ради этого кегль здесь меньше.
      assert.equal(r.rows, 1, `на ${where} ряд разъехался: ${r.widths.join("/")}`);
      // Админское «Удалить» из ряда убрано: рядом с «Отозвать» две красные
      // кнопки читались как одно и то же, а четвёртая ломала строку.
      assert.ok(!r.labels.some((x) => /Удалить/.test(x)),
        `на ${where} «Удалить» вернулась в ряд: ${r.labels.join("/")}`);
      assert.ok(!r.labels.some((x) => /Повторить/.test(x)),
        `на ${where} «Повторить» вернулась`);
      await page.close();
    }
  }
});

test("закрывающие не предлагаются, пока заявка не оплачена", async () => {
  // Акт приходит ПОСЛЕ оплаты. На «Новой» кнопка только занимала место
  // в ряду — а место там пересчитано под три кнопки, не под четыре.
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [{
      id: "INV-20260701-120000-0005", status: "Новая", counterparty: "ООО «Ромашка»",
      amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
      planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
      payment_source: "invoice", closing_count: 0, overdue: false,
    }] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(350);
  const labels = await page.evaluate(() =>
    [...document.querySelectorAll("#my-list .my-actions button")].map((b) => b.textContent.trim()));
  assert.ok(!labels.some((x) => /Акт/.test(x)), `кнопка акта на новой заявке: ${labels.join("/")}`);
  // …но если документы уже приложены, кнопка остаётся при любом статусе:
  // иначе к ним не вернуться, когда статус поменяли после загрузки.
  await page.close();
});

test("напомнить о просрочке может только автор просроченной заявки", async () => {
  const base = {
    counterparty: "ООО «Ромашка»", amount: "1000.00", currency: "RUB",
    article: "Аренда", urgency: "Обычная", planned_date: "05.07.2026",
    created_at: "2026-07-01 12:00", has_invoice: true,
    payment_source: "invoice", closing_count: 0, status: "Новая",
  };
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [
      { ...base, id: "INV-20260701-120000-0006", overdue: true },
      { ...base, id: "INV-20260701-120000-0007", overdue: false },
    ] },
    "/api/my/nudge": { ok: true, message: "Напомнили: получателей — 2." },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);

  const perRow = await page.evaluate(() =>
    [...document.querySelectorAll("#my-list .my-item")].map((row) =>
      [...row.querySelectorAll(".my-actions button")].map((b) => b.textContent.trim())));
  assert.equal(perRow.length, 2, "заявок в списке не две");
  assert.ok(perRow[0].some((x) => /Напомнить/.test(x)), "у просроченной нет напоминания");
  assert.ok(!perRow[1].some((x) => /Напомнить/.test(x)), "напоминание у непросроченной");

  // Сообщение уходит всем финансистам, поэтому спрашиваем подтверждение.
  await page.evaluate(() => {
    [...document.querySelectorAll("#my-list .my-actions button")]
      .find((b) => /Напомнить/.test(b.textContent)).click();
  });
  await page.waitForTimeout(200);
  assert.equal(
    await page.evaluate(() => window.__posts.filter((p) => p[0].indexOf("/api/my/nudge") !== -1).length),
    0, "запрос ушёл без подтверждения");
  await page.evaluate(() => {
    [...document.querySelectorAll("#modal-actions button")]
      .find((b) => /Напомнить/.test(b.textContent)).click();
  });
  await page.waitForTimeout(400);
  const post = await page.evaluate(() =>
    window.__posts.filter((p) => p[0].indexOf("/api/my/nudge") !== -1).pop());
  assert.ok(post, "запрос не ушёл");
  assert.match(String(post[1]), /INV-20260701-120000-0006/, "напомнили не по той заявке");
  assert.deepEqual(page.errors, []);
  await page.close();
});


test("админское удаление живёт в подробностях заявки", async () => {
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true, admin: true },
    "/api/my-requests": { items: [{
      id: "INV-20260701-120000-0008", status: "Новая", counterparty: "ООО «Ромашка»",
      amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
      planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
      payment_source: "invoice", closing_count: 0, overdue: false,
    }] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);
  // Тап по карточке (не по кнопке) открывает подробности.
  await page.evaluate(() => document.querySelector("#my-list .my-item .my-id").click());
  await page.waitForTimeout(250);
  const labels = await page.evaluate(() =>
    [...document.querySelectorAll("#modal-actions button")].map((b) => b.textContent.trim()));
  assert.ok(labels.some((x) => /Удалить/.test(x)), `в подробностях нет удаления: ${labels}`);
  await page.close();
});

test("ссылка из уведомления открывает панель на нужной заявке", async () => {
  // Финансисту приходит t.me/<бот>/<имя>?startapp=fin_<id>; приложение
  // подставляет номер в поиск панели — своей выборки для этого не заводим.
  const rid = "INV-20260701-120000-0009";
  const page = await openApp(browser, { skin: "neon", hash: "?fin=" + rid, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/finance/access": { ok: true },
    "/api/finance/requests": { items: [], total_found: 0, shown: 0 },
  } });
  await page.waitForTimeout(500);
  const state = await page.evaluate(() => ({
    open: !document.getElementById("fin-view").classList.contains("hidden"),
    query: document.getElementById("fin-query").value,
  }));
  assert.ok(state.open, "панель финансиста не открылась");
  assert.equal(state.query, rid, "номер заявки не подставлен в поиск");
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("ряд панели финансиста тоже в одну строку", async () => {
  // У новой заявки в панели три кнопки статуса, и админское удаление делало
  // четвёртую: на 360px ряд разъезжался ровно так же, как в «Моих заявках».
  const base = {
    counterparty: 'ООО "АТОЛ ОНЛАЙН"', amount: "52205.00", currency: "RUB",
    article: "Инвестиции", urgency: "Срочно", planned_date: "26.08.2026",
    created_at: "2026-08-26 18:08", has_invoice: false, payment_source: "requisites",
    sender: "@t (Т)", work_deadline: "12 месяцев", closing_count: 0, overdue: false,
  };
  for (const status of ["Новая", "Оплачена"]) {
    for (const width of [360, 430]) {
      const page = await openApp(browser, { skin: "neon", width, routes: {
        "/api/access": { allowed: true, pending: false, has_admins: true, admin: true },
        "/api/finance/access": { ok: true },
        "/api/finance/requests": {
          items: [{ ...base, id: "INV-20260826-180840-2541", status }],
          total_found: 1, shown: 1,
        },
      } });
      await page.evaluate(() => document.querySelector("#fin-btn").classList.remove("hidden"));
      await page.click("#fin-btn");
      await page.waitForTimeout(500);
      const r = await page.evaluate(() => {
        const btns = [...document.querySelectorAll("#fin-req-list .my-actions button")];
        return {
          labels: btns.map((b) => b.textContent.trim()),
          rows: new Set(btns.map((b) => Math.round(b.getBoundingClientRect().top))).size,
          widths: btns.map((b) => Math.round(b.getBoundingClientRect().width)),
        };
      });
      const where = `${status}/${width}px`;
      assert.equal(r.rows, 1, `на ${where} ряд разъехался: ${r.widths.join("/")}`);
      assert.ok(!r.labels.some((x) => /Удалить/.test(x)),
        `на ${where} удаление вернулось в ряд: ${r.labels.join("/")}`);
      await page.close();
    }
  }
});

test("разбор start_param из initData, а не только из адреса", async () => {
  // Настоящий клиент кладёт startapp в initData; через ?fin= приложение
  // открывают только web_app-кнопки. Проверяли раньше лишь второй путь.
  const rid = "INV-20260701-120000-0010";
  const initData =
    "query_id=AAA&user=%7B%22id%22%3A1%7D&start_param=fin_" + rid + "&auth_date=1&hash=x";
  const page = await openApp(browser, { skin: "neon", initData, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/finance/access": { ok: true },
    "/api/finance/requests": { items: [], total_found: 0, shown: 0 },
  } });
  await page.waitForTimeout(600);
  const state = await page.evaluate(() => ({
    open: !document.getElementById("fin-view").classList.contains("hidden"),
    query: document.getElementById("fin-query").value,
  }));
  assert.ok(state.open, "панель не открылась по start_param");
  assert.equal(state.query, rid);
  assert.deepEqual(page.errors, []);
  await page.close();
});

test("подробности: значение шире ярлыка и слова не рвутся", async () => {
  // Колонка ярлыков раньше росла по самому длинному («Срок исполнения
  // работ»), и на узком экране название ИП ломалось посреди слова.
  const page = await openApp(browser, { skin: "light", width: 320, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [{
      id: "INV-20260827-164013-2415", status: "Новая",
      counterparty: "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ НИЦЕВИЧ ТАТЬЯНА ВАЛЕРЬЕВНА",
      amount: "123.00", currency: "RUB", article: "Закупка товаров",
      urgency: "Срочно", planned_date: "27.08.2026", created_at: "2026-08-27 16:40",
      has_invoice: false, payment_source: "none", work_deadline: "123",
      sender: "@elementaryyy1997 (Pavel Elipashev)", closing_count: 0, overdue: false,
    }] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);
  await page.evaluate(() => document.querySelector("#my-list .my-item .my-id").click());
  await page.waitForTimeout(300);
  const r = await page.evaluate(() => {
    const dts = [...document.querySelectorAll(".modal-rows dt")];
    const dds = [...document.querySelectorAll(".modal-rows dd")];
    const probe = document.createElement("span");
    probe.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap";
    probe.style.font = getComputedStyle(dds[0]).font;
    document.body.appendChild(probe);
    // Самое длинное СЛОВО значений против ширины колонки: если слово шире,
    // браузеру приходится рвать его посередине. По дефису рвать можно —
    // номер заявки так и переносится, и это читается.
    const col = dds[0].getBoundingClientRect().width;
    let widest = 0, word = "";
    dds.forEach((dd) => dd.textContent.split(/[\s-]+/).forEach((w) => {
      probe.textContent = w;
      const width = probe.getBoundingClientRect().width;
      if (width > widest) { widest = width; word = w; }
    }));
    probe.remove();
    return {
      label: Math.round(dts[0].getBoundingClientRect().width),
      value: Math.round(col),
      widest: Math.ceil(widest), word,
      buttons: [...document.querySelectorAll("#modal-actions button")]
        .map((b) => Math.round(b.getBoundingClientRect().height)),
    };
  });
  assert.ok(r.value > r.label, `значение уже ярлыка: ${r.label} против ${r.value}`);
  assert.ok(r.widest <= r.value,
    `«${r.word}» (${r.widest}px) не влезает в колонку ${r.value}px — порвётся`);
  // Кнопки окна мельче базовых: две штуки по 46px занимали полосу выше шапки.
  assert.ok(r.buttons.every((h) => h <= 40), `кнопки окна крупные: ${r.buttons}`);
  await page.close();
});

test("фильтры панели стоят сеткой, а не зигзагом", async () => {
  const page = await openApp(browser, { skin: "light", width: 360, routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/finance/access": { ok: true },
    "/api/finance/requests": { items: [], total_found: 0, shown: 0 },
  } });
  await page.evaluate(() => document.querySelector("#fin-btn").classList.remove("hidden"));
  await page.click("#fin-btn");
  await page.waitForTimeout(400);
  await page.click("#fin-toggle");
  await page.waitForTimeout(300);
  const r = await page.evaluate(() => {
    const st = [...document.querySelectorAll("#fin-status-seg button")];
    const box = st.map((b) => b.getBoundingClientRect());
    return {
      // «Все» во всю строку, остальные — ровно две равные колонки.
      first: Math.round(box[0].width),
      rest: [...new Set(box.slice(1).map((b) => Math.round(b.width)))],
      columns: [...new Set(box.slice(1).map((b) => Math.round(b.left)))].length,
      clipped: st.filter((b) => b.scrollWidth > b.clientWidth + 1)
        .map((b) => b.textContent.trim()),
      urgency: [...new Set([...document.querySelectorAll("#fin-urgency-seg button")]
        .map((b) => Math.round(b.getBoundingClientRect().width)))],
    };
  });
  assert.equal(r.rest.length, 1, `статусы разной ширины: ${r.rest.join("/")}`);
  assert.equal(r.columns, 2, `колонок не две: ${r.columns}`);
  assert.ok(r.first > r.rest[0], "«Все» не во всю строку");
  assert.deepEqual(r.clipped, [], "надписи фильтров обрезаны");
  assert.equal(r.urgency.length, 1, `срочность разной ширины: ${r.urgency.join("/")}`);
  await page.close();
});

test("кнопка «Акт / УПД» не открывает подробности заодно", async () => {
  // input.click() из обработчика кнопки всплывал до строки уже от input,
  // а строка ловила только closest("button") — поверх выбора файла
  // открывалось окно подробностей.
  const page = await openApp(browser, { skin: "neon", routes: {
    "/api/access": { allowed: true, pending: false, has_admins: true },
    "/api/my-requests": { items: [{
      id: "INV-20260701-120000-0011", status: "Оплачена", counterparty: "ООО «Ромашка»",
      amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
      planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
      payment_source: "invoice", closing_count: 0, overdue: false,
    }] },
  } });
  await page.click("#my-btn");
  await page.waitForTimeout(400);
  await page.evaluate(() => {
    [...document.querySelectorAll("#my-list .my-actions button")]
      .find((b) => /Акт/.test(b.textContent)).click();
  });
  await page.waitForTimeout(300);
  assert.equal(
    await page.evaluate(() => document.getElementById("modal").classList.contains("shown")),
    false, "поверх выбора файла открылись подробности");
  await page.close();
});
