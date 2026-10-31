/* Service worker: хранит страницу и offline.js в кэше, чтобы приложение открывалось и работало без связи с сервером. */
const CACHE = 'assistant-shell-v1';
const SHELL = ['/', '/offline.js'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE)
    .then((c) => Promise.all(SHELL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {}))))
    .then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin || !SHELL.includes(url.pathname)) return;   // API и остальное — напрямую в сеть
  e.respondWith(shell(req));
});

// Сеть в приоритете (чтобы обновления доходили), но если сервер молчит дольше 3 секунд или недоступен — берём копию из кэша.
async function shell(req) {
  const cache = await caches.open(CACHE);
  const hit = await cache.match(req, { ignoreSearch: true });
  const net = fetch(req).then((r) => {
    if (r && r.ok) cache.put(req, r.clone());
    if (!r.ok && hit) return hit;
    return r;
  });
  if (!hit) return net;
  const timeout = new Promise((resolve) => setTimeout(() => resolve(hit), 3000));
  return Promise.race([net.catch(() => hit), timeout]);
}
