/* Up Next — the personal watch queue.
 *
 * Two jobs:
 *   1. The status picker. One button on the anime page (.un-anime-btn) and,
 *      on /up-next, a small "⋯" injected into every queue card. Both open the
 *      same centered popover, sharing the visual chrome of list_picker.css.
 *   2. Web Push. Enabling notifications is offered at the moment the user
 *      actually sets a reminder (that is when the permission prompt makes
 *      sense), and the /up-next page has an explicit on/off card.
 *
 * Reminder kinds mirror the server (up_next.py):
 *   now | tonight | tomorrow | hours | date | after
 */
(function () {
    "use strict";

    var overlay = null;      // open popover element
    var currentSlug = null;
    var currentTitle = "";
    var currentState = null;
    var stateCache = {};     // slug -> last known state, so reopening is instant
    var REPO = null;         // service worker registration

    /* ---------------------------------------------------------------- utils */

    function el(tag, cls, html) {
        var n = document.createElement(tag);
        if (cls) n.className = cls;
        if (html != null) n.innerHTML = html;
        return n;
    }

    function toast(msg) {
        var t = el("div", "list-picker-toast");
        t.textContent = msg;
        document.body.appendChild(t);
        setTimeout(function () {
            t.classList.add("out");
            setTimeout(function () { t.remove(); }, 300);
        }, 2400);
    }

    function b64urlToBytes(b64) {
        var pad = "=".repeat((4 - (b64.length % 4)) % 4);
        var norm = (b64 + pad).replace(/-/g, "+").replace(/_/g, "/");
        var raw = atob(norm);
        var out = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
        return out;
    }

    function bytesToB64url(buf) {
        var bytes = new Uint8Array(buf);
        var s = "";
        for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
        return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }

    function todayISO() {
        var d = new Date();
        var m = String(d.getMonth() + 1).padStart(2, "0");
        var day = String(d.getDate()).padStart(2, "0");
        return d.getFullYear() + "-" + m + "-" + day;
    }

    function api(url, opts) {
        return fetch(url, opts).then(function (res) {
            if (res.status === 401) {
                window.location.href = "/login?next=" +
                    encodeURIComponent(window.location.pathname);
                throw new Error("Unauthorized");
            }
            return res.json();
        });
    }

    /* ------------------------------------------------------------- picker */

    function closePicker() {
        if (!overlay) return;
        overlay.remove();
        overlay = null;
        currentSlug = null;
        currentState = null;
        document.body.style.overflow = "";
        document.removeEventListener("keydown", onKey, true);
    }

    function onKey(e) {
        if (e.key === "Escape") closePicker();
    }

    /* The picker's whole vocabulary lives in one table, so the markup and the
     * in-place refresh below can never disagree about a row's label. Ordered
     * as it reads top-to-bottom: what I'm doing, then when to nudge me, then
     * the two "just file it" actions. */
    var ROWS = [
        { kind: "now", ico: "fa-play", title: "Currently watching",
          desc: "A live dot on every poster" },
        { kind: "tonight", ico: "fa-moon", title: "Ping me tonight",
          desc: "One nudge after 5pm IST" },
        { kind: "tomorrow", ico: "fa-sun", title: "Ping me tomorrow",
          desc: "One nudge tomorrow" },
        { kind: "hours", ico: "fa-clock", title: "Ping me in a few hours",
          desc: "Set a timer — 1 to 72 hours" },
        { kind: "date", ico: "fa-calendar-day", title: "Ping me on a set day",
          desc: "Pick any date" },
        { kind: "after", ico: "fa-hourglass-half", title: "After I finish another show",
          desc: "Unlocks the moment you finish it" },
        { kind: "none", ico: "fa-bookmark", title: "Park it on my backlog",
          desc: "No reminder" },
        { kind: "done", ico: "fa-flag-checkered", title: "Finished this one",
          desc: "Clears it, and frees whatever waited on it" }
    ];

    // The single source of truth for whether a row is "on", shared by the
    // initial paint and the later state refresh.
    function isOn(key, state) {
        var s = state || {};
        if (key === "now") return s.status === "now";
        if (key === "none") return s.status === "planned" && !s.remind_kind;
        if (key === "done") return s.status === "done";
        return s.remind_kind === key;
    }

    function rowDesc(row, state) {
        var s = state || {};
        if (s.remind_kind !== row.kind) return row.desc;
        if (row.kind === "date" && s.remind_on) return "Set for " + s.remind_on;
        // resolve() already phrases a running timer for us ("in about 3 hours").
        if (row.kind === "hours" && s.caption) return s.caption;
        if (row.kind === "after" && s.remind_after_slug) return "Waiting on another show";
        return row.desc;
    }

    function optionsHTML(state) {
        var s = state || {};
        var html = "";
        ROWS.forEach(function (row) {
            var on = isOn(row.kind, state);
            html +=
                '<button type="button" class="un-row' + (on ? " active" : "") +
                '" data-kind="' + row.kind + '">' +
                '  <span class="un-row-ico ico-' + row.kind + (on ? " on" : "") + '">' +
                '    <i class="fas ' + row.ico + '"></i></span>' +
                '  <span class="un-row-text">' +
                '    <span class="un-row-title">' + row.title + '</span>' +
                '    <span class="un-row-desc">' + rowDesc(row, state) + '</span>' +
                '  </span>' +
                (on ? '<i class="fas fa-check un-row-tick"></i>' : '') +
                '</button>';
            if (row.kind === "hours") {
                html +=
                    '<div class="un-inline" id="unHoursRow">' +
                    '  <input type="number" id="unHoursInput" min="1" max="72" step="1" value="3"' +
                    '         inputmode="numeric" aria-label="Hours from now">' +
                    '  <span class="un-inline-unit">hrs from now</span>' +
                    '  <button type="button" class="un-mini-btn" id="unHoursSet">Set</button>' +
                    '</div>';
            }
            if (row.kind === "date") {
                html +=
                    '<div class="un-inline" id="unDateRow">' +
                    '  <input type="date" id="unDateInput" min="' + todayISO() + '"' +
                    (s.remind_kind === "date" && s.remind_on ? ' value="' + s.remind_on + '"' : "") + '>' +
                    '  <button type="button" class="un-mini-btn" id="unDateSet">Set</button>' +
                    '</div>';
            }
            if (row.kind === "after") {
                html +=
                    '<div class="un-inline" id="unAfterRow">' +
                    '  <select id="unAfterInput"></select>' +
                    '  <button type="button" class="un-mini-btn" id="unAfterSet">Set</button>' +
                    '</div>';
            }
        });
        return html +
            '<button type="button" class="un-row un-danger-row" data-kind="remove">' +
            '  <span class="un-row-ico"><i class="fas fa-trash"></i></span>' +
            '  <span class="un-row-text">' +
            '    <span class="un-row-title">Take it off Up Next</span>' +
            '  </span>' +
            '</button>';
    }

    // Refresh an already-painted list in place rather than rebuilding it, so
    // the rows the user is reading never flicker under them.
    function applyState(pop, state) {
        currentState = state;
        var rows = pop.querySelector("#unRows");
        if (!rows) return;
        ROWS.forEach(function (row) {
            var btn = rows.querySelector('.un-row[data-kind="' + row.kind + '"]');
            if (!btn) return;
            var on = isOn(row.kind, state);
            btn.classList.toggle("active", on);
            var ico = btn.querySelector(".un-row-ico");
            if (ico) ico.classList.toggle("on", on);
            var desc = btn.querySelector(".un-row-desc");
            if (desc) desc.textContent = rowDesc(row, state);
            var tick = btn.querySelector(".un-row-tick");
            if (on && !tick) {
                btn.insertAdjacentHTML("beforeend", '<i class="fas fa-check un-row-tick"></i>');
            } else if (!on && tick) {
                tick.remove();
            }
        });
        var s = state || {};
        var hInput = pop.querySelector("#unHoursInput");
        if (hInput && s.remind_kind === "hours" && s.remind_at) {
            // Prefill the stepper with whatever is left of a running timer.
            var left = (new Date(s.remind_at).getTime() - Date.now()) / 3600000;
            hInput.value = Math.max(1, Math.min(72, Math.round(left) || 1));
        }
    }

    function openPicker(slug, title) {
        closePicker();
        currentSlug = slug;
        currentTitle = title || "";

        overlay = el("div", "list-picker-overlay");
        var pop = el("div", "list-picker-popover modal un-pop");
        pop.innerHTML =
            '<div class="list-picker-head">' +
            '  <div class="list-picker-title"><i class="fas fa-tv"></i> Up Next</div>' +
            '  <button type="button" class="list-picker-close" aria-label="Close"><span class="modal-x"></span></button>' +
            '</div>' +
            '<div class="list-picker-sub">' + escapeHtml(currentTitle) + '</div>' +
            '<div class="list-picker-rows un-rows" id="unRows"></div>';

        overlay.appendChild(pop);
        document.body.appendChild(overlay);
        document.body.style.overflow = "hidden";

        overlay.addEventListener("click", function (e) {
            if (e.target === overlay) closePicker();
        });
        pop.querySelector(".list-picker-close").addEventListener("click", closePicker);
        document.addEventListener("keydown", onKey, true);

        // Paint the rows before touching the network. This box used to sit on
        // "Loading…" until /api/up-next/state returned, which is exactly what
        // made it feel slow — the rows themselves are static, so only the tick
        // marks actually need the server. The last known state (if we have
        // one) is applied straight away and corrected when the response lands.
        currentState = stateCache[slug] || null;
        pop.querySelector("#unRows").innerHTML = optionsHTML(currentState);
        wireRows(pop);

        api("/api/up-next/state?slug=" + encodeURIComponent(slug))
            .then(function (data) {
                stateCache[slug] = data.state || null;
                applyState(pop, data.state || null);
            })
            .catch(function () {
                // The rows still work — say the ticks may be stale instead of
                // silently showing state we could not verify.
                if (!stateCache[slug]) toast("Couldn't load your queue — tap a choice to set it");
            });
    }

    function escapeHtml(s) {
        var d = document.createElement("div");
        d.textContent = s;
        return d.innerHTML;
    }

    function wireRows(pop) {
        var rows = pop.querySelector("#unRows");
        var dateRow = pop.querySelector("#unDateRow");
        var dateInput = pop.querySelector("#unDateInput");
        var hoursRow = pop.querySelector("#unHoursRow");
        var hoursInput = pop.querySelector("#unHoursInput");
        var afterRow = pop.querySelector("#unAfterRow");
        var afterInput = pop.querySelector("#unAfterInput");

        rows.querySelectorAll(".un-row[data-kind]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var kind = btn.dataset.kind;
                if (kind === "remove") {
                    removeEntry();
                    return;
                }
                if (kind === "date") {
                    toggleInline(dateRow, [hoursRow, afterRow]);
                    if (dateInput && !dateInput.value) dateInput.value = todayISO();
                    if (dateInput) dateInput.focus();
                    return;
                }
                if (kind === "hours") {
                    toggleInline(hoursRow, [dateRow, afterRow]);
                    if (hoursInput) { hoursInput.focus(); hoursInput.select(); }
                    return;
                }
                if (kind === "after") {
                    toggleInline(afterRow, [dateRow, hoursRow]);
                    loadAfterOptions(pop);
                    var sel = pop.querySelector("#unAfterInput");
                    if (sel && !sel.disabled) sel.focus();
                    return;
                }
                if (kind === "now") {
                    save({ status: "now", remind_kind: null }, "Watching now");
                } else if (kind === "done") {
                    save({ status: "done", remind_kind: null }, "Marked as finished");
                } else if (kind === "none") {
                    save({ status: "planned", remind_kind: null }, "Parked on your backlog");
                } else {
                    save({ status: "planned", remind_kind: kind }, "Reminder set");
                }
            });
        });

        var dateSet = pop.querySelector("#unDateSet");
        if (dateSet) dateSet.addEventListener("click", function () {
            var v = dateInput.value;
            if (!v) { toast("Pick a date first"); return; }
            save({ status: "planned", remind_kind: "date", remind_on: v }, "Reminder scheduled");
        });

        var hoursSet = pop.querySelector("#unHoursSet");
        if (hoursSet) hoursSet.addEventListener("click", function () {
            var n = parseInt(hoursInput && hoursInput.value, 10);
            if (!n || n < 1) { toast("Give it at least an hour"); return; }
            n = Math.min(n, 72);
            save({ status: "planned", remind_kind: "hours", remind_hours: n },
                 "Timer set — " + n + (n === 1 ? " hour" : " hours") + " from now");
        });

        var afterSet = pop.querySelector("#unAfterSet");
        if (afterSet) afterSet.addEventListener("click", function () {
            var v = afterInput.value;
            if (!v) { toast("Queue another show first"); return; }
            save({ status: "planned", remind_kind: "after", remind_after: v },
                 "Queued for after you finish that");
        });
    }

    // The "after I finish…" row is the only one that needs the user's queue, so
    // it is fetched on demand rather than on every picker open.
    function loadAfterOptions(pop) {
        var sel = pop.querySelector("#unAfterInput");
        if (!sel || sel.dataset.loaded === "1") return;
        sel.dataset.loaded = "1";
        api("/api/up-next/options?exclude=" + encodeURIComponent(currentSlug))
            .then(function (res) {
                var opts = res.options || [];
                if (!opts.length) {
                    sel.innerHTML = '<option value="">Queue another show first</option>';
                    sel.disabled = true;
                    var b = pop.querySelector("#unAfterSet");
                    if (b) b.disabled = true;
                    return;
                }
                sel.innerHTML = opts.map(function (o) {
                    var chosen = (currentState && currentState.remind_after_slug === o.slug)
                        ? " selected" : "";
                    return '<option value="' + o.slug + '"' + chosen + '>' +
                        escapeHtml(o.title) + '</option>';
                }).join("");
            })
            .catch(function () {
                sel.dataset.loaded = "0";   // let the next open retry
                sel.innerHTML = '<option value="">Couldn\'t load your queue</option>';
            });
    }

    function toggleInline(row, others) {
        if (!row) return;
        var willOpen = !row.classList.contains("open");
        others.forEach(function (o) { if (o) o.classList.remove("open"); });
        row.classList.toggle("open", willOpen);
    }

    function save(payload, okMsg) {
        payload.slug = currentSlug;
        api("/api/up-next/set", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        }).then(function (res) {
            if (!res.success) {
                toast("Couldn't save that. Try again.");
                return;
            }
            currentState = res.state || null;
            closePicker();
            toast(okMsg);
            refreshTrigger();
            if (window.__UN_QUEUE_PAGE) {
                // Shelves are grouped server-side, so a change has to move the
                // card between them — one reload is the honest way to do that.
                setTimeout(function () { window.location.reload(); }, 550);
            } else if (payload.remind_kind) {
                // The user just said "remind me" — this is the moment the
                // permission prompt actually makes sense.
                maybeOfferPush();
            }
        }).catch(function () {});
    }

    function removeEntry() {
        api("/api/up-next/" + encodeURIComponent(currentSlug), { method: "DELETE" })
            .then(function () {
                closePicker();
                toast("Removed from Up Next");
                refreshTrigger();
                if (window.__UN_QUEUE_PAGE) {
                    setTimeout(function () { window.location.reload(); }, 550);
                }
            })
            .catch(function () {});
    }

    /* Repaint the anime page's button from currentState (no reload needed). */
    function refreshTrigger() {
        var btn = document.getElementById("unAnimeBtn");
        if (!btn) return;
        var label = btn.querySelector(".un-anime-label");
        var icon = btn.querySelector("i");
        var dot = btn.querySelector(".un-live-dot");
        btn.classList.remove("un-set", "un-now", "un-planned", "un-ready", "un-done");
        if (!currentState) {
            if (label) label.textContent = btn.dataset.offLabel || "Up Next";
            if (icon) icon.className = "fas fa-bell";
            if (dot) dot.remove();
            return;
        }
        var pill = currentState.pill;
        btn.classList.add("un-set", pill.cls);
        if (label) label.textContent = pill.label;
        if (icon) icon.className = "fas " + pill.icon;
        if (pill.cls === "un-now" && !dot) {
            var d = el("span", "un-live-dot");
            btn.insertBefore(d, btn.firstChild);
        } else if (pill.cls !== "un-now" && dot) {
            dot.remove();
        }
    }

    /* ---------------------------------------------------------- web push */

    function pushSupported() {
        return ("serviceWorker" in navigator) &&
               ("PushManager" in window) &&
               ("Notification" in window);
    }

    function ready() {
        if (REPO) return Promise.resolve(REPO);
        return navigator.serviceWorker.ready.then(function (reg) {
            REPO = reg;
            return reg;
        });
    }

    function setNote(msg) {
        var n = document.getElementById("unPushNote");
        if (!n) return;
        n.textContent = msg;
        n.hidden = !msg;
    }

    function updatePushCard(devices) {
        var p = document.getElementById("unPushText");
        if (!p) return;
        p.textContent = devices
            ? "On for " + devices + " device" + (devices === 1 ? "" : "s") +
              ". Reminders fire even when Otakul is closed."
            : "Turn these on and Up Next pings you when a reminder comes due — even with the site closed.";
    }

    function enablePush() {
        if (!pushSupported()) {
            setNote("This browser can't do web push. On iPhone, add Otakul to your " +
                    "Home Screen first, then enable notifications.");
            return Promise.resolve(false);
        }
        if (!window.isSecureContext) {
            setNote("Notifications need HTTPS.");
            return Promise.resolve(false);
        }
        return Notification.requestPermission().then(function (perm) {
            if (perm !== "granted") {
                setNote(perm === "denied"
                    ? "Notifications are blocked for this site in your browser settings."
                    : "Notifications were not allowed.");
                return false;
            }
            return ready()
                .then(function (reg) {
                    return fetch("/api/up-next/push/key").then(function (r) { return r.json(); });
                })
                .then(function (keyData) {
                    if (!keyData.success) throw new Error("no-key");
                    return ready().then(function (reg) {
                        return reg.pushManager.getSubscription().then(function (existing) {
                            if (existing) return existing;
                            return reg.pushManager.subscribe({
                                userVisibleOnly: true,
                                applicationServerKey: b64urlToBytes(keyData.key)
                            });
                        });
                    });
                })
                .then(function (sub) {
                    return api("/api/up-next/push/subscribe", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            endpoint: sub.endpoint,
                            keys: {
                                p256dh: bytesToB64url(sub.getKey("p256dh")),
                                auth: bytesToB64url(sub.getKey("auth"))
                            }
                        })
                    });
                })
                .then(function () {
                    return api("/api/up-next/push/status");
                })
                .then(function (st) {
                    updatePushCard(st.devices || 1);
                    setNote("Notifications on for this device.");
                    return true;
                })
                .catch(function (err) {
                    setNote(err && err.message === "no-key"
                        ? "Push is not available on the server yet."
                        : "Couldn't turn on notifications. Try again.");
                    return false;
                });
        });
    }

    function disablePush() {
        if (!pushSupported()) return Promise.resolve();
        return ready()
            .then(function (reg) { return reg.pushManager.getSubscription(); })
            .then(function (sub) {
                if (!sub) return null;
                var endpoint = sub.endpoint;
                return sub.unsubscribe().then(function () {
                    return api("/api/up-next/push/unsubscribe", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ endpoint: endpoint })
                    });
                });
            })
            .then(function () { return api("/api/up-next/push/status"); })
            .then(function (st) {
                updatePushCard(st.devices || 0);
                setNote(st.devices ? "Turned off on this device." : "Reminders are off.");
            })
            .catch(function () {});
    }

    /* After setting a reminder, nudge once — never nag. */
    var offered = false;
    function maybeOfferPush() {
        if (offered || !pushSupported()) return;
        if (Notification.permission !== "default") return;
        offered = true;
        var t = el("div", "list-picker-toast");
        t.innerHTML = 'Want a push when this is due? ' +
            '<a href="#" id="unWantPush" style="color:#c9a8ff;font-weight:800;">Turn on</a>' +
            ' <span style="opacity:.6">·</span> ' +
            '<a href="#" id="unNoPush" style="color:#8f94b0;">Not now</a>';
        document.body.appendChild(t);
        function kill() {
            t.classList.add("out");
            setTimeout(function () { t.remove(); }, 300);
        }
        t.querySelector("#unWantPush").addEventListener("click", function (e) {
            e.preventDefault(); kill(); enablePush();
        });
        t.querySelector("#unNoPush").addEventListener("click", function (e) {
            e.preventDefault(); kill();
        });
        setTimeout(kill, 9000);
    }

    /* --------------------------------------------------------------- wire */

    function wireAnimeButton() {
        var btn = document.getElementById("unAnimeBtn");
        if (!btn || btn.dataset.wired) return;
        btn.dataset.wired = "1";
        btn.dataset.offLabel = btn.querySelector(".un-anime-label")
            ? btn.querySelector(".un-anime-label").textContent : "Up Next";
        btn.addEventListener("click", function (e) {
            e.preventDefault();
            openPicker(btn.dataset.slug, btn.dataset.title);
        });
    }

    /* On /up-next every queue card gets a "⋯" so the queue is manageable
     * without opening each show. */
    function wireQueueCards() {
        document.querySelectorAll(".anime-card").forEach(function (card) {
            if (card.dataset.unWired) return;
            var actions = card.querySelector(".card-actions");
            if (!actions) return;
            card.dataset.unWired = "1";
            var slug = card.dataset.slug;
            var title = card.dataset.title || "";
            if (!slug) return;
            var b = el("button", "un-card-btn",
                '<i class="fas fa-sliders-h"></i>');
            b.type = "button";
            b.title = "Change Up Next";
            b.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation();
                openPicker(slug, title);
            });
            actions.insertBefore(b, actions.firstChild);
        });
    }

    function wirePushCard() {
        var card = document.getElementById("unPushCard");
        if (!card || card.dataset.wired) return;
        card.dataset.wired = "1";
        if (card.dataset.available !== "1") {
            setNote("Push notifications are not available on the server yet.");
            var en = document.getElementById("unEnablePush");
            if (en) en.disabled = true;
        }
        var on = document.getElementById("unEnablePush");
        var off = document.getElementById("unDisablePush");
        if (on) on.addEventListener("click", function () { enablePush(); });
        if (off) off.addEventListener("click", function () { disablePush(); });
        // An iPhone only exposes PushManager once installed to the Home
        // Screen — say so instead of silently doing nothing.
        if (!pushSupported() && /iPhone|iPad|iPod/.test(navigator.userAgent)) {
            setNote("On iPhone, add Otakul to your Home Screen to get reminders.");
        }
    }

    // The "while you were away" banner. Dismissal is per-session on purpose:
    // those reminders really are still waiting, so hiding it permanently would
    // be a lie. Coming back tomorrow should mention them again.
    function wireAwayBanner() {
        var banner = document.getElementById("unAway");
        if (!banner || banner.dataset.wired) return;
        banner.dataset.wired = "1";
        var key = "unAwayDismissed";
        try {
            if (sessionStorage.getItem(key)) { banner.remove(); return; }
        } catch (err) { /* private mode: just leave the banner up */ }
        var x = document.getElementById("unAwayX");
        if (x) x.addEventListener("click", function () {
            try { sessionStorage.setItem(key, "1"); } catch (err) { /* ignore */ }
            banner.remove();
        });
    }

    function wire() {
        wireAnimeButton();
        wireQueueCards();
        wirePushCard();
        wireAwayBanner();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wire);
    } else {
        wire();
    }
    if (window.MutationObserver) {
        new MutationObserver(wire).observe(document.body, { childList: true, subtree: true });
    }
})();
