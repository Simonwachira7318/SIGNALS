// Wi-Fi Sense service worker: makes the dashboard installable on a phone ("Add to Home screen").
// Pages are network-first with a cached fallback; live data (/api/) is never cached.
const CACHE = "wifi-sense-v1";
const SHELL = ["/", "/history", "/devices", "/health", "/floorplan", "/rules", "/tuning", "/manifest.json", "/icon.svg"];

self.addEventListener("install", ev => {
  ev.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener("activate", ev => {
  ev.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener("fetch", ev => {
  const url = new URL(ev.request.url);
  if (ev.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  ev.respondWith(
    fetch(ev.request).then(resp => {
      if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then(c => c.put(ev.request, copy)); }
      return resp;
    }).catch(() => caches.match(ev.request))
  );
});
