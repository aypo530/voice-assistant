/* Автономный режим: ассистент целиком работает в браузере, когда нет связи с сервером.
 * Это порт серверных модулей textutil / localnlu / search / assistant (Python) на JavaScript, без внешних библиотек.
 * Записи хранятся в localStorage; при восстановлении связи изменения отправляются на сервер (sync).
 * Поведение сверяется с Python-версией тестом app/tests/diff_offline.py. */
(function (global) {
'use strict';

// ---------------------------------------------------------------- регулярные выражения (Python → JS)
// В JS \b и \w понимают только латиницу, поэтому переводим шаблоны на Unicode-классы.
const WCH = '\\p{L}\\p{N}_';
const _rxCache = new Map();
function conv(src) {
  let out = '', cls = false;
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (c === '\\') {
      const n = src[++i];
      if (n === 'w') out += cls ? WCH : `[${WCH}]`;
      else if (n === 'W') out += `[^${WCH}]`;
      else if (n === 'b' && !cls) out += `(?:(?<=[${WCH}])(?![${WCH}])|(?<![${WCH}])(?=[${WCH}]))`;
      else out += '\\' + n;
      continue;
    }
    if (cls) { if (c === ']') cls = false; out += c; continue; }
    if (c === '[') { cls = true; out += c; continue; }
    if (c === '(' && src.startsWith('(?P<', i)) { out += '(?<'; i += 3; continue; }
    out += c;
  }
  return out;
}
function rx(src, flags) {
  const key = src + '\u0000' + (flags || '');
  let r = _rxCache.get(key);
  if (!r) { r = new RegExp(conv(src), 'u' + (flags || '')); _rxCache.set(key, r); }
  return r;
}
const rxs = (src) => rx(src);                       // для search / test (без g)
const rxStart = (src) => rx('^(?:' + src + ')');    // как re.match
function search(src, s) { return rxs(src).exec(s); }
function test(src, s) { return rxs(src).test(s); }
function sub(src, repl, s, flags) { return s.replace(rx(src, 'g' + (flags || '')), repl); }

// ---------------------------------------------------------------- даты
const MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];
const WEEKDAYS_ON = ['в понедельник', 'во вторник', 'в среду', 'в четверг', 'в пятницу', 'в субботу', 'в воскресенье'];
const WEEKDAYS_SHORT = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
const pad = (n, w = 2) => String(n).padStart(w, '0');

function mkDate(y, m, d) {            // null, если даты не существует (как ValueError в Python)
  if (!(y >= 1 && y <= 9999) || m < 1 || m > 12 || d < 1) return null;
  const dt = new Date(Date.UTC(y, m - 1, d));
  dt.setUTCFullYear(y);
  return (dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d) ? dt : null;
}
const addDays = (dt, n) => new Date(dt.getTime() + n * 86400000);
const iso = (dt) => `${pad(dt.getUTCFullYear(), 4)}-${pad(dt.getUTCMonth() + 1)}-${pad(dt.getUTCDate())}`;
const wday = (dt) => (dt.getUTCDay() + 6) % 7;               // понедельник = 0
const sameDay = (a, b) => a.getTime() === b.getTime();
const dayDiff = (a, b) => Math.round((a.getTime() - b.getTime()) / 86400000);
function monthLen(y, m) { return new Date(Date.UTC(y, m, 0)).getUTCDate(); }
function monday(d) { return addDays(d, -wday(d)); }

