// ============================================================
// THREADS — mobile gesture guard (loaded after threads.js)
//
// The drawer-swipe logic in threads.js turns a horizontal-ish
// drag into a sheet open/close. That is correct for a real
// swipe, but once a drag starts it never gives up, and a fast
// vertical scroll through a guild/channel/message list can curve
// far enough sideways to flip into a drawer drag — the list
// stutters, the sheets ride along, everything feels rubber-bandy.
//
// This file listens in the CAPTURE phase (so it runs before the
// threads.js bubble listeners) and classifies each gesture once:
// the moment a finger commits to vertical scrolling, it stops the
// touchmove events from reaching the drawer handler and undoes any
// drag state it already started. Native scrolling is untouched
// (no preventDefault), so lists scroll exactly as the browser
// intends. Pure horizontal swipes are left completely alone.
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

    var active = null; // { id, x0, y0, vertical }

    function mobile() {
        return window.innerWidth <= 700;
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

    document.addEventListener("touchstart", function (e) {
        if (!mobile() || !e.touches.length || !appEl.contains(e.target)) return;
        var t = e.touches[0];
        active = { id: t.identifier, x0: t.clientX, y0: t.clientY, vertical: false };
    }, { capture: true, passive: true });

    document.addEventListener("touchmove", function (e) {
        if (!active || !mobile()) return;
        var t = touchById(e.touches, active.id);
        if (!t) return;
        var dx = t.clientX - active.x0;
        var dy = t.clientY - active.y0;
        if (!active.vertical) {
            // Commit to "this is a scroll" once vertical clearly leads.
            // The extra 1.4 margin means a slightly diagonal swipe that
            // started vertically stays a scroll instead of flipping into
            // a drawer drag on a late sideways flick.
            if (Math.abs(dy) > 12 && Math.abs(dy) > Math.abs(dx) * 1.4) {
                active.vertical = true;
                killDrag(); // undo a drag the drawer logic may have started
            }
        }
        if (active.vertical) {
            // Keep the drawer handler from ever seeing this gesture.
            e.stopPropagation();
        }
    }, { capture: true, passive: false });

    function endTouch(e) {
        if (!active) return;
        // If the app ended up mid-drag on a gesture we classified as a
        // scroll (edge cases), clean up so nothing stays shifted.
        if (active.vertical) killDrag();
        active = null;
    }
    document.addEventListener("touchend", endTouch, { capture: true, passive: true });
    document.addEventListener("touchcancel", endTouch, { capture: true, passive: true });
})();
