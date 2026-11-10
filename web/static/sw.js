// App shell cache: the panel opens instantly and shows "no contact" if the device is off.
// API and WebSocket traffic is never cached.
const CACHE = "sms200-v17";
const SHELL = ["/", "index.html", "app.css", "app.js", "manifest.webmanifest", "fonts/jost-wght.woff2", "fonts/cormorant-wght.woff2", "fonts/cormorant-wght-italic.woff2",
  "icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(
  caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (u.origin !== location.origin || u.pathname.startsWith("/api/") || e.request.method !== "GET") return;
  // network first (always fresh when the device answers), cache as fallback
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("/"))));
});
