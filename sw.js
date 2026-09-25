/* Тугай — service worker
   1) Офлайн: приложение открывается без сети, данные берутся из последней сохранённой копии.
   2) Push-уведомления (в том числе на iPhone с iOS 16.4+, если сайт добавлен на экран «Домой»). */
const VERSION = 'tugai-v6';
const SHELL = `${VERSION}-shell`;
const API = 'tugai-api';        // без версии: данные переживают обновление приложения
const IMG = 'tugai-img';
const SHELL_FILES = ['/', '/manifest.json', '/icons/icon-192.png', '/icons/apple-touch-icon.png'];
const NET_TIMEOUT = 4500;        // медленный Wi-Fi в баре: через 4.5 с показываем сохранённое

self.addEventListener('install', e => {
  e.waitUntil(caches.open(SHELL).then(c => Promise.all(SHELL_FILES.map(u => c.add(new Request(u, {cache: 'reload'})).catch(() => {}))))
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys
    .filter(k => k.startsWith('tugai-v') && k !== SHELL).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

function timeout(ms) { return new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms)); }

async function networkFirst(req, cacheName, ms) {
  const cache = await caches.open(cacheName);
  try {
    const res = await Promise.race([fetch(req), timeout(ms)]);
    if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
    return res;
  } catch (err) {
    const hit = await cache.match(req, {ignoreVary: true});
    if (hit) {
      // Помечаем ответ как «из офлайн-копии», чтобы приложение показало это пользователю.
      const h = new Headers(hit.headers); h.set('X-Tugai-Offline', '1');
      return new Response(await hit.blob(), {status: hit.status, statusText: hit.statusText, headers: h});
    }
    throw err;
  }
}

async function cacheFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  if (hit) return hit;
  const res = await fetch(req);
  if (res && res.ok) {
    cache.put(req, res.clone()).catch(() => {});
    trim(cacheName, 400);
  }
  return res;
}

async function staleWhileRevalidate(req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const net = fetch(req).then(res => { if (res && (res.ok || res.type === 'opaque')) cache.put(req, res.clone()).catch(() => {}); return res; }).catch(() => hit);
  return hit || net;
}

async function trim(cacheName, max) {
  const cache = await caches.open(cacheName);
  const keys = await cache.keys();
  for (let i = 0; i < keys.length - max; i++) await cache.delete(keys[i]);
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;                               // запись идёт мимо кэша (очередь — в приложении)
  const url = new URL(req.url);

  if (url.origin !== location.origin) {                           // шрифты и т.п.
    if (/fonts\.(googleapis|gstatic)\.com$/.test(url.hostname)) e.respondWith(staleWhileRevalidate(req, SHELL));
    return;
  }
  if (req.mode === 'navigate' || url.pathname === '/') {
    e.respondWith(networkFirst(new Request('/'), SHELL, NET_TIMEOUT).catch(() => caches.match('/')));
    return;
  }
  if (url.pathname.startsWith('/api/archives/') && url.pathname.endsWith('/download')) return;   // ZIP не кэшируем
  if (url.pathname.startsWith('/api/logs')) return;                // журнал — только свежие данные, выгрузки не кэшируем
  if (/^\/api\/.*\/file$/.test(url.pathname)) { e.respondWith(cacheFirst(req, IMG)); return; }  // фото не меняются
  if (url.pathname.startsWith('/api/')) {
    e.respondWith(networkFirst(req, API, NET_TIMEOUT).catch(() =>
      new Response(JSON.stringify({detail: 'Нет соединения — эти данные ещё не сохранены на телефоне'}),
        {status: 503, headers: {'Content-Type': 'application/json', 'X-Tugai-Offline': '1'}})));
    return;
  }
  e.respondWith(staleWhileRevalidate(req, SHELL));
});

self.addEventListener('message', e => {
  if (e.data && e.data.type === 'clear-data') {
    e.waitUntil(Promise.all([caches.delete(API), caches.delete(IMG)]));
  }
});

/* ---------- PUSH ---------- */
self.addEventListener('push', e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (err) { d = {body: e.data ? e.data.text() : ''}; }
  const title = d.title || 'Тугай';
  const opts = {
    body: d.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    data: {url: d.url || '/'},
  };
  // iOS требует показывать уведомление на КАЖДЫЙ пуш — иначе отзывает подписку.
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const target = new URL((e.notification.data && e.notification.data.url) || '/', self.location.origin).href;
  e.waitUntil((async () => {
    const list = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const c of list) {
      if (new URL(c.url).origin === self.location.origin) {
        c.postMessage({type: 'open', url: target});
        return c.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});
