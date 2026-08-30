(() => {
    "use strict";

    const canPrefetch = "connection" in navigator
        ? !["slow-2g", "2g"].includes(navigator.connection.effectiveType)
        : true;
    const cache = new Map();

    function isLocalPage(link) {
        return link.origin === window.location.origin
            && link.pathname.startsWith("/")
            && !link.pathname.startsWith("/static/")
            && !link.hasAttribute("download")
            && link.target !== "_blank"
            && !link.href.includes("#");
    }

    async function prefetch(link) {
        if (!canPrefetch || cache.has(link.href)) return;
        try {
            const response = await fetch(link.href, {
                credentials: "same-origin",
                headers: { "X-Prefetch": "1" }
            });
            if (response.ok && response.headers.get("content-type")?.includes("text/html")) {
                cache.set(link.href, await response.text());
            }
        } catch (_) {
            // Prefetch is an enhancement; normal navigation remains untouched.
        }
    }

    document.querySelectorAll("a").forEach((link) => {
        if (!isLocalPage(link)) return;
        link.addEventListener("mouseenter", () => prefetch(link), { once: true });
        link.addEventListener("focus", () => prefetch(link), { once: true });
        link.addEventListener("click", (event) => {
            const html = cache.get(link.href);
            if (!html) return;
            event.preventDefault();
            document.documentElement.classList.add("page-leaving");
            window.setTimeout(() => {
                document.open();
                document.write(html);
                document.close();
                window.history.pushState({}, "", link.href);
            }, 110);
        });
    });
})();
