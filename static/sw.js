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
var CACHE = "otakul-v5";

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

/* ------------------------------------------------------------
 * Up Next reminders
 *
 * The server pushes a JSON payload when a reminder comes due
 * (see up_next.py). Show it, and open the anime page (or the queue)
 * when the user taps it.
 * ------------------------------------------------------------ */
self.addEventListener("push", function (e) {
    var data = { title: "Up Next", body: "A reminder is due.", url: "/up-next" };
    try {
        if (e.data) data = Object.assign(data, e.data.json());
    } catch (err) {
        try { data.body = e.data.text(); } catch (err2) { /* keep defaults */ }
    }
    e.waitUntil(
        self.registration.showNotification(data.title || "Up Next", {
            body: data.body || "",
            icon: data.icon || "/static/icons/icon-192.png",
            badge: data.badge || "/static/icons/icon-192.png",
            // Same tag = a newer reminder replaces the older one for that
            // show instead of stacking duplicate notifications.
            tag: data.tag || "up-next",
            renotify: true,
            data: { url: data.url || "/up-next" }
        })
    );
});

self.addEventListener("notificationclick", function (e) {
    e.notification.close();
    var url = (e.notification.data && e.notification.data.url) || "/up-next";
    e.waitUntil(
        self.clients.matchAll({ type: "window", includeUncontrolled: true })
            .then(function (wins) {
                // Reuse an open tab when there is one — focusing beats
                // spawning yet another Otakul window.
                for (var i = 0; i < wins.length; i++) {
                    var w = wins[i];
                    // w.url is not guaranteed to be set (or parseable) on
                    // every client, so never let that throw the whole
                    // click handler away.
                    var sameOrigin = false;
                    try { sameOrigin = new URL(w.url).origin === self.location.origin; }
                    catch (err) { sameOrigin = false; }
                    if ("focus" in w && sameOrigin) {
                        if ("navigate" in w) { try { w.navigate(url); } catch (err2) {} }
                        return w.focus();
                    }
                }
                if (self.clients.openWindow) return self.clients.openWindow(url);
            })
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