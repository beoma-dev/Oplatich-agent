/* Тесты чистых функций формы: node --test tests/js */
const test = require("node:test");
const assert = require("node:assert/strict");
const lib = require("../../webapp/form-lib.js");

test("parseAmount: валидные форматы, включая европейский", () => {
  assert.equal(lib.parseAmount("125000.50"), 125000.5);
  assert.equal(lib.parseAmount("125 000,50"), 125000.5);
  assert.equal(lib.parseAmount("1.000,50"), 1000.5);
  assert.equal(lib.parseAmount("1,000.50"), 1000.5);
  assert.equal(lib.parseAmount("1.000.000,50"), 1000000.5);
  assert.equal(lib.parseAmount("1.000.000"), 1000000);
  assert.equal(lib.parseAmount("123,45"), 123.45);
});

test("parseAmount: мусор и битая группировка отклоняются", () => {
  for (const bad of ["", "ноль", "0", "-5", "1000000001", "12..5", "1.00,50", "1,00,50", "1000.000,50"]) {
    assert.equal(lib.parseAmount(bad), null, `должно отклоняться: ${bad}`);
  }
});

test("formatAmount: пробелы-тысячи, копейки по необходимости", () => {
  assert.equal(lib.formatAmount(1000000), "1 000 000");
  assert.equal(lib.formatAmount(1234.5), "1 234,50");
});

test("ИНН: контрольные числа 10 и 12 знаков", () => {
  assert.ok(lib.innValid("7707083893"));
  assert.ok(!lib.innValid("7707083892"));
  assert.ok(lib.innValid("500100732259"));
  assert.ok(!lib.innValid("500100732258"));
});

test("БИК и ключевание счетов", () => {
  assert.ok(lib.bikValid("044525225"));
  assert.ok(!lib.bikValid("144525225"));
  assert.ok(lib.corrKeyValid("044525225", "30101810400000000225"));
  assert.ok(!lib.corrKeyValid("044525225", "30101810400000000226"));
});

test("checkRequisites: предупреждения только по делу", () => {
  const clean = "ИНН 7707083893\nБИК 044525225\nк/с 30101810400000000225";
  assert.equal(lib.checkRequisites(clean).length, 0);
  const badInn = lib.checkRequisites("ИНН 7707083892\nБИК 044525225");
  assert.equal(badInn.length, 1);
  assert.match(badInn[0], /^ИНН/);
});

test("nextBusinessISO: пятница и выходные переносятся на понедельник", () => {
  assert.equal(lib.nextBusinessISO(new Date(2026, 7, 7)), "2026-08-10");  // пт → пн
  assert.equal(lib.nextBusinessISO(new Date(2026, 7, 8)), "2026-08-10");  // сб → пн
  assert.equal(lib.nextBusinessISO(new Date(2026, 7, 9)), "2026-08-10");  // вс → пн
  assert.equal(lib.nextBusinessISO(new Date(2026, 7, 3)), "2026-08-04");  // пн → вт
});

test("fmtRu", () => {
  assert.equal(lib.fmtRu("2026-08-04"), "04.08.2026");
});

test("brokenReason: отказ только там, где ошибиться нельзя", () => {
  const { brokenReason } = lib;
  assert.equal(brokenReason("ООО «Ромашка»"), null);
  assert.equal(brokenReason(""), null, "пустое — это «не заполнено», не брак");
  assert.match(brokenReason("н".repeat(22)), /повторяется/);
  assert.match(brokenReason("12321432132"), /нет ни одной буквы/);
  assert.equal(brokenReason("."), "слишком короткое значение");
  // Кириллица — буквы: в JS класс \W работает по ASCII и когда-то их не считал.
  assert.equal(brokenReason("Аренда"), null);
  // Срок работ законно пишут датой.
  assert.match(brokenReason("15.12.2026"), /нет ни одной буквы/);
  assert.equal(brokenReason("15.12.2026", false), null);
});

test("plural: склонение после числа", () => {
  const { plural } = lib;
  const f = (n) => plural(n, "поле", "поля", "полей");
  assert.equal(f(1), "поле");
  assert.equal(f(2), "поля");
  assert.equal(f(5), "полей");
  assert.equal(f(11), "полей");
  assert.equal(f(21), "поле");
  assert.equal(f(22), "поля");
});

test("initData читается из хеша, когда SDK Telegram не загрузился", () => {
  // Авторизация формы висела на внешнем скрипте telegram.org: не загрузился —
  // человек заполнял всю форму и узнавал об этом на кнопке «Отправить».
  // Telegram кладёт данные в хеш сам, SDK их только разбирает.
  const hash =
    "#tgWebAppData=query_id%3DAAE%26user%3D%257B%2522id%2522%253A42%257D" +
    "%26auth_date%3D1787000000%26hash%3Ddeadbeef&tgWebAppVersion=7.0";
  const got = lib.initDataFromHash(hash);
  assert.match(got, /^query_id=AAE&user=/);
  assert.match(got, /hash=deadbeef$/);
});

test("нет хеша или он битый — пустая строка, а не исключение", () => {
  assert.equal(lib.initDataFromHash(""), "");
  assert.equal(lib.initDataFromHash("#tgWebAppVersion=7.0"), "");
  assert.equal(lib.initDataFromHash("#tgWebAppData=%E0%A4%A"), "");
});

test("чат возврата берётся из initData, иначе из query", () => {
  // Кнопка в группе открывает форму ссылкой ?startapp=<chat_id>, и Telegram
  // отдаёт его же в query как tgWebAppStartParam.
  assert.equal(lib.startParamFrom("a=1&start_param=-100777&b=2", ""), "-100777");
  assert.equal(lib.startParamFrom("", "?tgWebAppStartParam=-1004467808639"), "-1004467808639");
  assert.equal(lib.startParamFrom("", ""), "");
});
