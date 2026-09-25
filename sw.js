// Тугай ТТК — service worker: офлайн-доступ к оболочке приложения и последним данным API.
const CACHE_NAME = 'tugai-v1';
const SHELL = ['/', '/manifest.json', '/logo.png'];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL).catch(() => {}))
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k))
    )).then(() => self.clients.claim())
  );
});

// Сеть -> кэш (свежие данные когда есть связь, последний известный ответ — когда нет)
async function networkFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const fresh = await fetch(request);
    if (fresh && fresh.ok) cache.put(request, fresh.clone());
    return fresh;
  } catch (err) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw err;
  }
}

// Кэш -> сеть (для статики/оболочки: мгновенная загрузка, обновление в фоне)
async function cacheFirst(request) {
  const cache = await caches.open(CACHE_NAME);
  const cached = await cache.match(request);
  const network = fetch(request).then((res) => {
    if (res && res.ok) cache.put(request, res.clone());
    return res;
  }).catch(() => null);
  return cached || network || caches.match('/');
}

// Push-уведомления
self.addEventListener('push', (event) => {
  let data = { title: 'Тугай ТТК', body: 'У вас новое уведомление', url: '/' };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (err) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/logo.png',
      badge: '/logo.png',
      data: { url: data.url || '/' },
      vibrate: [80, 40, 80],
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ('focus' in client) {
          client.navigate(url).catch(() => {});
          return client.focus();
        }
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Мутации (POST/PUT/DELETE) не перехватываем — их офлайн-очередь ведёт сама страница.
  if (req.method !== 'GET') return;
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(req));
    return;
  }

  if (url.pathname === '/' || url.pathname === '/manifest.json' || url.pathname === '/logo.png' || url.pathname === '/sw.js') {
    event.respondWith(cacheFirst(req));
  }
});
