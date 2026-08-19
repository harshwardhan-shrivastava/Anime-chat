/* Small helpers shared by the page scripts. Loaded before them, so every
   file can rely on `window.AnimeUtils` existing. */
(function () {
    "use strict";

    /* Escape user text for interpolation into innerHTML. Uses the DOM's own
       serializer so there is no hand-written entity table to get wrong. */
    function escapeHtml(value) {
        const div = document.createElement("div");
        div.textContent = value == null ? "" : String(value);
        return div.innerHTML;
    }

    window.AnimeUtils = { escapeHtml: escapeHtml };
})();
