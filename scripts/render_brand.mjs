#!/usr/bin/env node
/**
 * Сборка фирменных знаков из webapp/logo.svg.
 *
 * Зачем скрипт, а не готовые файлы: аватарка — это та же марка на подложке.
 * Первая версия набора копировала геометрию персонажа в каждый файл и отстала
 * от logo.svg в тот же день, когда козырёк счетовода заменили кепкой. Здесь
 * геометрия НЕ дублируется: скрипт берёт содержимое logo.svg как есть и
 * оборачивает в рамку, поэтому любая правка марки доезжает до всех знаков.
 *
 * Растеризатор — Chromium из Playwright, он уже нужен для `npm run test:e2e`.
 * Запуск: npm run brand   (после npm ci && npx playwright install chromium)
 *
 * Что получается в assets/brand/ (и SVG, и PNG — всё генерируется):
 *   avatar-512        фото бота в Telegram (BotFather → /setuserpic)
 *   mark-512          знак на прозрачном фоне, на любую подложку
 *   mark-neutral-512  он же без тона: ч/б печать, чужой бланк, золотистый фон
 *   cover-640x360     обложка Mini App (BotFather → /newapp), его жёсткий размер
 *   cover-540x360     она же под ручной запрос 540×360
 */
import { chromium } from 'playwright';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BRAND = join(ROOT, 'assets', 'brand');
const SIZE = 512;

/* Telegram обрезает фото по вписанной окружности (r = SIZE/2). SAFE — радиус,
   внутри которого обязано лежать всё значимое; запас против кромки обода и
   расхождений между клиентами: обод занимает 244…256, значимому нужен воздух
   от него. Масштаб НЕ константа: его считает fitScale по фактическому замеру,
   иначе он врёт при первой же правке марки — так и вышло, когда у logo.svg
   поменялся viewBox: центр съехал, угол счёта вылез за круг, а число осталось
   прежним. */
const SAFE = 238;

/* Тон убираем, светлоту сохраняем: персонаж узнаётся именно контрастом
   шерсть/контур/белок глаза. Сведение в ОДИН цвет для него не работает —
   силуэт с белыми глазами читается как панда, контурный — как медведь
   в очках; проверено, потому одноцветной версии в наборе нет. */
const NEUTRAL = {
  '#F8DEA9': '#F0EEEA', '#EABF74': '#D9D5CD', '#D09C4E': '#B5AFA4',
  '#8A5F27': '#55514A', '#FCEFD3': '#F2F0EB', '#E9A0A6': '#C9C4BB',
  '#FF7BA6': '#A7A7A2', '#E04D80': '#7F7F7A', '#FF8FB4': '#B5B5B0',
  '#EC6493': '#929290', '#B83466': '#616160', '#FFC7DC': '#DADAD6',
  '#FFD3E3': '#E2E2DE', '#0B3B2C': '#3A3833',
  '#2A2018': '#2B2926', '#7A4A2C': '#4A463F', '#EDF2EF': '#EFEEEB',
  '#BCC8C2': '#C6C2B9',
};

const HEADER = (what) => `<!-- СГЕНЕРИРОВАНО scripts/render_brand.mjs — руками не править.
     Источник геометрии: webapp/logo.svg. ${what} -->`;

function readMark() {
  const src = readFileSync(join(ROOT, 'webapp', 'logo.svg'), 'utf8');
  const vb = src.match(/viewBox="([^"]+)"/);
  if (!vb) throw new Error('в webapp/logo.svg нет viewBox — не могу вычислить центр');
  const [x, y, w, h] = vb[1].trim().split(/\s+/).map(Number);
  const inner = src.replace(/^[\s\S]*?<svg[^>]*>/, '').replace(/<\/svg>\s*$/, '');
  return { inner, cx: x + w / 2, cy: y + h / 2 };
}

/* Персонаж кладётся в центр холста: центр viewBox марки → центр квадрата. */
const place = (inner, cx, cy, scale) =>
  `<g transform="translate(${SIZE / 2},${SIZE / 2}) scale(${scale}) translate(${-cx},${-cy})">${inner}</g>`;

