// ============================================================
// THREADS — mobile gesture guard + swipe-to-reply (after threads.js)
//
// Two jobs:
//
// 1. The drawer-swipe logic in threads.js turns a horizontal-ish drag
//    into a sheet open/close. That is correct for a real swipe, but once
//    a drag starts it never gives up, and a fast vertical scroll through
//    a guild/channel/message list can curve far enough sideways to flip
//    into a drawer drag — the list stutters, the sheets ride along,
//    everything feels rubber-bandy.
//
//    This file listens in the CAPTURE phase (before threads.js bubble
//    listeners) and classifies each gesture once: the moment a finger
//    commits to vertical scrolling it stops the touchmove events from
//    reaching the drawer handler and undoes any drag state it started.
//
// 2. Swipe RIGHT on a message = reply (Discord-mobile behaviour). The
//    row rides along with the finger with a reply tint; releasing past
//    the threshold fires the message's own Reply button, which runs the
//    exact same code as tapping it. The drawer only opens when the swipe
//    does NOT start on a message (or when the sheet is already open), so
//    the two gestures never fight.
// ============================================================
(function () {
    "use strict";

    var appEl = document.getElementById("threads-app");
    if (!appEl) return;

    // The same elements the drawer logic finger-tracks.
    var DRAW_PARTS = [
        ".thr-left",
        "#commRail",
        "#channelPanel",
        "#discoverPanel",
        ".thr-main",
    ];

    var active = null; // { id, x0, y0, mode, row }

    function mobile() {
        return window.innerWidth <= 700;
    }

    function chatOpen() {
        return appEl.classList.contains("thr-dm-open") ||
               appEl.classList.contains("thr-guild-open");
    }

    function sheetOpen() {
        return appEl.classList.contains("thr-sheet-open");
    }

    function convViewVisible() {
        var view = document.getElementById("convView");
        return !!(view && !view.classList.contains("hidden"));
    }

    function killDrag() {
        if (!appEl.classList.contains("thr-drag")) return;
        appEl.classList.remove("thr-drag");
        for (var i = 0; i < DRAW_PARTS.length; i++) {
            var el = document.querySelector(DRAW_PARTS[i]);
            if (el) el.style.transform = "";
        }
    }

    function touchById(list, id) {
        for (var i = 0; i < list.length; i++) {
            if (list[i].identifier === id) return list[i];
        }
        return null;
    }

    // Can this touch become a swipe-to-reply? Only on a message row, with
    // a conversation open and the drawer closed, and not on an interactive
    // part of the row (links, avatars, buttons, jump refs, the action bar).
    function replyRowFrom(e) {
        if (!chatOpen() || sheetOpen() || !convViewVisible()) return null;
        var el = e.target;
        if (!el || !el.closest) return null;
        var row = el.closest(".thr-msg");
        if (!row) return null;
        if (el.closest(".thr-msg-actions, a, button, [data-act], [data-jump], .thr-profile-open, textarea, input")) return null;
        return row;
    }

    function resetReplyRow(row, animate) {
        if (!row) return;
        row.classList.remove("thr-swipe-reply");
        if (animate) {
            row.style.transition = "transform .16s ease, background .16s ease";
            row.style.transform = "";
            setTimeout(function () {
                if (row && row.style) row.style.transition = "";
            }, 200);
        } else {
            row.style.transition = "";
            row.style.transform = "";
        }
    }

    document.addEventListener("touchstart", function (e) {
        if (!mobile() || !e.touches.length || !appEl.contains(e.target)) {
            active = null;
            return;
        }
        var t = e.touches[0];
        var row = replyRowFrom(e);
        active = { id: t.identifier, x0: t.clientX, y0: t.clientY, mode: row ? "msg" : null, row: row, t0: Date.now() };
    }, { capture: true, passive: true });

    document.addEventListener("touchmove", function (e) {
        if (!active || !mobile()) return;
        var t = touchById(e.touches, active.id);
        if (!t) return;
        var dx = t.clientX - active.x0;
        var dy = t.clientY - active.y0;

        if (!active.mode || active.mode === "msg") {
            if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx) * 1.4) {
                // Committed to a vertical scroll — keep it a scroll and
                // undo any drag the drawer logic may have started.
                active.mode = "scroll";
                killDrag();
            } else if (active.row && dx > 10 && dx > Math.abs(dy) * 1.2) {
                // (active.row is only set in "msg" mode, so this is the
                // row case) — right swipe on a message = reply.
                active.mode = "reply";
            } else if (active.row && dx < -10 && -dx > Math.abs(dy) * 1.2) {
                // Left swipe on a message: not a reply — swallow it so it
                // can't be mistaken for drawer motion.
                active.mode = "noop";
            }
        }

        if (active.mode === "scroll") {
            // Keep the drawer handler from ever seeing this gesture.
            e.stopPropagation();
        } else if (active.mode === "reply") {
            e.preventDefault();
            e.stopPropagation();
            var d = Math.min(Math.max(dx, 0), 110);
            active.row.style.transition = "none";
            active.row.style.transform = "translateX(" + d + "px)";
            active.row.classList.add("thr-swipe-reply");
        } else if (active.mode === "noop") {
            e.stopPropagation();
        }
    }, { capture: true, passive: false });

    function endTouch(e) {
        if (!active) return;
        var a = active;
        active = null;

        if (a.mode === "reply" && a.row) {
            var dx = 0;
            var dt = 0;
            if (e.changedTouches && e.changedTouches.length) {
                dx = e.changedTouches[0].clientX - a.x0;
                dt = Date.now() - a.t0;
            }
            var fired = (dx >= 62) || (dx >= 28 && dt <= 300);
            if (fired) {
                resetReplyRow(a.row, true);
                var btn = a.row.querySelector('.thr-msg-actions [data-act="reply"]');
                if (btn) btn.click();
            } else {
                resetReplyRow(a.row, true);
            }
            return;
        }
        if (a.mode === "noop" && a.row) {
            resetReplyRow(a.row, false);
            return;
        }
        // If the app ended up mid-drag on a gesture we classified as a
        // scroll (edge cases), clean up so nothing stays shifted.
        if (a.mode === "scroll") killDrag();
    }
    document.addEventListener("touchend", endTouch, { capture: true, passive: true });
    document.addEventListener("touchcancel", function (e) {
        if (!active) return;
        var a = active;
        active = null;
        if (a.row) resetReplyRow(a.row, a.mode === "reply");
        if (a.mode === "scroll") killDrag();
    }, { capture: true, passive: true });
})();
