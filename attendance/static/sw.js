// Aura service worker: makes the kiosk and staff screens installable and lets them open without a
// network hiccup killing the screen. Pages are network-first (a UI update still shows on the next
// load, as with the no-cache headers); static files are served from cache and refreshed behind.
// The API, media and camera/voice traffic always go straight to the server and are never cached.
const CACHE = 'aura-v1';
const PRECACHE = ['/static/icons/icon-180.png', '/static/icons/icon-192.png', '/static/icons/icon-512.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener('fetch', e => {
  const req = e.request;
  const url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;

  if (req.mode === 'navigate') {
    // one cached copy per page, whatever its ?kiosk=… query, so an offline reopen still finds it
    const key = url.origin + url.pathname;
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key).then(hit => hit || offline())));
    return;
  }

  if (url.pathname.startsWith('/static/')) {
    e.respondWith(caches.open(CACHE).then(c => c.match(req).then(hit => {
      const fresh = fetch(req).then(res => { if (res.ok) c.put(req, res.clone()); return res; });
      if (hit) { e.waitUntil(fresh.catch(() => {})); return hit; }
      return fresh;
    })));
  }
});

function offline() {
  return new Response(
    '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1">' +
    '<title>Aura · offline</title><body style="margin:0;height:100vh;display:grid;place-items:center;' +
    'background:#031a2b;color:#e6f0f7;font:18px system-ui,sans-serif;text-align:center">' +
    '<div><p style="font-size:1.4em;margin:0 0 .5em">Can’t reach the Aura server</p>' +
    '<p style="opacity:.7;margin:0">Retrying…</p></div>' +
    '<script>setTimeout(() => location.reload(), 5000)</script>',
    { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } });
}
