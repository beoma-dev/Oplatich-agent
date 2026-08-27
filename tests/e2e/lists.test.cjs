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
      .find((x) => /Закрывающие/.test(x.textContent));
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
      .find((x) => /Закрывающие/.test(x.textContent));
    return b ? b.textContent.trim() : null;
  });
  assert.match(label || "", /\(2\)/, "счётчик на кнопке не показан");
  await page.close();
});

test("ряд кнопок заявки помещается и не обрезается", async () => {
  // «Повторить» убрана, «Закрывающие документы» добавлены — надпись длинная,
  // и с прежними размерами кнопок ряд разъезжался.
  const item = {
    id: "INV-20260701-120000-0003", status: "Новая", counterparty: "ООО «Ромашка»",
    amount: "1000.00", currency: "RUB", article: "Аренда", urgency: "Обычная",
    planned_date: "05.07.2026", created_at: "2026-07-01 12:00", has_invoice: true,
    payment_source: "invoice", closing_count: 0, overdue: false,
  };
  for (const width of [360, 390, 430]) {
    const page = await openApp(browser, { skin: "neon", width, routes: {
      "/api/access": { allowed: true, pending: false, has_admins: true },
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
        multiline: btns.filter((b) => b.getBoundingClientRect().height > 34)
          .map((b) => b.textContent.trim()),
      };
    });
    assert.deepEqual(r.clipped, [], `на ${width}px надписи обрезаны`);
    assert.ok(r.labels.some((x) => /Закрывающие/.test(x)),
      `на ${width}px нет кнопки закрывающих`);
    // Кнопки по ширине надписи: раньше они делились на равные доли, длинный
    // текст переносился внутри кнопки, а одинокая кнопка на второй строке
    // растягивалась на всю ширину — 294px при экране в 360.
    assert.ok(r.widths.every((x) => x < width * 0.6),
      `на ${width}px кнопка растянулась: ${r.widths.join("/")}`);
    assert.deepEqual(r.multiline, [], `на ${width}px надпись перенеслась внутри кнопки`);
    assert.ok(!r.labels.some((x) => /Повторить/.test(x)),
      `на ${width}px «Повторить» вернулась`);
    await page.close();
  }
});