function svgAvatar({ inner, cx, cy }, scale) {
  /* Фон почти БЕЛЫЙ с розовым подмесом. Аватарка живёт рядом с чужими цветами —
     светлой и тёмной темами Telegram, акцентом темы пользователя (синий,
     фиолетовый, красный), чужими плашками, — и насыщенный круг с ними спорит.
     Работает по той же причине, что раньше работал графит: фон почти
     ахроматичен, спорить нечем. Розовый подмес держит тон кепки, но остаётся
     настолько светлым, что кепка от фона отделяется.
     Обод — не украшение, а единственный край: на светлой теме и белых плашках
     светлый круг без него растворяется в подложке. Цвет обода повторяет кепку:
     с изумрудным ободом розовая кепка заметно спорит, проверено сравнением.
     Плата за светлый фон — контраст на 40 px в списке чатов ниже, чем у
     тёмного. Тёмный вариант: стопы #2E3A36 → #1D2523 → #121817, обод
     #FF8FB4 (.38), тень #000000 (.38) и halo #EFFFF8 (.16). */
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}" role="img" aria-label="Оплатыч">
${HEADER('Фото бота в Telegram: квадрат и НЕПРОЗРАЧНЫЙ фон обязательны — Telegram жмёт фото в JPEG и заливает прозрачное чёрным.')}
  <defs>
    <radialGradient id="bg" cx="28%" cy="20%" r="98%">
      <stop offset="0%" stop-color="#FFFBFC"/>
      <stop offset="60%" stop-color="#FCF0F5"/>
      <stop offset="100%" stop-color="#F4DFE9"/>
    </radialGradient>
    <filter id="soft" x="-40%" y="-60%" width="180%" height="220%">
      <feGaussianBlur stdDeviation="16"/>
    </filter>
  </defs>
  <rect width="${SIZE}" height="${SIZE}" fill="url(#bg)"/>
  <ellipse cx="256" cy="452" rx="150" ry="30" fill="#B8608A" opacity=".24" filter="url(#soft)"/>
  <circle cx="256" cy="256" r="250" fill="none" stroke="#D63F73" stroke-width="12" opacity=".52"/>
  ${place(inner, cx, cy, scale)}
</svg>`;
}

function svgMark({ inner, cx, cy }, scale, neutral) {
  let body = place(inner, cx, cy, scale);
  if (neutral) {
    for (const [from, to] of Object.entries(NEUTRAL)) body = body.replaceAll(from, to);
    for (const hex of new Set(body.match(/#[0-9A-Fa-f]{6}/g) ?? [])) {
      const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
      if (Math.max(r, g, b) - Math.min(r, g, b) > 24)
        console.warn(`⚠ ${hex} остался цветным — допишите его в NEUTRAL`);
    }
  }
  const what = neutral
    ? 'Знак без тона: там, где цветной спорит с окружением — ч/б печать, чужой бланк, золотисто-коричневая подложка.'
    : 'Знак на прозрачном фоне: кладётся на любую подложку. Безопасное поле то же, что у аватарки, — годится и под круглую обрезку.';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${SIZE} ${SIZE}" width="${SIZE}" height="${SIZE}" role="img" aria-label="Оплатыч">
${HEADER(what)}
  ${body}
</svg>`;
}

async function shot(page, svg, transparent) {
  await page.setContent(
    '<style>html,body{margin:0;padding:0;overflow:hidden}svg{display:block}</style>' + svg,
    { waitUntil: 'load' },
  );
  return page.screenshot({ omitBackground: transparent });
}

/* Замер вместо догадки: рисуем марку с запасом по полотну и ищем самый далёкий
   непрозрачный пиксель от центра — это и есть радиус, который надо вписать.
   Сравнивать PNG «с обрезкой и без» нельзя: Chromium растеризует группу с
   clip-path отдельным слоем, и байты расходятся даже там, где обрезать нечего.
   Замер идёт по alpha, поэтому учитывает и обводки, и блики, и текст. */
