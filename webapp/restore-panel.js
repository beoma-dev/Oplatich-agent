/* Восстановление данных из загруженного архива.
 *
 * Панель намеренно двухшаговая. Сначала «Посмотреть архив»: сервер
 * разворачивает файл во временный каталог, ничего не трогая, и отвечает,
 * что внутри — дата, сколько заявок, сколько файлов, сколько финансистов.
 * Только увидев это, человек нажимает «Восстановить». Ошибка «не тот файл»
 * ловится глазами лучше, чем любой проверкой на сервере.
 *
 * Второй шаг ещё и переспрашивает: операция пишет поверх живых данных,
 * и единственное, что отделяет её от случайного нажатия, — это вопрос.
 * Возврат при этом всё равно есть: сервер снимает копию текущего
 * состояния до подмены и называет её в ответе.
 *
 * Вынесено в отдельный файл по той же причине, что и alerts-panel.js:
 * app.js уже у своего потолка, и класть туда ещё одну панель значит
 * платить за это чужим порогом.
 */
function buildRestorePanel(ctx) {
  var $ = ctx.$;
  var input = $("restore-file");
  var note = $("restore-what");
  var checkBtn = $("restore-check");
  var applyBtn = $("restore-apply");
  if (!input || !checkBtn || !applyBtn) return null;

  /** Разобранный архив ждёт подтверждения. Сбрасываем при любой смене файла. */
  var ready = false;

  function reset() {
    ready = false;
    applyBtn.classList.add("hidden");
    note.classList.add("hidden");
    note.textContent = "";
  }

  function send(action, btn) {
    var file = input.files && input.files[0];
    if (!file) {
      ctx.showMsg("Сначала выберите файл архива.", true);
      return Promise.resolve(null);
    }
    var body = new FormData();
    body.append("file", file);
    body.append("action", action);
    btn.disabled = true;
    btn.style.opacity = ".6";
    return fetch("/api/admin/restore", {
      method: "POST",
      headers: { "X-Telegram-Init-Data": ctx.initData() },
      body: body
    })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .catch(function () { return { ok: false, d: { detail: "Сеть недоступна." } }; })
      .then(function (res) {
        btn.disabled = false;
        btn.style.opacity = "";
        return res;
      });
  }

  function describe(s) {
    var parts = [
      "Архив от " + s.made_at,
      "заявок: " + s.requests,
      "записей аудита: " + s.audit,
      "файлов: " + s.files
    ];
    if (s.has_settings) {
      parts.push("финансистов: " + s.financiers, "в whitelist: " + s.allowed);
    }
    return parts.join(" · ");
  }

  checkBtn.addEventListener("click", function () {
    reset();
    send("inspect", this).then(function (res) {
      if (!res) return;
      if (!res.ok) {
        ctx.showMsg(res.d.detail || "Архив не принят.", true);
        return;
      }
      ready = true;
      note.textContent = describe(res.d.summary)
        + ". Это заменит текущие данные; копия прежнего состояния сохранится.";
      note.classList.remove("hidden");
      applyBtn.classList.remove("hidden");
      if (ctx.haptic) ctx.haptic();
    });
  });

  applyBtn.addEventListener("click", function () {
    if (!ready) return;
    // Единственная преграда между «нажал» и «переписал боевые данные».
    // Своё окно, а не window.confirm: нативное на десктопе рисуется
    // в ГЛАВНОМ окне Telegram, то есть на другом экране, — вопрос там
    // и остался бы незамеченным, а кнопка выглядела бы «залипшей».
    var btn = this;
    ctx.confirm(
      "💾 Восстановить данные",
      "Текущие настройки, журнал и файлы будут заменены содержимым архива. "
      + "Копия прежнего состояния сохранится на сервере.",
      "Восстановить",
      function () { apply(btn); },
      true
    );
  });

  function apply(btn) {
    send("apply", btn).then(function (res) {
      if (!res) return;
      ctx.showMsg(
        res.ok ? (res.d.message || "Восстановлено.") : (res.d.detail || "Не удалось."),
        !res.ok
      );
      if (res.ok) {
        reset();
        input.value = "";
      }
    });
  }

  // Выбрали другой файл — прежний разбор больше не про него.
  input.addEventListener("change", reset);
  return { reset: reset };
}
