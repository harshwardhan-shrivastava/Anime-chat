/* ============================================================
 * Otakul service worker
 *
 * Goals:
 *  - Make the site installable (manifest + fetch handler) so the
 *    Android APK / "Add to home screen" works and looks native.
 *  - Make it FAST: posters/thumbnails (AniList/TVmaze/Kitsu) and
 *    same-origin css/js are served from cache after the first visit.
 *  - NEVER cache chat / threads / API polling — those must stay live.
 *
 * Bump CACHE when this file changes on purpose.
 * ============================================================ */
var CACHE = "otakul-v1";

self.addEventListener("install", function (e) {
    e.waitUntil(
        caches.open(CACHE)
            .then(function (c) {
                return c.addAll([
                    "/manifest.webmanifest",
                    "/static/icons/icon-192.png",
                    "/static/icons/icon-512.png",
                    "/static/icons/maskable-192.png",
                    "/static/icons/maskable-512.png"
                ]);
            })
            .then(function () { return self.skipWaiting(); })
    );
});

self.addEventListener("activate", function (e) {
    e.waitUntil(
        caches.keys().then(function (keys) {
            return Promise.all(
                keys.filter(function (k) { return k !== CACHE; })
                    .map(function (k) { return caches.delete(k); })
            );
        }).then(function () { return self.clients.claim(); })
    );
});

self.addEventListener("fetch", function (e) {
    var req = e.request;
    if (req.method !== "GET") return;
    var url = new URL(req.url);

    // Live data — pages, chat, threads, APIs: always hit the network.
    // Navigation falls back to cache only when offline.
    if (req.mode === "navigate") {
        e.respondWith(
            fetch(req)
                .then(function (res) {
                    var copy = res.clone();
                    caches.open(CACHE).then(function (c) { c.put(req, copy); });
                    return res;
                })
                .catch(function () { return caches.match(req); })
        );
        return;
    }

    // Chat / threads / API endpoints must never be cached (polling would
    // go stale and look broken).
    if (url.pathname.indexOf("/community/") === 0 ||
        url.pathname.indexOf("/threads/") === 0 ||
        url.pathname.indexOf("/api/") === 0) {
        return;
    }

    // Everything else (css, js, same-origin images, and the cross-origin
    // poster/thumbnail CDNs): cache first, fill on miss.
    e.respondWith(
        caches.match(req).then(function (hit) {
            if (hit) return hit;
            return fetch(req).then(function (res) {
                if (res.ok || res.type === "opaque") {
                    var copy = res.clone();
                    caches.open(CACHE).then(function (c) { c.put(req, copy); });
                }
                return res;
            }).catch(function () {
                return caches.match(req);
            });
        })
    );
});