/* Global "Add to list" picker for every anime card.
 * Clicking the bookmark button on a card opens a small popover (appended
 * to <body> so cards with overflow:hidden can't clip it) listing the
 * user's custom lists, with checkmarks for lists that already contain the
 * anime. Rows toggle membership; a footer input creates a new list.
 */
(function () {
    "use strict";

    let popover = null;      // the open popover element (or null)
    let currentBtn = null;   // the bookmark button the popover belongs to
    let currentSlug = null;

    function closestBtn(el) {
        while (el && el !== document.body) {
            if (el.classList && el.classList.contains("card-list-btn")) return el;
            el = el.parentElement;
        }
        return null;
    }

    function closePopover() {
        if (popover) {
            popover.remove();
            popover = null;
            currentBtn = null;
            currentSlug = null;
        }
        document.removeEventListener("keydown", onKey, true);
        window.removeEventListener("resize", closePopover);
    }

    function onKey(e) {
        if (e.key === "Escape") closePopover();
    }

    function positionPopover(btn) {
        const rect = btn.getBoundingClientRect();
        const width = Math.min(300, window.innerWidth - 24);
        let left = rect.left + rect.width / 2 - width / 2;
        left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
        let top = rect.bottom + 10;
        const height = popover.offsetHeight || 320;
        if (top + height > window.innerHeight - 12) {
            top = rect.top - height - 10;
        }
        popover.style.left = left + "px";
        popover.style.top = top + "px";
        popover.style.width = width + "px";
    }

    function toast(msg) {
        const el = document.createElement("div");
        el.className = "list-picker-toast";
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(() => {
            el.classList.add("out");
            setTimeout(() => el.remove(), 300);
        }, 2200);
    }

    function renderRows(lists, slug) {
        const rows = popover.querySelector(".list-picker-rows");
        rows.innerHTML = "";
        lists.forEach(function (lst) {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "list-picker-row" + (lst.contains ? " checked" : "");
            row.dataset.id = lst.id;
            row.innerHTML =
                '<span class="list-picker-check"><i class="fas fa-check"></i></span>' +
                '<span class="list-picker-name"></span>' +
                '<span class="list-picker-count"></span>';
            row.querySelector(".list-picker-name").textContent = lst.name;
            row.querySelector(".list-picker-count").textContent =
                lst.item_count ? lst.item_count + " items" : "empty";

            row.addEventListener("click", async function () {
                const id = lst.id;
                const wasChecked = row.classList.contains("checked");
                row.classList.toggle("checked", !wasChecked);
                row.disabled = true;
                try {
                    let res, data;
                    if (wasChecked) {
                        res = await fetch(
                            `/api/lists/${id}/items/${encodeURIComponent(slug)}`,
                            { method: "DELETE" }
                        );
                        data = await res.json();
                    } else {
                        const body = new URLSearchParams({ slug: slug });
                        res = await fetch(`/api/lists/${id}/items`, {
                            method: "POST",
                            headers: { "Content-Type": "application/x-www-form-urlencoded" },
                            body: body.toString()
                        });
                        data = await res.json();
                    }
                    if (!res.ok) throw new Error(data.error || "failed");
                    toast(wasChecked ? "Removed from list" : "Added to list");
                } catch (err) {
                    row.classList.toggle("checked", wasChecked);
                    if (err && err.message === "Unauthorized") {
                        window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
                        return;
                    }
                    toast("Something went wrong. Try again.");
                } finally {
                    row.disabled = false;
                }
            });
            rows.appendChild(row);
        });
        if (!lists.length) {
            rows.innerHTML =
                '<div class="list-picker-empty">No lists yet — create one below.</div>';
        }
    }

    async function openPopover(btn) {
        const slug = btn.dataset.slug;
        const title = btn.dataset.title || "";

        let res;
        try {
            res = await fetch(`/api/lists?slug=${encodeURIComponent(slug)}`);
        } catch (err) {
            return;
        }
        if (res.status === 401) {
            window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
            return;
        }
        let data;
        try {
            data = await res.json();
        } catch (err) {
            return;
        }
        if (!data.success) return;

        closePopover();
        currentBtn = btn;
        currentSlug = slug;

        popover = document.createElement("div");
        popover.className = "list-picker-popover";
        popover.dataset.max = data.max || 10;
        popover.innerHTML =
            '<div class="list-picker-head">' +
            '   <div class="list-picker-title">Add to list</div>' +
            '   <button type="button" class="list-picker-close" aria-label="Close"><span class="modal-x"></span></button>' +
            '</div>' +
            '<div class="list-picker-sub">' + escapeHtml(title) + '</div>' +
            '<div class="list-picker-counter">' + data.count + ' / ' + (data.max || 10) + ' lists</div>' +
            '<div class="list-picker-rows"></div>' +
            '<div class="list-picker-new">' +
            '   <input type="text" maxlength="50" placeholder="New list name…" autocomplete="off">' +
            '   <button type="button" class="list-picker-add"><i class="fas fa-plus"></i></button>' +
            '</div>';
        document.body.appendChild(popover);

        renderRows(data.lists, slug);

        popover.querySelector(".list-picker-close").addEventListener("click", closePopover);
        popover.querySelector(".list-picker-new input").addEventListener("keydown", function (e) {
            if (e.key === "Enter") createNewList();
        });
        popover.querySelector(".list-picker-add").addEventListener("click", createNewList);

        positionPopover(btn);
        document.addEventListener("keydown", onKey, true);
        window.addEventListener("resize", closePopover);

        // Close when clicking anywhere outside the popover (and not on a
        // bookmark button, which would just reopen it for that card).
        setTimeout(function () {
            document.addEventListener("mousedown", function (e) {
                if (!popover) return;
                if (popover.contains(e.target)) return;
                if (closestBtn(e.target)) return;
                closePopover();
            }, { once: true });
        }, 0);
    }

    async function createNewList() {
        const input = popover.querySelector(".list-picker-new input");
        const name = input.value.trim();
        if (!name) return;
        input.disabled = true;
        try {
            const res = await fetch("/api/lists", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: name })
            });
            const data = await res.json();
            if (res.status === 401) {
                window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
                return;
            }
            if (!res.ok) {
                if (data.error === "limit") {
                    toast("You've hit the 10-list max");
                } else {
                    toast("Couldn't create the list");
                }
                return;
            }
            input.value = "";
            const slug = currentSlug;
            const fresh = await fetch(`/api/lists?slug=${encodeURIComponent(slug)}`).then(r => r.json());
            if (popover && fresh.success) {
                popover.querySelector(".list-picker-counter").textContent =
                    fresh.count + " / " + (fresh.max || 10) + " lists";
                renderRows(fresh.lists, slug);
            }
            toast("List created");
        } catch (err) {
            toast("Something went wrong. Try again.");
        } finally {
            input.disabled = false;
        }
    }

    function escapeHtml(s) {
        const div = document.createElement("div");
        div.textContent = s;
        return div.innerHTML;
    }

    function wire() {
        document.querySelectorAll(".card-list-btn").forEach(function (btn) {
            if (btn.dataset.wired) return;
            btn.dataset.wired = "1";
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation();
                if (popover && currentBtn === btn) {
                    closePopover();
                    return;
                }
                openPopover(btn);
            });
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", wire);
    } else {
        wire();
    }

    // Cards can be added dynamically (e.g. quiz results), so keep scanning.
    if (window.MutationObserver) {
        const observer = new MutationObserver(function () { wire(); });
        observer.observe(document.body, { childList: true, subtree: true });
    }
})();
