/* ============================================================
 * THREADS — message action bar on touch (loaded after threads.js)
 *
 * The reply/pin/edit/delete bar only appeared on :hover, which touch
 * devices don't have — tapping a message either did nothing or needed
 * two taps plus luck. Here a plain tap on a message toggles its action
 * bar open (Discord-mobile style); the buttons inside stay one tap
 * away. Taps on links, avatars, jump-to-reply refs and the action
 * buttons themselves are left to their normal handlers.
 * ============================================================ */
(function () {
    "use strict";

    var list = document.getElementById("msgList");
    if (!list) return;

    list.addEventListener("click", function (e) {
        var act = e.target.closest ? e.target.closest("[data-act]") : null;
        if (act) return; // real action button — let threads.js handle it

        var row = e.target.closest ? e.target.closest(".thr-msg") : null;
        if (!row) return;

        if (e.target.closest(".thr-msg-actions, .thr-msg-avatar, .thr-profile-open, a, button, [data-jump], textarea, input")) return;

        var wasOpen = row.classList.contains("thr-actions-open");
        var open = list.querySelectorAll(".thr-msg.thr-actions-open");
        for (var i = 0; i < open.length; i++) open[i].classList.remove("thr-actions-open");
        if (!wasOpen) row.classList.add("thr-actions-open");
    });
})();