async function scanMark(page, mark) {
  const pad = SIZE * 2;
  const r = await page.evaluate(async ([svgInner, ox, oy, side]) => {
    const src = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${side} ${side}"`
      + ` width="${side}" height="${side}"><g transform="translate(${side / 2},${side / 2})`
      + ` translate(${-ox},${-oy})">${svgInner}</g></svg>`;
    const img = new Image();
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(src);
    await img.decode();
    const cv = document.createElement('canvas');
    cv.width = cv.height = side;
    const ctx = cv.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const { data } = ctx.getImageData(0, 0, side, side);
    const c = side / 2;
    let max = 0, painted = 0;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (let y = 0; y < side; y += 1) {
      for (let x = 0; x < side; x += 1) {
        if (data[(y * side + x) * 4 + 3] > 8) {
          painted += 1;
          const d = Math.hypot(x + 0.5 - c, y + 0.5 - c);
          if (d > max) max = d;
          if (x < minX) minX = x;
          if (x > maxX) maxX = x;
          if (y < minY) minY = y;
          if (y > maxY) maxY = y;
        }
      }
    }
    return { max, painted, minX, minY, maxX, maxY };
  }, [mark.inner, mark.cx, mark.cy, pad]);
  if (!r.painted) throw new Error('марка отрисовалась пустой — проверьте webapp/logo.svg');
  /* Тем же проходом берём и габаритную рамку: она нужна ландшафтной обложке.
     Вписывать знак в неё по радиусу нельзя — знак несимметричен (счёт в правой
     лапе), и по радиусу треть ширины уходит в пустоту, а центр съезжает влево.
     Пиксели полотна → единицы марки: рисовали со сдвигом центра viewBox в
     центр полотна, поэтому обратный сдвиг — тот же. */
  const units = (v, o) => v - pad / 2 + o;
  return {
    radius: r.max,
    box: {
      minX: units(r.minX, mark.cx), maxX: units(r.maxX + 1, mark.cx),
      minY: units(r.minY, mark.cy), maxY: units(r.maxY + 1, mark.cy),
    },
  };
}

/* Обложка Mini App. BotFather → /newapp просит РОВНО 640×360 (DEPLOY.md), это
   его жёсткая проверка, а не рекомендация. 540×360 собирается рядом, потому
   что её запросили под ручную регистрацию: если BotFather откажет по размеру —
   берите 640×360, файл тот же по раскладке.
   Раскладка описана один раз в базовых COVER_W×COVER_H и вписывается в холст
   через один scale, поэтому у всех размеров обложка — один и тот же знак, а не
   набор похожих. Фон здесь НАСЫЩЕННО розовый — в отличие от аватарки: обложка
   лежит внутри карточки приложения на своём поле, спорить ей не с чем, а
   светлая кепка и золотая шерсть от малины отделяются лучше, чем от пудры. */
const COVERS = [{ w: 640, h: 360 }, { w: 540, h: 360 }];
const COVER_W = 640;
const COVER_H = 360;
const FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'DejaVu Sans', sans-serif";

/* Кегль подбирается замером, а не константой: у 540×360 текстовое поле на
   16% уже, и подобранный на глаз кегль вылезал за правый край. */
async function fitSize(page, text, weight, maxWidth, maxSize, spacing) {
  return page.evaluate(async ([txt, w, limit, max, sp, stack]) => {
    await document.fonts.ready;
    const ctx = document.createElement('canvas').getContext('2d');
    for (let size = max; size > 8; size -= 0.5) {
      ctx.font = `${w} ${size}px ${stack}`;
      if (ctx.measureText(txt).width + sp * Math.max(txt.length - 1, 0) <= limit * 0.98) return size;
    }
    return 8;
  }, [text, weight, maxWidth, maxSize, spacing, FONT]);
}

