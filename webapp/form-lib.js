/*
 * form-lib.js — чистые функции формы invoice-bot (без DOM).
 *
 * Вынесены отдельно, чтобы гонять их в CI (node --test tests/js) и
 * держать в синхроне с серверными валидаторами (bot/validators.py,
 * bot/scheduling.py). Подключается в index.html ПЕРЕД основным скриптом.
 */

/* ---------- Сумма (зеркало bot/validators.py::parse_amount) ---------- */

function parseAmount(raw) {
  // Последний разделитель — десятичный; тысячи — только корректные группы по 3.
  var cleaned = String(raw).replace(/[^\d,.\-]/g, "");
  var hasDot = cleaned.indexOf(".") !== -1, hasComma = cleaned.indexOf(",") !== -1;
  function isThousands(str, sep) {
    return new RegExp("^\\d{1,3}(\\" + sep + "\\d{3})+$").test(str);
  }
  if (hasDot && hasComma) {
    var dec = cleaned.lastIndexOf(",") > cleaned.lastIndexOf(".") ? "," : ".";
    var thou = dec === "," ? "." : ",";
    var idx = cleaned.lastIndexOf(dec);
    var intPart = cleaned.slice(0, idx), frac = cleaned.slice(idx + 1);
    if (!isThousands(intPart, thou)) return null;
    cleaned = intPart.split(thou).join("") + "." + frac;
  } else if (hasComma) {
    var parts = cleaned.split(",");
    if (parts.length === 2) cleaned = parts[0] + "." + parts[1];
    else if (isThousands(cleaned, ",")) cleaned = parts.join("");
    else return null;
  } else if (cleaned.split(".").length > 2) {
    if (!isThousands(cleaned, ".")) return null;
    cleaned = cleaned.split(".").join("");
  }
  if (!cleaned) return null;
  var num = Number(cleaned);
  if (!isFinite(num) || num <= 0 || num > 1e9) return null;
  return num;
}

function formatAmount(num) {
  var parts = num.toFixed(2).split(".");
  parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return parts[0] + (parts[1] === "00" ? "" : "," + parts[1]);
}

/* ---------- Реквизиты: контрольные суммы (алгоритмы ЦБ РФ) ---------- */

function innValid(inn) {
  if (!/^\d{10}$|^\d{12}$/.test(inn)) return false;
  var d = inn.split("").map(Number);
  function ctrl(weights) {
    var s = 0;
    for (var i = 0; i < weights.length; i++) s += weights[i] * d[i];
    return (s % 11) % 10;
  }
  if (inn.length === 10) return ctrl([2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[9];
  return ctrl([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[10] &&
         ctrl([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === d[11];
}

function bikValid(bik) { return /^04\d{7}$/.test(bik); }

function keyCheck(str) {
  // Сумма (цифра × вес) % 10 по весам 7,1,3; итог кратен 10.
  var w = [7, 1, 3], s = 0;
  for (var i = 0; i < str.length; i++) s += ((str.charCodeAt(i) - 48) * w[i % 3]) % 10;
  return s % 10 === 0;
}

function accountKeyValid(bik, acc) { return keyCheck(bik.slice(-3) + acc); }
function corrKeyValid(bik, acc) { return keyCheck("0" + bik.substr(4, 2) + acc); }

function checkRequisites(text) {
  var warns = [];
  var inn = (text.match(/ИНН\D{0,10}(\d{12}|\d{10})/i) || [])[1];
  if (inn && !innValid(inn)) warns.push("ИНН " + inn + " не проходит проверку контрольного числа.");
  var bik = (text.match(/БИК\D{0,10}(\d{9})/i) || [])[1];
  if (bik && !bikValid(bik)) warns.push("БИК " + bik + " выглядит некорректно (9 цифр, начинается с 04).");
  var rs = (text.match(/(?:р\/?с|расч[её]тн\S*\s+сч[её]т\S*)\D{0,10}(\d{20})/i) || [])[1];
  if (bik && bikValid(bik) && rs && !accountKeyValid(bik, rs)) {
    warns.push("Расчётный счёт не сходится с БИК (контрольное число) — проверьте цифры.");
  }
  var ks = (text.match(/(?:к\/?с|корр\S*\s+сч[её]т\S*)\D{0,10}(\d{20})/i) || [])[1];
  if (bik && bikValid(bik) && ks && !corrKeyValid(bik, ks)) {
    warns.push("Корр. счёт не сходится с БИК (контрольное число) — проверьте цифры.");
  }
  return warns;
}

/* ---------- Даты (предпросмотр; авторитетный расчёт — на сервере) ---------- */

function isoOf(d) {
  return d.getFullYear() + "-" +
    String(d.getMonth() + 1).padStart(2, "0") + "-" +
    String(d.getDate()).padStart(2, "0");
}

function todayISO() { return isoOf(new Date()); }

function nextBusinessISO(from) {
  // Следующий рабочий день: пятница и выходные переносятся на понедельник.
  var d = from ? new Date(from) : new Date();
  d.setDate(d.getDate() + 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() + 1);
  return isoOf(d);
}

function fmtRu(iso) {
  var p = (iso || "").split("-");
  return p.length === 3 ? p[2] + "." + p[1] + "." + p[0] : "";
}

/* Экспорт для node --test; в браузере функции просто глобальные. */
/* ---------- Осмысленность текста (зеркало bot/validators.py) ---------- */

/** Жёсткие правила, которые НЕ МОГУТ ошибиться. Причина отказа или null.
 *  requireLetter=false — для срока работ: его законно пишут датой. */
function brokenReason(value, requireLetter) {
  var text = String(value == null ? "" : value).trim();
  if (!text) return null;                       // пустое — это «не заполнено»
  if (text.length < 2) return "слишком короткое значение";
  // \p{L}, а не [^\W\d_]: в JS класс \W работает по ASCII, и кириллица
  // считалась бы «не буквой» — «Аренда» получала бы отказ.
  if (requireLetter !== false && !/\p{L}/u.test(text)) return "нет ни одной буквы";
  if (/(.)\1{5,}/u.test(text)) return "один символ повторяется шесть раз подряд";
  return null;
}

/** Русское склонение после числа: 2 поля, 5 полей, 21 поле. */
function plural(n, one, few, many) {
  var mod10 = n % 10, mod100 = n % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}


/** «rgb(16, 185, 129)» → «#10b981»: setParams родной кнопки принимает hex. */
function cssHex(value) {
  var m = /^rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(value || "");
  if (!m) return null;
  return "#" + [m[1], m[2], m[3]].map(function (n) {
    return ("0" + (+n).toString(16)).slice(-2);
  }).join("");
}

/** Сумма заявки для показа: «125 000,50 RUB». Непарсящееся отдаём как есть. */
function amountText(item) {
  var parsed = parseAmount(item.amount);
  return (parsed === null ? item.amount : formatAmount(parsed)) +
    " " + (item.currency || "");
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    parseAmount: parseAmount,
    formatAmount: formatAmount,
    innValid: innValid,
    bikValid: bikValid,
    keyCheck: keyCheck,
    accountKeyValid: accountKeyValid,
    corrKeyValid: corrKeyValid,
    checkRequisites: checkRequisites,
    isoOf: isoOf,
    todayISO: todayISO,
    nextBusinessISO: nextBusinessISO,
    fmtRu: fmtRu,
    brokenReason: brokenReason,
    plural: plural,
    cssHex: cssHex,
    amountText: amountText
  };
}