function parseDate(v) {
  if (!v) return null;
  const m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})/.exec(String(v).trim().slice(0, 10) + ' ');
  return m ? mkDate(+m[1], +m[2], +m[3]) : null;
}
function parseTime(v) {
  if (!v) return null;
  const m = /^([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/.exec(String(v).trim());
  return m ? `${pad(+m[1])}:${m[2]}` : null;
}
function shortDate(d, today) {
  const s = `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
  return d.getUTCFullYear() === today.getUTCFullYear() ? s : `${s} ${d.getUTCFullYear()}`;
}
function humanDate(d, today) {
  const diff = dayDiff(d, today);
  const names = { 0: 'сегодня', 1: 'завтра', 2: 'послезавтра', '-1': 'вчера' };
  const base = shortDate(d, today);
  if (diff in names) return `${names[diff]}, ${base}`;
  if (diff > 2 && diff < 7) return `${WEEKDAYS_ON[wday(d)]}, ${base}`;
  return `${WEEKDAYS_SHORT[wday(d)]}, ${base}`;
}
function whenPhrase(d, t, today) {
  const parts = [];
  if (d) parts.push(humanDate(d, today));
  if (t) parts.push(`в ${t}`);
  return parts.join(', ');
}
function periodPhrase(start, end, today) {
  if (sameDay(start, end)) {
    const names = { 0: 'на сегодня', 1: 'на завтра', 2: 'на послезавтра' };
    const k = dayDiff(start, today);
    return names[k] || `на ${shortDate(start, today)}`;
  }
  if (start.getUTCMonth() === end.getUTCMonth() && start.getUTCFullYear() === end.getUTCFullYear())
    return `с ${start.getUTCDate()} по ${end.getUTCDate()} ${MONTHS[end.getUTCMonth()]}`;
  return `с ${shortDate(start, today)} по ${shortDate(end, today)}`;
}

// «сейчас»: date — сегодняшняя дата (UTC-представление), hm — "HH:MM", ts — миллисекунды
function makeNow(ts) {
  const d = new Date(ts);
  return { ts, date: mkDate(d.getFullYear(), d.getMonth() + 1, d.getDate()), hm: `${pad(d.getHours())}:${pad(d.getMinutes())}` };
}
function nowPlus(now, ms) { return makeNow(now.ts + ms); }

// ---------------------------------------------------------------- слова и поиск по словам (textutil)
const STOP = new Set(['про', 'что', 'для', 'как', 'это', 'мой', 'моя', 'мои', 'его', 'ней', 'она', 'они', 'когда', 'где', 'был', 'была',
  'было', 'были', 'есть', 'нет', 'или', 'при', 'над', 'под', 'без', 'найди', 'найти', 'задача', 'задачу', 'встреча', 'встречу', 'ставил', 'поставил']);
const words = (text) => ((text || '').toLowerCase().replace(/ё/g, 'е').match(rx('\\w+', 'g')) || []);
function queryStems(query) {
  const out = [];
  for (const w of words(query)) {
    if (w.length < 3 || STOP.has(w)) continue;
    out.push(w.length > 5 ? w.slice(0, 5) : (w.length > 4 ? w.slice(0, -1) : w));
  }
  return out;
}
function matchScore(stems, title) {
  const tw = words(title);
  return stems.reduce((n, s) => n + (tw.some(w => w.startsWith(s)) ? 1 : 0), 0);
}

// ---------------------------------------------------------------- поиск по смыслу: n-граммы + синонимы (search.py)
const SYNONYMS = [
  ['договор', 'контракт', 'соглашен', 'подписать'], ['презентац', 'доклад', 'слайд', 'выступлен'],
  ['отчет', 'отчёт', 'справк', 'сводк', 'документ'], ['встреч', 'созвон', 'совещан', 'планерк', 'митинг', 'переговор'],
  ['врач', 'доктор', 'поликлиник', 'стоматолог', 'терапевт', 'больниц', 'приём', 'прием'],
  ['купить', 'покупк', 'заказать', 'приобрест', 'магазин'], ['билет', 'авиабилет', 'поезд', 'рейс', 'самолет', 'самолёт'],
  ['позвонить', 'звонок', 'созвониться', 'набрать', 'телефон'], ['письмо', 'почт', 'email', 'емейл', 'написать'],
  ['оплатить', 'оплат', 'счет', 'счёт', 'платеж', 'платёж', 'квитанц'], ['деньги', 'перевод', 'перевести', 'банк', 'карта'],
  ['уборк', 'убрать', 'пропылесосить', 'помыть'], ['тренировк', 'спортзал', 'зал', 'фитнес', 'бег', 'пробежк'],
  ['день рождения', 'подарок', 'поздравить', 'праздник'], ['машин', 'авто', 'автомобил', 'техосмотр', 'шиномонтаж'],
];
const STOP_NG = new Set(['про', 'что', 'для', 'как', 'это', 'или', 'при', 'над', 'под', 'без', 'найди', 'задача', 'встреча']);
const NGRAM_FLOOR = 0.18;

function expand(text) {
  const ws = words(text), extra = [];
  for (const grp of SYNONYMS) {
    if (grp.some(s => ws.some(w => w.startsWith(s.replace(/ё/g, 'е').slice(0, 6))))) extra.push(...grp);
  }
  return extra.length ? text + ' ' + extra.join(' ') : text;
}
function grams(text) {
  const c = new Map();
  for (const w of words(text)) {
    if (w.length < 2 || STOP_NG.has(w)) continue;
    const p = `_${w}_`;
    for (let i = 0; i < p.length - 2; i++) { const g = p.slice(i, i + 3); c.set(g, (c.get(g) || 0) + 1); }
  }
  return c;
}
function cosMap(a, b) {
  let dot = 0, na = 0, nb = 0;
  for (const [k, v] of a) dot += v * (b.get(k) || 0);
  for (const v of a.values()) na += v * v;
  for (const v of b.values()) nb += v * v;
  na = Math.sqrt(na); nb = Math.sqrt(nb);
  return na && nb ? dot / (na * nb) : 0;
}
function ngramRank(query, items) {
  const docs = items.map(i => grams(expand(i.title)));
  const q = grams(expand(query));
  const n = docs.length + 1, df = new Map();
  for (const d of [...docs, q]) for (const k of d.keys()) df.set(k, (df.get(k) || 0) + 1);
  const idf = new Map([...df].map(([k, v]) => [k, Math.log(n / v) + 1.0]));
  const wq = new Map([...q].map(([k, v]) => [k, v * idf.get(k)]));
  return items.map((it, i) => [it, cosMap(wq, new Map([...docs[i]].map(([k, v]) => [k, v * idf.get(k)])))]);
}
function semantic(query, items, limit = 5) {
  if (!items.length) return [];
  let ranked = ngramRank(query, items).filter(([, s]) => s >= NGRAM_FLOOR);
  ranked.sort((a, b) => b[1] - a[1]);
  if (ranked.length) { const top = ranked[0][1]; ranked = ranked.filter(([, s]) => s >= top - 0.12).slice(0, limit); }
  return ranked.map(([it]) => it);
}
function find(query, items, limit = 10) {
  query = (query || '').trim();
  if (!items.length) return [[], 'none'];
  const stems = queryStems(query);
  const byId = [...items].sort((a, b) => a.id - b.id);
  if (!stems.length) return [byId.slice(-limit).reverse(), 'exact'];
  const scored = items.map(r => [matchScore(stems, r.title), r]);
  scored.sort((a, b) => b[0] - a[0]);
  const hits = scored.filter(([s]) => s > 0).slice(0, limit).map(([, r]) => r);
  if (hits.length) return [hits, 'exact'];
  const found = semantic(query, byId.slice(-150));
  return [found, found.length ? 'semantic' : 'none'];
}

// ---------------------------------------------------------------- локальный разбор фраз (localnlu.py)
const UNITS = { 'один': 1, 'одну': 1, 'одна': 1, 'одно': 1, 'два': 2, 'две': 2, 'три': 3, 'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9 };
const TEENS = { 'десять': 10, 'одиннадцать': 11, 'двенадцать': 12, 'тринадцать': 13, 'четырнадцать': 14, 'пятнадцать': 15,
  'шестнадцать': 16, 'семнадцать': 17, 'восемнадцать': 18, 'девятнадцать': 19 };
const TENS = { 'двадцать': 20, 'тридцать': 30, 'сорок': 40, 'пятьдесят': 50 };
const NUMS = { ...UNITS, ...TEENS, ...TENS, 'ноль': 0 };
const NUM_WORD = String.raw`(?:(?:двадцать|тридцать|сорок|пятьдесят)(?:\s+(?:одну|один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять))?` +
  String.raw`|одиннадцать|двенадцать|тринадцать|четырнадцать|пятнадцать|шестнадцать|семнадцать|восемнадцать|девятнадцать` +
  String.raw`|десять|одну|один|одна|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять)`;
const NUM = String.raw`(?:\d{1,3}|${NUM_WORD})`;
const HOUR_WORD = String.raw`(?:одиннадцать|двенадцать|десять|девять|восемь|семь|шесть|пять|четыре|три|два|один)`;
const MONTH_RE = MONTHS.join('|');
const WD = [String.raw`понедельник\w*`, String.raw`вторник\w*`, String.raw`сред[ауые]\b`, String.raw`четверг\w*`, String.raw`пятниц\w*`, String.raw`суббот\w*`, String.raw`воскресень\w*`];
const WD_ANY = '(?:' + WD.join('|') + ')';

const GLUED = String.raw`\b(?<h>одиннадцать|двенадцать|десять|девять|восемь|восьм|семь|шесть|пять|четыре|три|два|один)(?<ap>утра|вечера|дня|ночи)\b`;
function fixGlued(text) {
  return text.replace(rx(GLUED, 'gi'), (...a) => {
    const g = a[a.length - 1];
    return (g.h.toLowerCase() === 'восьм' ? 'восемь' : g.h) + ' ' + g.ap;
  });
}
const BARE_TIME = String.raw`^\s*(?:на\s+|в\s+)?(?<h>\d{1,2}|${HOUR_WORD})(?:[:.\s]+(?<m>[0-5]\d))?\s*(?:час\w*)?\s*$`;

function toInt(s) {
  s = s.trim();
  if (/^\d+$/.test(s)) return parseInt(s, 10);
  return s.split(/\s+/).reduce((a, p) => a + (NUMS[p] || 0), 0);
}
const isDigits = (s) => /^\d+$/.test(s);

class Text {
  constructor(raw) {
    const norm = raw.toLowerCase().replace(/ё/g, 'е');
    this.raw = norm.length === raw.length ? raw : norm;
    this.norm = norm;
    this.w = norm.split('');
    this.cut = new Array(this.raw.length).fill(false);
  }
  s() { return this.w.join(''); }
  search(p) { return search(p, this.s()); }
  take(m) { for (let i = m.index; i < m.index + m[0].length; i++) { this.w[i] = ' '; this.cut[i] = true; } }
  takeAll(p) {
    let found = false;
    for (;;) {
      const m = this.search(p);
      if (!m || m[0].length === 0) return found;
      this.take(m); found = true;
    }
  }
  remainder() {
    let out = '';
    for (let i = 0; i < this.raw.length; i++) out += this.cut[i] ? ' ' : this.raw[i];
    return out.replace(/\s+/g, ' ').trim();
  }
  remainderNorm() { return this.s().replace(/\s+/g, ' ').trim(); }
}

function weekdayDate(idx, today, mod = '', shift = 0) {
  const base = monday(today);
  if (shift || mod.startsWith('следующ') || mod.startsWith('будущ')) return addDays(base, 7 + idx);
  if (['эту', 'этот', 'этой'].includes(mod)) {
    const d = addDays(base, idx);
    return d.getTime() >= today.getTime() ? d : addDays(d, 7);
  }
  const delta = (((idx - wday(today)) % 7) + 7) % 7;
  return addDays(today, delta || 7);
}
function dayMonth(day, month, today, year) {
  let d = mkDate(year || today.getUTCFullYear(), month, day);
  if (!d) return null;
  if (year == null && d.getTime() < today.getTime()) {
    d = mkDate(today.getUTCFullYear() + 1, month, day);
    if (!d) return null;
  }
  return d;
}
function to24(h, ap, literal = '') {
  if (ap === 'вечера' || ap === 'дня') return h < 12 ? h + 12 : h;
  if (ap === 'ночи') return h === 12 ? 0 : h;
  if (ap === 'утра') return h;
  if (literal.startsWith('0') || h === 0 || h >= 8) return h;
  return (h >= 1 && h <= 7) ? h + 12 : h;
}
const hourOf = (raw) => (isDigits(raw) ? parseInt(raw, 10) : (raw in NUMS ? NUMS[raw] : -1));

function newParsed() {
  return { date: null, time: null, week_shift: 0, this_week: false, weekend: false, to_week_end: false, range: null, relative_time: false };
}

function scanWhen(t, now) {
  const p = newParsed();
  const today = now.date;
  let m;

  m = t.search(String.raw`\bчерез\s+(?:полчаса|(?:(?<n>${NUM})\s+)?(?<u>час\w*|минут\w*|мин\b|дн\w*|день|недел\w*))`);
  if (m) {
    t.take(m);
    const setRel = (dt) => { p.date = dt.date; p.time = dt.hm; p.relative_time = true; };
    if (m[0].includes('полчаса')) setRel(nowPlus(now, 30 * 60000));
    else {
      const n = m.groups.n ? toInt(m.groups.n) : 1, u = m.groups.u;
      if (u.startsWith('час')) setRel(nowPlus(now, n * 3600000));
      else if (u.startsWith('мин')) setRel(nowPlus(now, n * 60000));
      else if (u.startsWith('недел')) p.date = addDays(today, 7 * n);
      else p.date = addDays(today, n);
    }
  }

  if ((m = t.search(String.raw`\b(?:на|в|за)\s+следующ\w+\s+недел\w+|\bна\s+будущ\w+\s+недел\w+`))) { p.week_shift = 1; t.take(m); }
  if ((m = t.search(String.raw`\b(?:на|в|за)\s+(?:этой|эту|текущей|эту)\s+недел\w+|\bна\s+недел[юие]\b`))) { p.this_week = true; t.take(m); }
  if ((m = t.search(String.raw`\bна\s+выходн\w+|\bв\s+выходн\w+`))) { p.weekend = true; t.take(m); }
  if ((m = t.search(String.raw`\bдо\s+конца\s+недели\b`))) { p.to_week_end = true; t.take(m); }
  if ((m = t.search(String.raw`\bдо\s+конца\s+месяца\b`))) {
    p.date = mkDate(today.getUTCFullYear(), today.getUTCMonth() + 1, monthLen(today.getUTCFullYear(), today.getUTCMonth() + 1));
    t.take(m);
  }
  if ((m = t.search(String.raw`\b(?:на|в)\s+(?:ближайш\w+|следующ\w+)\s+(?<n>${NUM})?\s*(?:дн\w+|день)\b`))) {
    const n = m.groups.n ? toInt(m.groups.n) : 7;
    p.range = [today, addDays(today, Math.max(n - 1, 0))];
    t.take(m);
  }
  if ((m = t.search(String.raw`\b(?:в|на)\s+(?:этом|текущем)\s+месяц\w*|\bна\s+месяц\b`))) {
    p.range = [today, mkDate(today.getUTCFullYear(), today.getUTCMonth() + 1, monthLen(today.getUTCFullYear(), today.getUTCMonth() + 1))];
    t.take(m);
  }

  if (p.date === null && (m = t.search(String.raw`\b(\d{4})-(\d{2})-(\d{2})\b`))) {
    const d = mkDate(+m[1], +m[2], +m[3]);
    if (d) { p.date = d; t.take(m); }
  }
  if (p.date === null && (m = t.search(String.raw`\b(?:на\s+|к\s+|до\s+)?(\d{1,2})\s+(${MONTH_RE})(?:\s+(\d{4}))?`))) {
    const d = dayMonth(+m[1], MONTHS.indexOf(m[2]) + 1, today, m[3] ? +m[3] : null);
    if (d) { p.date = d; t.take(m); }
  }
  if (p.date === null && (m = t.search(String.raw`\b(?:на\s+|к\s+|до\s+)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b`)) && +m[2] >= 1 && +m[2] <= 12) {
    let y = m[3] ? +m[3] : null;
    if (y !== null && y < 100) y += 2000;
    const d = dayMonth(+m[1], +m[2], today, y);
    if (d) { p.date = d; t.take(m); }
  }

  if (p.date === null && (m = t.search(String.raw`\b(?:на\s+|с\s+|до\s+)?(сегодня|завтра|послезавтра|вчера|позавчера)\b`))) {
    const off = { 'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'вчера': -1, 'позавчера': -2 }[m[1]];
    p.date = addDays(today, off);
    t.take(m);
  }

  if (p.date === null && (m = t.search(String.raw`\b(?:(?:в|во|на|к|до)\s+)?(?:(?<mod>эту|этот|этой|следующ\w+|ближайш\w+|будущ\w+)\s+)?(?<wd>${WD_ANY})`))) {
    const wd = m.groups.wd;
    const idx = WD.findIndex(pat => rxStart(pat).test(wd));
    p.date = weekdayDate(idx, today, m.groups.mod || '', p.week_shift);
    t.take(m);
  }

  if (p.time === null && (m = t.search(String.raw`\bв\s+полдень\b`))) { p.time = '12:00'; t.take(m); }
  if (p.time === null && (m = t.search(String.raw`\bв\s+полночь\b`))) { p.time = '00:00'; t.take(m); }
  if (p.time === null && (m = t.search(String.raw`\b(?:(?:в|к|до|около|ровно|на)\s+)?(?<h>[01]?\d|2[0-3]):(?<m>[0-5]\d)(?:\s*(?<ap>утра|вечера|дня|ночи))?`))) {
    const h = to24(parseInt(m.groups.h, 10), m.groups.ap || null, m.groups.h);
    p.time = `${pad(h)}:${m.groups.m}`;
    t.take(m);
  }
  if (p.time === null && (m = t.search(String.raw`\b(?:(?:в|к|до|около|на)\s+)?(?<h>\d{1,2}|${HOUR_WORD})(?:[:.\s]+(?<mm>[0-5]\d))?(?:\s+час\w*)?\s+(?<ap>утра|вечера|дня|ночи)\b`))) {
    const h = hourOf(m.groups.h);
    if (h >= 0 && h <= 23) { p.time = `${pad(to24(h, m.groups.ap, m.groups.h))}:${m.groups.mm || '00'}`; t.take(m); }
  }
  if (p.time === null && (m = t.search(String.raw`\b(?:в|к|до|около|на)\s+(?<h>\d{1,2}|${HOUR_WORD})(?:\s+час\w*)?(?:\s+(?<ap>утра|вечера|дня|ночи))?` +
      String.raw`(?!\s*(?:\d|раз\b|человек|штук|шт\b|минут|секунд|дн|недел|месяц|год|лет\b|рубл|тысяч|процент|%))`))) {
    const h = hourOf(m.groups.h);
    if (h >= 0 && h <= 23) { p.time = `${pad(to24(h, m.groups.ap || null, m.groups.h))}:00`; t.take(m); }
  }
  return p;
}

const RE_HELP = String.raw`\b(?:что\s+ты\s+(?:умеешь|можешь)|помощь|помоги\s+мне\s+разобраться|как\s+тобой\s+пользоваться|как\s+(?:это\s+)?работает)\b`;
const RE_GREET = String.raw`^(?:привет\w*|здравствуй\w*|добрый\s+(?:день|вечер|утро)|доброе\s+утро|хай|hello|hi|спасибо\w*|благодар\w+|пока|до\s+свидания|ок|окей|ясно|понятно|отлично|хорошо)\W*$`;
const RE_CANCEL = String.raw`^(?:нет|отмена|отмени\w*|не\s+надо|не\s+нужно|забудь|стоп)\b`;
const RE_COMPLETE = String.raw`\b(?:отмет\w+|помет\w+|закро\w+|заверш\w+)\b.*\b(?:выполнен\w+|сделан\w+|готов\w+|заверш\w+)\b` +
  String.raw`|\b(?:отмет\w+|помет\w+|закро\w+)\s+(?:задач\w*|встреч\w*|напоминани\w*|дел\w*)` +
  String.raw`|\b(?:отмет\w+|помет\w+)\b` +
  String.raw`|\bгалочк\w*` +
  String.raw`|^(?:выполнено|сделано|готово)\b` +
  String.raw`|\b(?:выполнен[аоы]?|сделан[аоы]?|готов[аоы]?|завершен[аоы]?)\W*$` +
  String.raw`|\b(?:я\s+)?(?:уже\s+)?(?:выполнил\w*|сделал\w*|закончил\w*|завершил\w*)\b` +
  String.raw`|^я\s+(?:уже\s+)?\w+(?:ил|ыл|ал|ел|ял|ул|ол)(?:а)?\b`;
const RE_DELETE = String.raw`^(?:пожалуйста\s+)?(?:удали\w*|сотри|стери|убери|очисти\w*)\b` +
  String.raw`|\b(?:удали\w*|убери)\s+(?:задач\w*|встреч\w*|напоминани\w*|запис\w*|заметк\w*)` +
  String.raw`|^(?:пожалуйста\s+)?отмени\w*\s+(?:задач\w*|встреч\w*|напоминани\w*|запис\w*)`;
const RE_UPDATE = String.raw`\b(?:перенеси\w*|перенести|перемести\w*|передвин\w*|сдвин\w*|отложи\w*|поменяй\w*\s+(?:время|дат\w*)|измени\w*\s+(?:время|дат\w*))\b`;
const RE_FIND = String.raw`\b(?:найди|найти|найдешь|поищи|поискать|искать|ищи|отыщи|вспомни|подскажи\s+где)\b`;
const RE_LIST = [
  String.raw`^(?:а\s+)?(?:что|чем)\s+(?:у\s+меня|мне|запланирован\w*|намечен\w*|на\b|по\s+план|в\b|есть|буд\w+)`,
  String.raw`\bкакие\s+(?:у\s+меня\s+)?(?:планы|задачи|встречи|дела|напоминания|события)\b`,
  String.raw`\b(?:покажи|показать|выведи|назови|перечисли|расскажи|скажи)\b`,
  String.raw`\b(?:есть|будут|будет)\s+ли\b`,
  String.raw`\bсписок\s+(?:задач|встреч|дел|планов)\b|\bрасписание\b|\bповестка\b|\bпланы\s+на\b|\bчто\s+по\s+план`,
  String.raw`\bсколько\s+(?:у\s+меня\s+)?(?:задач|встреч|дел)\b`,
];
const RE_WHEN_SEARCH = String.raw`\bкогда\b.*\b(?:был[аио]?|были|ставил\w*|поставил\w*|создавал\w*|записывал\w*|планировал\w*|назначал\w*)\b` +
  String.raw`|\bкогда\s+(?:у\s+меня\s+)?(?:встреча|задача|напоминание|созвон)\b` +
  String.raw`|\bя\s+(?:когда-?то|как-?то|ранее|раньше|давно)\b|\bпомнишь\b|\bгде\s+(?:моя\s+|мое\s+)?(?:задача|встреча|запись|напоминание)\b`;
const RE_CREATE_VERB = String.raw`\b(?:напомни\w*|поставь|поставить|добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|` +
  String.raw`назначь|назначить|внеси|не\s+забудь|нужно|надо|необходимо|пора)\b`;
const RE_REMINDER = String.raw`\bнапомни\w*|\bнапоминани\w*`;
const RE_MEETING = String.raw`\b(?:встреч\w*|созвон\w*|совещани\w*|планерк\w*|митинг\w*|собеседовани\w*|прием\w*|визит\w*|консультаци\w*)` +
  String.raw`|\b(?:к|у)\s+(?:врач\w*|стоматолог\w*|доктор\w*|парикмахер\w*|терапевт\w*|окулист\w*)`;
const KIND_NOUNS = [['meeting', String.raw`\b(?:встреч\w*|созвон\w*|совещани\w*)`], ['task', String.raw`\bзадач\w*|\bдел[оа]\b`], ['reminder', String.raw`\bнапоминани\w*`]];
const Q_DROP = String.raw`\b(?:найди|найти|найдешь|поищи|поискать|искать|ищи|отыщи|вспомни|покажи|показать|скажи|подскажи|расскажи|` +
  String.raw`когда|где|что|какая|какой|какую|какие|был[аио]?|были|будет|будут|ставил\w*|поставил\w*|создавал\w*|создал\w*|` +
  String.raw`записывал\w*|записал\w*|добавлял\w*|планировал\w*|назначал\w*|у\s+меня|мне|мой|моя|мою|мои|мое|ее|его|их|` +
  String.raw`я|мы|ты|вы|когда-?то|как-?то|ранее|раньше|давно|помнишь|пожалуйста|ли|задач\w*|встреч\w*|напоминани\w*|` +
  String.raw`запис\w*|дело|дела|дел|про|по|о|об|обо|насчет|относительно|связанн\w*|касающ\w*|` +
  String.raw`с|со|в|во|на|к|для|из|от|и|а|но|то|же|бы|это|эту|этот|уже|отмет\w+|помет\w+|закро\w+|заверш\w+|` +
  String.raw`выполнен\w*|выполнил\w*|сделан\w*|сделал\w*|закончил\w*|готов\w*|галочк\w*|поставь|как|что|которую|который)\b`;
const TARGET_DROP = String.raw`\b(?:пожалуйста|удали\w*|сотри|стери|убери|очисти\w*|отмени\w*|перенеси\w*|перенести|перемести\w*|передвин\w*|` +
  String.raw`сдвин\w*|отложи\w*|поменяй\w*|измени\w*|время|дат[ауы]|последн\w+|заметк\w*|запис\w*|ее|его|их|эту|этот|это|ту|тот|` +
  String.raw`мне|мою|мой|моя|мое|мои|про|по|о|об|с|со|в|во|на|к|и|а|для|из|от|у|меня)\b`;
const Q_KIND = String.raw`\b(?:задач\w*|встреч\w*|напоминани\w*)\b`;

function newCmd(transcript) {
  return { intent: 'other', transcript: transcript || '', kind: null, title: null, date: null, time: null,
    range_start: null, range_end: null, query: null, ref: null, reply: null };
}
function kindFromNouns(n) {
  const found = KIND_NOUNS.filter(([, pat]) => test(pat, n)).map(([k]) => k);
  return found.length === 1 ? found[0] : null;
}
function queryOf(n) {
  let q = sub(Q_DROP, ' ', n);
  q = sub(String.raw`[^\w\s]`, ' ', q);
  return q.replace(/\s+/g, ' ').trim();
}
function titleClean(t, kind) {
  t.takeAll(String.raw`\b(?:пожалуйста|плиз)\b`);
  t.takeAll(String.raw`\bнапомни(?:ть)?(?:\s+мне)?\b`);
  t.takeAll(String.raw`\b(?:поставь|поставить|добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|` +
    String.raw`внеси|сделай|назначь|назначить)\s+(?:мне\s+)?(?:новую\s+|новое\s+)?(?:задач\w*|напоминани\w*|заметк\w*)?`);
  t.takeAll(String.raw`\b(?:мне\s+)?(?:нужно|надо|необходимо|придется|не\s+забудь|не\s+забыть|пора)\b`);
  t.takeAll(String.raw`\bу\s+меня\b`);
  const rem = t.remainder();
  const ws = rem ? rem.split(' ') : [];
  const stop = new Set(['в', 'на', 'к', 'до', 'по', 'около', 'и', 'что', 'чтобы', 'а', 'с', 'во']);
  const strip = (w) => w.toLowerCase().replace(/^[,.:;]+|[,.:;]+$/g, '');
  while (ws.length && (stop.has(strip(ws[0])) || (kind === 'reminder' && ['о', 'про', 'об'].includes(ws[0].toLowerCase())))) ws.shift();
  while (ws.length && stop.has(strip(ws[ws.length - 1]))) ws.pop();
  let title = ws.join(' ').replace(/^[ .,!?;:\-—]+|[ .,!?;:\-—]+$/g, '');
  title = sub(String.raw`^(?:[Вв]стречу)\b`, 'Встреча', title);
  return title ? title[0].toUpperCase() + title.slice(1) : '';
}
function listPeriod(p, today) {
  if (p.range) return p.range;
  if (p.date) return [p.date, p.date];
  if (p.week_shift) { const mon = addDays(monday(today), 7); return [mon, addDays(mon, 6)]; }
  if (p.this_week || p.to_week_end) return [today, addDays(monday(today), 6)];
  if (p.weekend) { const sat = addDays(monday(today), 5); return [sat.getTime() > today.getTime() ? sat : today, addDays(sat, 1)]; }
  return [today, today];
}
const hasWhen = (p) => !!(p.date || p.time || p.to_week_end || p.week_shift);

function asSearch(cmd, text, n, now) {
  const t = new Text(text);
  scanWhen(t, now);
  cmd.intent = 'search'; cmd.kind = kindFromNouns(n); cmd.query = queryOf(t.remainderNorm());
  return cmd;
}
function asTarget(cmd, text, n, now, intent) {
  const t = new Text(text);
  const p = scanWhen(t, now);
  const rest = t.remainderNorm();
  let d = p.date || (p.week_shift ? addDays(monday(now.date), 7) : null);
  if (d === null && p.to_week_end) d = addDays(monday(now.date), 6);
  cmd.intent = intent; cmd.kind = kindFromNouns(n);
  const q = sub(TARGET_DROP, ' ', sub(Q_KIND, ' ', rest));
  cmd.query = sub(String.raw`[^\w\s]`, ' ', q).replace(/\s+/g, ' ').trim();
  if (!cmd.query) cmd.ref = test(String.raw`\bпоследн\w+`, n) ? 'last' : 'it';
  if (intent === 'update') { cmd.date = d ? iso(d) : null; cmd.time = p.time; }
  return cmd;
}
function buildCreate(cmd, t, p, now, pending) {
  const today = now.date, n = t.norm;
  if (pending) {
    const kind = pending.kind || 'task', title = pending.title || '';
    let d = p.date || parseDate(pending.date);
    const tm = p.time || pending.time || null;
    if (p.to_week_end && !d) d = addDays(monday(today), 6);
    cmd.intent = 'create'; cmd.kind = kind; cmd.title = title;
    cmd.date = d ? iso(d) : null; cmd.time = tm;
    return cmd;
  }
  const kind = test(RE_REMINDER, n) ? 'reminder' : (test(RE_MEETING, n) ? 'meeting' : 'task');
  let d = p.date;
  if (d === null && p.to_week_end) d = addDays(monday(today), 6);
  else if (d === null && p.week_shift) d = addDays(monday(today), 7);
  else if (d === null && p.weekend) { const sat = addDays(monday(today), 5); d = sat.getTime() > today.getTime() ? sat : today; }
  const tm = p.time;
  if (d === null && tm && (kind === 'reminder' || kind === 'task')) d = tm > now.hm ? today : addDays(today, 1);
  const title = titleClean(t, kind);
  cmd.intent = 'create'; cmd.kind = kind; cmd.title = title || null;
  cmd.date = d ? iso(d) : null; cmd.time = tm;
  return cmd;
}

function parseCommand(text, now, pending) {
  text = fixGlued((text || '').trim());
  const n = text.toLowerCase().replace(/ё/g, 'е');
  const cmd = newCmd(text);
  if (!n) return cmd;
  const today = now.date;

  if (pending && test(RE_CANCEL, n)) { cmd.intent = 'other'; cmd.reply = 'Хорошо, не буду добавлять.'; return cmd; }
  if (test(RE_GREET, n)) { cmd.intent = 'other'; cmd.reply = null; return cmd; }
  if (test(RE_HELP, n)) { cmd.intent = 'other'; return cmd; }

  if (!n.includes('?') && test(RE_DELETE, n)) return asTarget(cmd, text, n, now, 'delete');
  if (!n.includes('?') && test(RE_UPDATE, n)) return asTarget(cmd, text, n, now, 'update');
  if (!n.includes('?') && test(RE_COMPLETE, n)) {
    const t = new Text(text);
    scanWhen(t, now);
    cmd.intent = 'complete'; cmd.kind = kindFromNouns(n); cmd.query = queryOf(t.remainderNorm());
    return cmd;
  }
  if (test(RE_FIND, n)) return asSearch(cmd, text, n, now);
  if (RE_LIST.some(p => test(p, n))) {
    const t = new Text(text);
    const p = scanWhen(t, now);
    const hasPeriod = !!(p.date || p.range || p.week_shift || p.this_week || p.weekend || p.to_week_end);
    if (!hasPeriod && queryOf(t.remainderNorm()) && test(String.raw`\bпокажи|показать|выведи|назови|перечисли\b`, n)) return asSearch(cmd, text, n, now);
    const [start, end] = listPeriod(p, today);
    cmd.intent = 'list'; cmd.kind = kindFromNouns(n);
    cmd.range_start = iso(start); cmd.range_end = iso(end);
    return cmd;
  }
  if (test(RE_WHEN_SEARCH, n)) return asSearch(cmd, text, n, now);

  if (pending && pending.op === 'move') {
    const t = new Text(text);
    const p = scanWhen(t, now);
    if (p.date || p.time || p.week_shift || p.to_week_end) {
      let d = p.date || (p.week_shift ? addDays(monday(today), 7) : null);
      if (d === null && p.to_week_end) d = addDays(monday(today), 6);
      cmd.intent = 'update'; cmd.ref = 'it';
      cmd.date = d ? iso(d) : null; cmd.time = p.time;
      return cmd;
    }
    pending = null;
  }
  if (pending && !test(RE_CREATE_VERB, n) && !test(RE_MEETING, n)) {
    const t = new Text(text);
    const p = scanWhen(t, now);
    if (p.time === null && !pending.time) {
      const b = rx(BARE_TIME).exec(n);
      if (b) {
        const h = hourOf(b.groups.h);
        if (h >= 0 && h <= 23) p.time = `${pad(to24(h, null, b.groups.h))}:${b.groups.m || '00'}`;
      }
    }
    if (hasWhen(p)) return buildCreate(cmd, t, p, now, pending);
    const tries = (pending._tries || 0) + 1;
    if (tries < 2) {
      pending._tries = tries;
      cmd.intent = 'clarify';
      cmd.reply = 'Не понял. Скажите день и время, например «в 10 утра», «в 15:30» или «завтра в три».';
      return cmd;
    }
  }

  const hasVerb = test(RE_CREATE_VERB, n);
  const t = new Text(text);
  const p = scanWhen(t, now);
  const isMeeting = test(RE_MEETING, n);
  if (n.includes('?') && !hasVerb) { cmd.intent = 'other'; return cmd; }
  if (!(hasVerb || hasWhen(p) || isMeeting) && n.split(/\s+/).filter(Boolean).length < 2) { cmd.intent = 'other'; return cmd; }
  return buildCreate(cmd, t, p, now, null);
}

// ---------------------------------------------------------------- хранилище (localStorage) и журнал изменений
const LS_ROWS = 'oa_rows', LS_JOURNAL = 'oa_journal', LS_NEXT = 'oa_next';
const TEMP_BASE = 1000000000;           // id записей, созданных без связи (до синхронизации)
const STATUSES = ['open', 'done'];
const KINDS = ['task', 'meeting', 'reminder'];
const mem = {};                          // запасной вариант, если localStorage недоступен
const ls = {
  get(k, dflt) { try { const v = global.localStorage.getItem(k); return v === null ? (k in mem ? mem[k] : dflt) : JSON.parse(v); } catch (e) { return k in mem ? mem[k] : dflt; } },
  set(k, v) { mem[k] = v; try { global.localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* память заполнена или запрещена */ } },
};
const store = {
  rows: () => ls.get(LS_ROWS, []),
  save(rows) { ls.set(LS_ROWS, rows); },
  journal: () => ls.get(LS_JOURNAL, []),
  log(entry) { const j = store.journal(); j.push(entry); ls.set(LS_JOURNAL, j); },
  all(kinds) {
    let rows = store.rows();
    if (kinds && kinds.length) rows = rows.filter(r => kinds.includes(r.kind));
    return rows.slice().sort(sortRows);
  },
  get(id) { return store.rows().find(r => r.id === id) || null; },
  add(kind, title, due_date, due_time, created_at) {
    const rows = store.rows();
    const next = Math.max(TEMP_BASE, ls.get(LS_NEXT, TEMP_BASE)) + 1;
    ls.set(LS_NEXT, next);
    const row = { id: next, kind, title, due_date, due_time, status: 'open', created_at, user_id: '' };
    rows.push(row); store.save(rows); store.log({ op: 'add', id: next });
    return row;
  },
  update(id, fields) {
    const rows = store.rows(), t = rows.find(r => r.id === id);
    if (!t) return null;
    const ok = ['kind', 'title', 'due_date', 'due_time', 'status'], applied = {};
    for (const k of ok) if (k in fields) { t[k] = fields[k]; applied[k] = fields[k]; }
    store.save(rows); store.log({ op: 'update', id, fields: applied });
    return { ...t };
  },
  remove(id) {
    const rows = store.rows(), i = rows.findIndex(r => r.id === id);
    if (i < 0) return false;
    rows.splice(i, 1); store.save(rows); store.log({ op: 'delete', id });
    return true;
  },
  findDuplicate(kind, title, d, t) {
    return store.rows().find(r => r.kind === kind && r.due_date === d && r.due_time === t && r.title.toLowerCase() === title.toLowerCase()) || null;
  },
  meetingsAt(d, t) { return store.rows().filter(r => r.kind === 'meeting' && r.status !== 'done' && r.due_date === d && r.due_time === t); },
  range(start, end, kinds) {
    return store.all(kinds).filter(r => r.due_date && start <= r.due_date && r.due_date <= end);
  },
};
function sortRows(a, b) {
  const an = a.due_date === null || a.due_date === undefined, bn = b.due_date === null || b.due_date === undefined;
  if (an !== bn) return an ? 1 : -1;
  const k = (x) => [x.due_date || '', x.due_time || '', x.id];
  const ka = k(a), kb = k(b);
  for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] < kb[i] ? -1 : 1;
  return 0;
}

// ---------------------------------------------------------------- логика ассистента (assistant.py)
const KIND_LABEL = { task: 'задача', meeting: 'встреча', reminder: 'напоминание' };
const KIND_FILTER = { task: ['task', 'reminder'], meeting: ['meeting'], reminder: ['reminder'] };
const HELP = 'Я умею добавлять задачи, напоминания и встречи, показывать планы на любой период, искать старые записи, переносить и удалять их, отмечать задачи выполненными.';
const NOTICE = 'Автономный режим: работаю на этом устройстве без связи с сервером — точность ниже.';
class UserError extends Error {}

const sess = { pending: null, last_id: null };
const pastNow = (d, t, now) => `${iso(d)} ${t}` < `${iso(now.date)} ${now.hm}`;

function line(r, today, withDate = false, fullDate = false) {
  const d = parseDate(r.due_date);
  const t = r.due_time || (d ? 'без времени' : 'без срока');
  let prefix;
  if (fullDate && d) prefix = `${humanDate(d, today)}, ${t}`;
  else if (withDate && d) prefix = `${WEEKDAYS_SHORT[wday(d)]} ${shortDate(d, today)}, ${t}`;
  else prefix = t;
  return `• ${prefix} — ${r.title} (${KIND_LABEL[r.kind]})${r.status === 'done' ? ' — выполнено' : ''}`;
}

function searchItems(query, kind) {
  const pool = store.all(KIND_FILTER[kind || '']);
  let [found, mode] = find(query, pool);
  if (found.length && mode === 'exact' && query.trim()) {
    found = found.slice().sort((a, b) => {
      const ka = [a.due_date || '9999', a.due_time || ''], kb = [b.due_date || '9999', b.due_time || ''];
      return ka[0] !== kb[0] ? (ka[0] < kb[0] ? -1 : 1) : (ka[1] !== kb[1] ? (ka[1] < kb[1] ? -1 : 1) : 0);
    });
  }
  return [found, mode];
}

function doCreate(cmd, now, s) {
  const today = now.date, kind = cmd.kind || 'task';
  const title = (cmd.title || '').replace(/^[ .,!?]+|[ .,!?]+$/g, '');
  if (!title) return ['Что именно нужно записать?', 'clarify'];
  const d = parseDate(cmd.date), t = parseTime(cmd.time), label = KIND_LABEL[kind];
  if (kind === 'meeting' || kind === 'reminder') {
    const noun = kind === 'meeting' ? 'встречу' : 'напоминание';
    const partial = { kind, title, date: d ? iso(d) : null, time: t };
    if (!d && !t) { s.pending = partial; return [`На какой день и время поставить ${noun}?`, 'clarify']; }
    if (!d) { s.pending = partial; return ['На какой день?', 'clarify']; }
    if (!t) { s.pending = partial; return [`На какое время поставить ${noun}?`, 'clarify']; }
    if (pastNow(d, t, now)) return ['Это время уже прошло. На какую дату и время поставить?', 'clarify'];
  }
  const dd = d ? iso(d) : null;
  if (store.findDuplicate(kind, title, dd, t)) {
    return [`Такая ${label} уже есть: «${title}»` + (d || t ? ` — ${whenPhrase(d, t, today)}.` : '.'), 'create'];
  }
  const conflicts = kind === 'meeting' && dd && t ? store.meetingsAt(dd, t) : [];
  const added = store.add(kind, title, dd, t, `${iso(today)}T${now.hm}:00`);
  s.last_id = added.id;
  const when = whenPhrase(d, t, today);
  let reply = `Записано: ${label} «${title}»` + (when ? ` — ${when}.` : '.');
  if (conflicts.length) reply += `\nВнимание: в это время уже есть встреча «${conflicts[0].title}».`;
  return [reply, 'create'];
}
function doList(cmd, now) {
  const today = now.date;
  let start = parseDate(cmd.range_start) || parseDate(cmd.date) || today;
  let end = parseDate(cmd.range_end) || start;
  if (end.getTime() < start.getTime()) [start, end] = [end, start];
  const rows = store.range(iso(start), iso(end), KIND_FILTER[cmd.kind || '']);
  const phrase = periodPhrase(start, end, today);
  const what = { meeting: 'Встреч', task: 'Задач' }[cmd.kind || ''] || 'Записей';
  if (!rows.length) return `${what} ${phrase} нет.`;
  const multi = !sameDay(start, end);
  return `${what} ${phrase}: ${rows.length}.\n` + rows.map(r => line(r, today, multi)).join('\n');
}
function doSearch(cmd, heard, now, s) {
  const today = now.date;
  if (!store.all().length) return 'Пока нет ни одной записи.';
  const [found, mode] = searchItems(cmd.query || heard, cmd.kind);
  if (!found.length) return 'Ничего похожего не нашёл. Попробуйте назвать другое слово из записи.';
  const lines = found.map(r => line(r, today, false, true));
  if (found.length === 1) s.last_id = found[0].id;
  const head = mode === 'semantic' ? 'Точных совпадений нет. Возможно, вы имели в виду:'
    : (found.length === 1 ? 'Нашёл одну запись:' : `Нашёл записей: ${found.length}.`);
  return head + '\n' + lines.join('\n');
}
function doComplete(cmd, now, s) {
  const pool = store.all(KIND_FILTER[cmd.kind || '']).filter(r => r.status !== 'done');
  if (!pool.length) return 'Невыполненных записей нет.';
  if (!cmd.query && s.last_id) {
    const f = pool.filter(r => r.id === s.last_id);
    if (f.length) { store.update(f[0].id, { status: 'done' }); return `Отметил выполненной: ${KIND_LABEL[f[0].kind]} «${f[0].title}».`; }
  }
  const [found] = find(cmd.query || '', pool, 5);
  if (!cmd.query || !found.length) return 'Не нашёл такую невыполненную запись. Назовите её по-другому.';
  if (found.length > 1) {
    return 'Нашёл несколько подходящих записей, какую отметить? Уточните название.\n' + found.map(r => line(r, now.date, false, true)).join('\n');
  }
  const item = found[0];
  s.last_id = item.id;
  store.update(item.id, { status: 'done' });
  return `Отметил выполненной: ${KIND_LABEL[item.kind]} «${item.title}».`;
}
function resolveTarget(cmd, now, s) {
  const pool = store.all(KIND_FILTER[cmd.kind || '']);
  if (!pool.length) return [null, 'Записей пока нет.'];
  if (cmd.query) {
    const [found, mode] = find(cmd.query, pool, 5);
    if (!found.length) return [null, 'Не нашёл такую запись. Назовите её по-другому.'];
    const lines = found.map(r => line(r, now.date, false, true));
    if (mode !== 'exact') return [null, 'Точного совпадения нет. Возможно, вы имели в виду:\n' + lines.join('\n') + '\nУточните название.'];
    if (found.length > 1) return [null, 'Нашёл несколько подходящих записей, какую выбрать? Уточните название.\n' + lines.join('\n')];
    return [found[0], null];
  }
  if (cmd.ref === 'last') return [pool.reduce((a, b) => (b.id > a.id ? b : a)), null];
  const item = s.last_id ? store.get(s.last_id) : null;
  if (!item) return [null, 'О какой записи речь? Назовите её, например: «задачу про договор».'];
  return [item, null];
}
function doDelete(cmd, now, s) {
  const [item, ask] = resolveTarget(cmd, now, s);
  if (ask) return ask;
  store.remove(item.id);
  if (s.last_id === item.id) s.last_id = null;
  return `Удалил: ${KIND_LABEL[item.kind]} «${item.title}».`;
}
function updateItem(id, data) {
  const cur = store.get(id);
  if (!cur) throw new KeyError(id);
  const ch = {};
  if ('title' in data) {
    const title = String(data.title || '').trim();
    if (!title) throw new UserError('Название не может быть пустым.');
    ch.title = title.slice(0, 200);
  }
  if ('kind' in data) { if (!KINDS.includes(data.kind)) throw new UserError('Неизвестный тип записи.'); ch.kind = data.kind; }
  if ('date' in data) {
    const raw = String(data.date || '').trim();
    if (raw && !parseDate(raw)) throw new UserError('Некорректная дата.');
    ch.due_date = raw ? iso(parseDate(raw)) : null;
  }
  if ('time' in data) {
    const raw = String(data.time || '').trim();
    if (raw && !parseTime(raw)) throw new UserError('Некорректное время.');
    ch.due_time = raw ? parseTime(raw) : null;
  }
  if ('status' in data) { if (!STATUSES.includes(data.status)) throw new UserError('Неизвестный статус.'); ch.status = data.status; }
  const kind = 'kind' in ch ? ch.kind : cur.kind;
  const dd = 'due_date' in ch ? ch.due_date : cur.due_date;
  const dt = 'due_time' in ch ? ch.due_time : cur.due_time;
  if ((kind === 'meeting' || kind === 'reminder') && !(dd && dt)) throw new UserError('Для встречи и напоминания нужны дата и время.');
  if (dt && !dd) throw new UserError('Укажите дату вместе со временем.');
  return store.update(id, ch);
}
class KeyError extends Error {}
function doUpdate(cmd, now, s) {
  const [item, ask] = resolveTarget(cmd, now, s);
  if (ask) return [ask, 'clarify'];
  s.last_id = item.id;
  const d = parseDate(cmd.date), t = parseTime(cmd.time);
  if (!d && !t) {
    s.pending = { op: 'move', id: item.id };
    return [`На какую дату и время перенести ${KIND_LABEL[item.kind]} «${item.title}»?`, 'clarify'];
  }
  const data = {};
  if (d) data.date = iso(d);
  if (t) data.time = t;
  if (t && !d && !item.due_date) data.date = iso(now.date);
  const newD = parseDate(data.date) || parseDate(item.due_date);
  const newT = data.time || item.due_time;
  if (newD && newT && pastNow(newD, newT, now)) return ['Это время уже прошло. На какую дату и время перенести?', 'clarify'];
  const updated = updateItem(item.id, data);
  let reply = `Перенёс: ${KIND_LABEL[updated.kind]} «${updated.title}» — ${whenPhrase(newD, newT, now.date)}.`;
  if (updated.kind === 'meeting' && newD && newT) {
    const others = store.meetingsAt(iso(newD), newT).filter(m => m.id !== item.id);
    if (others.length) reply += `\nВнимание: в это время уже есть встреча «${others[0].title}».`;
  }
  return [reply, 'update'];
}

function handleText(text, nowTs) {
  const now = makeNow(nowTs == null ? Date.now() : nowTs);
  const heard = (text || '').trim();
  const cmd = parseCommand(heard, now, sess.pending);
  if (cmd.intent !== 'clarify') sess.pending = null;
  let reply, intent;
  try {
    if (cmd.intent === 'create') [reply, intent] = doCreate(cmd, now, sess);
    else if (cmd.intent === 'list') [reply, intent] = [doList(cmd, now), 'list'];
    else if (cmd.intent === 'search') [reply, intent] = [doSearch(cmd, heard, now, sess), 'search'];
    else if (cmd.intent === 'complete') [reply, intent] = [doComplete(cmd, now, sess), 'complete'];
    else if (cmd.intent === 'delete') [reply, intent] = [doDelete(cmd, now, sess), 'delete'];
    else if (cmd.intent === 'update') [reply, intent] = doUpdate(cmd, now, sess);
    else if (cmd.intent === 'clarify') [reply, intent] = [cmd.reply || 'Уточните, пожалуйста, детали.', 'clarify'];
    else [reply, intent] = [cmd.reply || HELP, 'other'];
  } catch (e) {
    if (e instanceof UserError) [reply, intent] = [e.message, 'error'];
    else { console.error(e); [reply, intent] = ['Что-то пошло не так при выполнении. Попробуйте переформулировать запрос.', 'error']; }
  }
  return { ok: intent !== 'error', transcript: heard, reply, intent, engine: 'local', notice: NOTICE };
}

// ---------------------------------------------------------------- эмуляция API сервера + синхронизация
function httpError(msg) { const e = new Error(msg); e.offlineHandled = true; return e; }
function csv() {
  const q = (v) => { v = String(v ?? ''); return /[;"\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const rows = [['id', 'type', 'title', 'date', 'time', 'status', 'created_at'].join(';')];
  for (const r of store.all()) rows.push([r.id, r.kind, r.title, r.due_date || '', r.due_time || '', r.status, r.created_at].map(q).join(';'));
  return '﻿' + rows.join('\r\n') + '\r\n';
}
async function call(path, opts = {}) {
  const method = (opts.method || 'GET').toUpperCase();
  const url = new URL(path, 'http://x');
  const p = url.pathname;
  let body = {};
  if (opts.body && typeof opts.body === 'string') { try { body = JSON.parse(opts.body); } catch (e) { body = {}; } }
  if (p === '/api/health') {
    return { status: 'ok', cloud: { state: 'device', message: 'Нет связи с сервером — автономный режим на этом устройстве.' },
      mode: 'offline', server_key: false, local_stt: { state: 'missing', message: '' }, local_embed: { state: 'missing' }, multi_user: true, device: true };
  }
  if (p === '/api/message' && method === 'POST') {
    const text = String(body.text || '').trim().slice(0, 1000);
    if (!text) return { ok: false, reply: 'Пустой запрос.', transcript: '', intent: 'error' };
    return handleText(text);
  }
  if (p === '/api/voice') {
    return { ok: true, transcript: '', intent: 'other', engine: 'local', notice: NOTICE,
      reply: 'Без связи с сервером распознавание речи здесь недоступно. Напишите запрос текстом — всё остальное работает.' };
  }
  if (p === '/api/items' && method === 'GET') return store.all();
  if (p === '/api/search') {
    const q = (url.searchParams.get('q') || '').trim().slice(0, 200);
    if (!q) return { items: store.all(), mode: 'all' };
    const [items, mode] = searchItems(q);
    return { items, mode };
  }
  const m = /^\/api\/items\/(\d+)$/.exec(p);
  if (m && method === 'PATCH') {
    try { return updateItem(parseInt(m[1], 10), body); }
    catch (e) { throw httpError(e instanceof KeyError ? 'Запись не найдена.' : (e.message || 'Ошибка')); }
  }
  if (m && method === 'DELETE') {
    if (!store.remove(parseInt(m[1], 10))) throw httpError('Запись не найдена.');
    return { ok: true };
  }
  throw httpError('Эта функция недоступна без связи с сервером.');
}


// ---------------------------------------------------------------- файл с записями: дополнение и слияние
const CSV_HEAD = ['id', 'type', 'title', 'date', 'time', 'status', 'created_at'];
function parseCsv(text) {
  text = String(text || '').replace(/^﻿/, '');
  const delim = (text.split(/\r?\n/)[0] || '').includes(';') ? ';' : ',';
  const rows = []; let row = [], cur = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; } else cur += c; }
    else if (c === '"') q = true;
    else if (c === delim) { row.push(cur); cur = ''; }
    else if (c === '\n' || c === '\r') { if (c === '\r' && text[i + 1] === '\n') i++; row.push(cur); cur = ''; if (row.some(x => x !== '')) rows.push(row); row = []; }
    else cur += c;
  }
  row.push(cur); if (row.some(x => x !== '')) rows.push(row);
  if (!rows.length) return [];
  const head = rows[0].map(h => h.trim().toLowerCase());
  const col = (names) => names.map(n => head.indexOf(n)).find(i => i >= 0);
  const ix = { kind: col(['type', 'kind']), title: col(['title']), date: col(['date']), time: col(['time']), status: col(['status']), created: col(['created_at']) };
  if (ix.title === undefined || ix.kind === undefined) return [];
  const out = [];
  for (const r of rows.slice(1)) {
    const g = (i) => (i === undefined ? '' : (r[i] || '').trim());
    const kind = g(ix.kind), title = g(ix.title);
    if (!title || !['task', 'reminder', 'meeting'].includes(kind)) continue;
    const date = /^\d{4}-\d{2}-\d{2}$/.test(g(ix.date)) ? g(ix.date) : null;
    const time = /^\d{2}:\d{2}$/.test(g(ix.time)) ? g(ix.time) : null;
    out.push({ kind, title, due_date: date, due_time: time, status: g(ix.status) === 'done' ? 'done' : 'open', created_at: g(ix.created) });
  }
  return out;
}
const rowKey = (r) => [r.kind, String(r.title).trim().toLowerCase(), r.due_date || '', r.due_time || ''].join('|');
/** Сливает записи файла и записи приложения: порядок файла сохраняется, новые дописываются в конец, «выполнено» не теряется. */
function mergeCsv(existingText, items) {
  const base = parseCsv(existingText), seen = new Map();
  base.forEach(r => seen.set(rowKey(r), r));
  let added = 0;
  for (const it of items) {
    const k = rowKey({ kind: it.kind, title: it.title, due_date: it.due_date, due_time: it.due_time }), ex = seen.get(k);
    if (ex) { if (it.status === 'done') ex.status = 'done'; continue; }
    const row = { kind: it.kind, title: it.title, due_date: it.due_date || null, due_time: it.due_time || null, status: it.status || 'open', created_at: it.created_at || '' };
    base.push(row); seen.set(k, row); added++;
  }
  const q = (v) => { v = String(v ?? ''); return /[;"\n\r]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  const lines = [CSV_HEAD.join(';')];
  base.forEach((r, i) => lines.push([i + 1, r.kind, r.title, r.due_date || '', r.due_time || '', r.status, r.created_at].map(q).join(';')));
  return { text: '﻿' + lines.join('\r\n') + '\r\n', rows: base, added };
}
/** Записи, которые были в копии на устройстве, но пропали на сервере (например, сервер перезапустился). */
function missingOnServer(serverItems) {
  const have = new Set(serverItems.map(rowKey));
  return store.rows().filter(r => !have.has(rowKey(r)));
}
function addRows(rows) {   // добавить в локальное хранилище записи (из файла), которых там ещё нет
  const have = new Set(store.rows().map(rowKey)); let n = 0;
  for (const r of rows) { if (have.has(rowKey(r))) continue; const x = store.add(r.kind, r.title, r.due_date, r.due_time, r.created_at); if (r.status === 'done') store.update(x.id, { status: 'done' }); have.add(rowKey(r)); n++; }
  return n;
}

const Offline = {
  parseCsv, mergeCsv, missingOnServer, addRows,
  call, csv, handleText, parseCommand, makeNow, find, store, sess,
  /** Запомнить копию записей сервера (только если нет несинхронизированных изменений). */
  seed(items) {
    if (!Array.isArray(items) || store.journal().length) return;
    store.save(items.map(r => ({ id: r.id, kind: r.kind, title: r.title, due_date: r.due_date || null, due_time: r.due_time || null,
      status: r.status || 'open', created_at: r.created_at || '', user_id: '' })));
  },
  pendingChanges() { return store.journal().length; },
  /** Отправить изменения, сделанные без связи. rawFetch(path, init) → Response. Возвращает число отправленных изменений. */
  async sync(rawFetch) {
    const journal = store.journal();
    if (!journal.length) return 0;
    const rows = store.rows();
    const send = async (path, method, body) => {
      const r = await rawFetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined });
      if (r.status >= 500) throw new Error('server ' + r.status);
      return r;
    };
    let n = 0;
    for (const e of journal) {                      // правки и удаления записей, которые уже были на сервере
      if (e.id >= TEMP_BASE) continue;
      if (e.op === 'update') {
        const f = e.fields || {}, b = {};
        if ('title' in f) b.title = f.title;
        if ('kind' in f) b.kind = f.kind;
        if ('status' in f) b.status = f.status;
        if ('due_date' in f) b.date = f.due_date || '';
        if ('due_time' in f) b.time = f.due_time || '';
        await send(`/api/items/${e.id}`, 'PATCH', b); n++;
      } else if (e.op === 'delete') { await send(`/api/items/${e.id}`, 'DELETE'); n++; }
    }
    const fresh = rows.filter(r => r.id >= TEMP_BASE);   // записи, созданные без связи (в их текущем виде)
    if (fresh.length) {
      await send('/api/import', 'POST', { items: fresh.map(r => ({ kind: r.kind, title: r.title, date: r.due_date, time: r.due_time, status: r.status, created_at: r.created_at })) });
      n += fresh.length;
    }
    ls.set(LS_JOURNAL, []);
    return n;
  },
  _internals: { mkDate, iso, scanWhen, Text, conv, rx, TEMP_BASE },
};
global.Offline = Offline;
if (typeof module !== 'undefined' && module.exports) module.exports = Offline;
})(typeof window !== 'undefined' ? window : globalThis);
