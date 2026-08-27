/* Живая инструкция: пять шагов с анимацией поверх текстового экрана.
 *
 * Зачем. Текстовая инструкция верна и подробна, но её не читают: человек
 * открывает форму, чтобы оплатить счёт, а не изучать документацию. Пять
 * коротких сценок показывают путь заявки целиком за полминуты, и после них
 * тот же текст ниже читается уже как справочник, а не как первое знакомство.
 *
 * Формат «сторис» выбран не ради моды: он знаком любому, кто пользуется
 * Telegram, — полоски сверху, тап вперёд, удержание паузит. Учить нечему.
 *
 * Устройство. Сцена — это статическая разметка (строки ниже, БЕЗ данных
 * пользователя, поэтому innerHTML безопасен) плюс класс `fx` на элементах,
 * которые должны появиться, и `--d` — задержка каждого. Всё движение делает
 * CSS (tour.css); JS только переключает шаг и слушает конец полоски.
 *
 * Автопереход завязан на animationend полоски прогресса, а не на таймер:
 * пауза тогда паузит и полоску, и переход одним и тем же способом, и они
 * не могут разъехаться.
 *
 * Живёт отдельным файлом и подключается сам, наблюдая за #help-view:
 * app.js у своего потолка, и трогать его ради этого не пришлось.
 */
var TOUR_STEPS = [
  {
    title: "Счёт или реквизиты",
    text: "Приложите счёт. Нет счёта — впишите реквизиты. Нет ни того ни другого — "
      + "тоже заявка: «оплатим по договору, документы позже».",
    html: '<div class="t-col">'
      + '<div class="t-chip fx" style="--d:.1s">📎 счёт.pdf<i class="t-ok">✓</i></div>'
      + '<div class="t-or fx" style="--d:.9s">или</div>'
      + '<div class="t-chip fx" style="--d:1.1s">✍️ ИНН 7707083893, р/с 40702…<i class="t-ok">✓</i></div>'
      + '<div class="t-or fx" style="--d:1.9s">или</div>'
      + '<div class="t-chip t-warn fx" style="--d:2.1s">⚠️ ни счёта, ни реквизитов</div>'
      + "</div>"
  },
  {
    title: "Заполните форму",
    text: "Сумма понимает пробелы и запятую. Контрагент подставляется из прошлых "
      + "заявок вместе с реквизитами. Дата считается сама.",
    html: '<div class="t-form">'
      + '<div class="t-row fx" style="--d:.1s"><span>Сумма</span>'
      + '<b class="t-type" style="--d:.5s">125 000,50 ₽</b></div>'
      + '<div class="t-row fx" style="--d:.9s"><span>Контрагент</span>'
      + '<b class="t-hint fx" style="--d:1.5s">ООО «Ромашка» ✓</b></div>'
      + '<div class="t-row fx" style="--d:2.1s"><span>Срочность</span>'
      + '<b><i class="t-pill">🟢 обычная</i><i class="t-pill t-on fx" style="--d:2.7s">🔴 срочно</i></b></div>'
      + "</div>"
  },
  {
    title: "Отправили",
    text: "Заявка попадает в реестр, вам приходит подтверждение с номером и "
      + "PDF-документом. В общий чат уходит краткий итог — без файла и реквизитов.",
    html: '<div class="t-col">'
      + '<div class="t-btn fx t-press" style="--d:.2s">Отправить заявку</div>'
      + '<div class="t-card fx" style="--d:1.4s"><b>✅ Заявка принята</b>'
      + '<span>INV-20260827-164013-2415 · 125 000,50 ₽</span></div>'
      + '<div class="t-reg fx" style="--d:2.2s">📊 строка 42 в реестре</div>'
      + "</div>"
  },
  {
    title: "Финансист видит её сразу",
    text: "Ему приходит карточка с файлом счёта и кнопкой «Открыть в приложении». "
      + "Статус он ставит в панели — вы узнаете о нём сразу.",
    html: '<div class="t-col">'
      + '<div class="t-bubble fx" style="--d:.1s"><b>🧾 ООО «Ромашка»</b>'
      + '<span>125 000,50 ₽ · Аренда</span>'
      + '<i class="t-open">🔎 Открыть в приложении</i></div>'
      + '<div class="t-swap fx" style="--d:1.6s">'
      + '<i class="t-st t-st-old">⏳ Новая</i><i class="t-st t-st-new">✅ Оплачена</i></div>'
      + "</div>"
  },
  {
    title: "Дальше — «Мои заявки»",
    text: "Просрочили — поторопите одной кнопкой. Оплатили — приложите акт: "
      + "он попадёт в ту же строку реестра, хоть через месяц.",
    html: '<div class="t-col">'
      + '<div class="t-item fx" style="--d:.1s"><b>⏳ Новая · 125 000,50 ₽</b>'
      + '<span class="t-late">⚠️ просрочено · 📅 до 26.08.2026</span>'
      + '<i class="t-act t-act-1">⏰ Напомнить</i></div>'
      + '<div class="t-item fx" style="--d:1.6s"><b>✅ Оплачена · 18 400 ₽</b>'
      + '<span>📅 до 24.08.2026</span>'
      + '<i class="t-act t-act-2">📄 Акт / УПД</i></div>'
      + "</div>"
  }
];

