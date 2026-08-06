/*
 * skin-field.js — живое полотно фона Mini App: узлы, связи и бегущие огни.
 *
 * Замкнутый модуль: единственная зависимость — переданный <canvas>, наружу
 * отдаёт объект с toggle()/refresh(). Подключается ПЕРЕД app.js.
 */

/** Живое полотно фона: узлы, связи и бегущие огни — заявки в пути. */
function buildSkinField(canvas) {
  if (!canvas || !canvas.getContext) return null;
  var ctx = canvas.getContext("2d");
  if (!ctx) return null;

  var reduce = typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var W = 0, H = 0, nodes = [], packets = [], raf = null, on = false;
  var LINK = 150;
  // Цвета берём из ЖИВОЙ темы, а не зашиваем: на светлом телеграмном фоне
  // изумрудное свечение выглядело бы грязью, а линии — мусором.
  var ACCENT = [16, 185, 129], COOL = [91, 155, 255], dark = true;

  function rgba(c, a) { return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }

  /** «rgb(36, 129, 204)» → [36, 129, 204]; мусор → null. */
  function parseRgb(value) {
    var m = /rgba?\(([^)]+)\)/.exec(value || "");
    if (!m) return null;
    var parts = m[1].split(",").map(function (n) { return parseFloat(n); });
    return parts.length >= 3 ? [parts[0] | 0, parts[1] | 0, parts[2] | 0] : null;
  }

  /** Читает акцент и светлоту фона через служебный элемент: так значение
   *  приходит уже разрешённым, каким бы ни была цепочка var(). */
  function readTheme() {
    var probe = document.createElement("span");
    probe.style.cssText =
      "position:absolute;left:-9999px;color:var(--accent);background-color:var(--bg2)";
    document.body.appendChild(probe);
    var st = getComputedStyle(probe);
    var accent = parseRgb(st.color);
    var bg = parseRgb(st.backgroundColor);
    document.body.removeChild(probe);

    if (accent) ACCENT = accent;
    if (bg) {
      // Относительная яркость фона: ниже половины — считаем тему тёмной.
      dark = (0.2126 * bg[0] + 0.7152 * bg[1] + 0.0722 * bg[2]) / 255 < .5;
    }
    // Второй, «холодный» цвет — тот же акцент со сдвигом в синеву.
    COOL = dark ? [91, 155, 255]
                : [Math.round(ACCENT[0] * .7), Math.round(ACCENT[1] * .8), 255];
  }

  function resize() {
    var dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth || window.innerWidth;
    H = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    // Плотность меньше, чем на сайте: это форма на телефоне, а не витрина.
    var n = Math.round(Math.min(Math.max((W * H) / 24000, 14), 42));
    nodes = [];
    for (var i = 0; i < n; i++) {
      nodes.push({
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - .5) * .13, vy: (Math.random() - .5) * .13,
        r: Math.random() * 1.3 + .6, hot: Math.random() < .16
      });
    }
    packets = [];
  }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    for (var i = 0; i < nodes.length; i++) {
      var a = nodes[i];
      for (var j = i + 1; j < nodes.length; j++) {
        var b = nodes[j];
        var dx = a.x - b.x, dy = a.y - b.y, d2 = dx * dx + dy * dy;
        if (d2 > LINK * LINK) continue;
        var t = 1 - Math.sqrt(d2) / LINK;
        ctx.strokeStyle = rgba(a.hot || b.hot ? COOL : ACCENT, t * t * (dark ? .16 : .1));
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
    for (var k = 0; k < nodes.length; k++) {
      var n = nodes[k], col = n.hot ? COOL : ACCENT;
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      ctx.fillStyle = rgba(col, dark ? (n.hot ? .9 : .55) : (n.hot ? .5 : .3));
      ctx.shadowColor = rgba(col, .8);
      // На светлом фоне свечение мутит картинку — рисуем чистые точки.
      ctx.shadowBlur = dark ? (n.hot ? 14 : 7) : 0;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
    for (var p = packets.length - 1; p >= 0; p--) {
      var pk = packets[p];
      pk.t += pk.speed;
      if (pk.t >= 1) { packets.splice(p, 1); continue; }
      var e = pk.t < .5 ? 2 * pk.t * pk.t : 1 - Math.pow(-2 * pk.t + 2, 2) / 2;
      var fade = Math.sin(pk.t * Math.PI);
      ctx.beginPath();
      ctx.arc(pk.a.x + (pk.b.x - pk.a.x) * e, pk.a.y + (pk.b.y - pk.a.y) * e, 2.1, 0, Math.PI * 2);
      ctx.fillStyle = rgba(ACCENT, (dark ? .9 : .65) * fade);
      ctx.shadowColor = rgba(ACCENT, .85);
      ctx.shadowBlur = dark ? 18 : 0;
      ctx.fill();
      ctx.shadowBlur = 0;
    }
  }

  function spawn() {
    if (nodes.length < 2 || packets.length > 3) return;
    var a = nodes[(Math.random() * nodes.length) | 0], near = [];
    for (var i = 0; i < nodes.length; i++) {
      var b = nodes[i];
      if (b === a) continue;
      var dx = a.x - b.x, dy = a.y - b.y;
      if (dx * dx + dy * dy < LINK * LINK) near.push(b);
    }
    if (near.length) {
      packets.push({ a: a, b: near[(Math.random() * near.length) | 0],
                     t: 0, speed: .006 + Math.random() * .005 });
    }
  }

  function step() {
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      n.x += n.vx; n.y += n.vy;
      if (n.x < -20) n.x = W + 20; else if (n.x > W + 20) n.x = -20;
      if (n.y < -20) n.y = H + 20; else if (n.y > H + 20) n.y = -20;
    }
    if (Math.random() < .018) spawn();
    draw();
    raf = requestAnimationFrame(step);
  }

  function start() { if (raf === null && on) raf = requestAnimationFrame(step); }
  function stop() { if (raf !== null) { cancelAnimationFrame(raf); raf = null; } }

  var rt = null;
  window.addEventListener("resize", function () {
    if (!on) return;
    clearTimeout(rt);
    rt = setTimeout(function () { resize(); draw(); }, 200);
  });
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop(); else start();
  });

  return {
    toggle: function (enabled) {
      on = enabled;
      if (!enabled) { stop(); return; }
      readTheme();
      resize();
      if (reduce) { draw(); return; }   // один кадр — и хватит
      start();
    },
    // Сменили тему — перечитать цвета и перерисовать.
    refresh: function () {
      if (!on) return;
      readTheme();
      draw();
    }
  };
}