async function svgCover(page, { inner }, box, w, h) {
  const pad = 36;
  const markH = 252;
  const mScale = markH / (box.maxY - box.minY);
  const mW = (box.maxX - box.minX) * mScale;
  const mCx = pad + mW / 2;
  const textX = pad + mW + 26;
  const textW = COVER_W - pad - textX;

  const titleSize = await fitSize(page, 'Оплатыч', 700, textW, 76, 1);
  const subSize = await fitSize(page, 'заявки на оплату счетов', 500, textW, 23, 0.2);
  const blockH = titleSize * 0.72 + 16 + subSize;
  const titleY = (COVER_H - blockH) / 2 + titleSize * 0.72;
  const subY = titleY + 16 + subSize * 0.78;

  /* Знак центрируется по ФАКТИЧЕСКОЙ рамке (box), а не по центру viewBox:
     хомяк держит счёт справа, из-за чего геометрия несимметрична и по центру
     viewBox знак съезжал к левому краю. */
  const bCx = (box.minX + box.maxX) / 2;
  const bCy = (box.minY + box.maxY) / 2;
  const s = Math.min(w / COVER_W, h / COVER_H);

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Оплатыч — заявки на оплату счетов">
${HEADER(`Обложка Mini App ${w}×${h} (BotFather → /newapp). Фон НЕПРОЗРАЧНЫЙ: Telegram жмёт картинку и заливает прозрачное чёрным.`)}
  <defs>
    <linearGradient id="cover-bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#FFA6C6"/>
      <stop offset="52%" stop-color="#EC6493"/>
      <stop offset="100%" stop-color="#BE3466"/>
    </linearGradient>
    <radialGradient id="cover-glow" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#FFFFFF" stop-opacity=".30"/>
      <stop offset="100%" stop-color="#FFFFFF" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="cover-halo" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#A82A5A" stop-opacity=".26"/>
      <stop offset="100%" stop-color="#A82A5A" stop-opacity="0"/>
    </radialGradient>
    <filter id="cover-soft" x="-60%" y="-80%" width="220%" height="260%">
      <feGaussianBlur stdDeviation="14"/>
    </filter>
  </defs>
  <rect width="${w}" height="${h}" fill="url(#cover-bg)"/>
  <ellipse cx="${w * 0.24}" cy="${h * 0.12}" rx="${w * 0.5}" ry="${h * 0.5}" fill="url(#cover-glow)"/>
  <rect x="12" y="12" width="${w - 24}" height="${h - 24}" rx="26" fill="none" stroke="#FFFFFF" stroke-opacity=".22" stroke-width="2"/>
  <g transform="translate(${(w - COVER_W * s) / 2},${(h - COVER_H * s) / 2}) scale(${s})">
    <!-- Подложка под знаком ТЕМНЕЕ фона, а не светлее: кепка сама розовая, и
         на светлом углу фона держалась только за счёт обводки. С тёмной
         подложкой светлая кепка отделяется тоном, а не одной линией; текст
         справа она не задевает, поэтому белый заголовок не теряет контраст. -->
    <ellipse cx="${mCx}" cy="${COVER_H / 2}" rx="${mW * 0.72}" ry="${markH * 0.66}" fill="url(#cover-halo)"/>
    <ellipse cx="${mCx}" cy="${COVER_H / 2 + markH / 2 - 4}" rx="${mW * 0.42}" ry="16" fill="#7A1F45" opacity=".30" filter="url(#cover-soft)"/>
    <g transform="translate(${mCx},${COVER_H / 2}) scale(${mScale}) translate(${-bCx},${-bCy})">${inner}</g>
    <text x="${textX}" y="${titleY}" font-family="${FONT}" font-weight="700" font-size="${titleSize}" letter-spacing="1" fill="#FFFFFF">Оплатыч</text>
    <text x="${textX}" y="${subY}" font-family="${FONT}" font-weight="500" font-size="${subSize}" letter-spacing=".2" fill="#FFFFFF" fill-opacity=".86">заявки на оплату счетов</text>
  </g>
</svg>`;
}

const mark = readMark();
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: SIZE, height: SIZE }, deviceScaleFactor: 1 });

const { radius, box } = await scanMark(page, mark);
const scale = Math.floor((SAFE / radius) * 1000) / 1000;
console.log(`радиус марки ${radius.toFixed(1)} ед. → масштаб ${scale} (безопасный круг r=${SAFE})`);

for (const [name, svg, transparent] of [
  ['avatar-512', svgAvatar(mark, scale), false],
  ['mark-512', svgMark(mark, scale, false), true],
  ['mark-neutral-512', svgMark(mark, scale, true), true],
]) {
  writeFileSync(join(BRAND, `${name}.svg`), svg + '\n');
  writeFileSync(join(BRAND, `${name}.png`), await shot(page, svg, transparent));
  console.log(`${name}.svg + .png готовы`);
}

for (const { w, h } of COVERS) {
  const name = `cover-${w}x${h}`;
  const svg = await svgCover(page, mark, box, w, h);
  await page.setViewportSize({ width: w, height: h });
  writeFileSync(join(BRAND, `${name}.svg`), svg + '\n');
  writeFileSync(join(BRAND, `${name}.png`), await shot(page, svg, false));
  console.log(`${name}.svg + .png готовы`);
}
await browser.close();