function helpTour(enabled) {
  var view = document.getElementById("help-view");
  if (!enabled || !view || document.getElementById("tour")) return;
  // «Уменьшить движение» — показываем обычный текст, он никуда не делся.
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var box = document.createElement("div");
  box.className = "tour";
  box.id = "tour";
  var bars = document.createElement("div");
  bars.className = "tour-bars";
  TOUR_STEPS.forEach(function () { bars.appendChild(document.createElement("i")); });
  var stage = document.createElement("div");
  stage.className = "tour-stage";
  var cap = document.createElement("div");
  cap.className = "tour-cap";
  var capTitle = document.createElement("b");
  var capText = document.createElement("span");
  cap.appendChild(capTitle);
  cap.appendChild(capText);
  var foot = document.createElement("div");
  foot.className = "tour-foot";
  var prev = mkBtn("←", "Назад");
  var skip = mkBtn("Пропустить", "Пропустить инструкцию");
  var next = mkBtn("→", "Дальше");
  skip.className = "tour-skip";
  foot.appendChild(prev);
  foot.appendChild(skip);
  foot.appendChild(next);
  box.appendChild(bars);
  box.appendChild(stage);
  box.appendChild(cap);
  box.appendChild(foot);
  view.insertBefore(box, view.children[1] || null);

  function mkBtn(label, title) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "tour-nav";
    b.textContent = label;
    b.title = title;
    return b;
  }

  var at = -1;

  function show(i) {
    if (i < 0 || i >= TOUR_STEPS.length) return;
    at = i;
    var step = TOUR_STEPS[i];
    // Разметка сцен — свои строки из этого файла, данных пользователя в них
    // нет; иначе здесь был бы textContent.
    stage.innerHTML = step.html;
    capTitle.textContent = step.title;
    capText.textContent = step.text;
    // Перезапуск проявления сцены и подписи: без этого смена шага была
    // подменой содержимого в один кадр и читалась рывком. Чтение offsetWidth
    // — та самая принудительная перерисовка, ради которой оно и написано.
    stage.className = "tour-stage";
    cap.style.animation = "none";
    void stage.offsetWidth;
    stage.className = "tour-stage s" + (i + 1);
    cap.style.animation = "";
    [].forEach.call(bars.children, function (bar, n) {
      bar.className = n < i ? "done" : (n === i ? "run" : "");
    });
    prev.disabled = i === 0;
    next.textContent = i === TOUR_STEPS.length - 1 ? "✓" : "→";
  }

  // Полоска досчитала — следующий шаг. Последний просто останавливается:
  // подсовывать «начать сначала» тому, кто уже досмотрел, незачем.
  bars.addEventListener("animationend", function (ev) {
    if (ev.target.className === "run" && at < TOUR_STEPS.length - 1) show(at + 1);
    else if (ev.target.className === "run") ev.target.className = "done";
  });

  prev.addEventListener("click", function () { show(at - 1); });
  next.addEventListener("click", function () {
    if (at < TOUR_STEPS.length - 1) show(at + 1);
    else box.classList.add("hidden");
  });
  skip.addEventListener("click", function () { box.classList.add("hidden"); });

  // Удержание паузит — как в сторис. pointer* покрывает и мышь, и палец.
  function pause(on) { box.classList.toggle("paused", on); }
  stage.addEventListener("pointerdown", function () { pause(true); });
  ["pointerup", "pointercancel", "pointerleave"].forEach(function (e) {
    stage.addEventListener(e, function () { pause(false); });
  });

  // Экран показали — начинаем с первого шага. Наблюдаем сами, чтобы не
  // трогать app.js: он у своего потолка по числу строк.
  new MutationObserver(function () {
    if (!view.classList.contains("hidden") && at < 0) show(0);
  }).observe(view, { attributes: true, attributeFilter: ["class"] });
}
