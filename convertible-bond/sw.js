const CACHE_NAME = 'cb-monthly-v4';
const BASE = new URL('./', self.location.href);
const APP_SHELL = ['index.html', 'style.css', 'app.js', 'manifest.webmanifest', 'icon.svg',
  'apple-touch-icon.png', 'icon-192.png', 'icon-512.png',
  'data/strategy.json', 'data/performance.json', 'data/fit_curve.json'];
const SAFE = new Set(APP_SHELL.map(path => new URL(path, BASE).href));
SAFE.add(BASE.href);

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith('cb-monthly-') && key !== CACHE_NAME) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  url.search = '';
  if (request.method !== 'GET' || !SAFE.has(url.href)) return;
  const isData = url.pathname.endsWith('.json');
  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    try {
      const response = await fetch(request);
      if (!response.ok || response.redirected) throw new Error('Snapshot unavailable');
      if (isData) await response.clone().json();
      await cache.put(url.href, response.clone());
      return response;
    } catch {
      const saved = await cache.match(url.href) || (url.href === BASE.href
        ? await cache.match(new URL('index.html', BASE).href) : null);
      if (saved) {
        const headers = new Headers(saved.headers);
        if (isData) headers.set('X-CB-Offline', '1');
        return new Response(await saved.blob(), { status: 200, headers });
      }
      return new Response('请联网完成首次加载。', { status: 503,
        headers: { 'Content-Type': 'text/plain;charset=utf-8' } });
    }
  })());
});
