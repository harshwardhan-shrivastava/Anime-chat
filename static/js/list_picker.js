/* Global "Add to list" picker.
 * - Every anime card's bookmark button (.card-list-btn) opens a compact
 *   popover next to it.
 * - The anime page's "Add to List" button (.anime-add-list-btn) opens the
 *   same list rows in a centered Crunchyroll-style modal.
 * Rows show the list name, item count, and a plus button that adds the
 * anime (turns into a checkmark once the anime is in that list). Clicking
 * an already-checked row removes it again.
 */
(function () {
    "use strict";

    let picker = null;      // open popover/modal element (or null)
    let currentBtn = null;  // button it belongs to
    let currentSlug = null;

    function closestBtn(el) {
        while (el && el !== document.body) {
            if (el.classList &&
                (el.classList.contains("card-list-btn") ||
                 el.classList.contains("anime-add-list-btn"))) return el;
            el = el.parentElement;
        }
        return null;
    }

    function closePicker() {
        if (picker) {
            const container = picker.parentElement;
            if (container) container.remove();
            else picker.remove();
            picker = null;
            currentBtn = null;
            currentSlug = null;
        }
        document.body.style.overflow = "";
        document.removeEventListener("keydown", onKey, true);
        window.removeEventListener("resize", closePicker);
    }

    function onKey(e) {
        if (e.key === "Escape") closePicker();
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

    function setRowState(row, checked) {
        row.classList.toggle("checked", !!checked);
        const icon = row.querySelector(".list-picker-plus i");
        if (icon) {
            icon.className = checked ? "fas fa-check" : "fas fa-plus";
        }
    }

    function renderRows(rowsEl, lists, slug) {
        rowsEl.innerHTML = "";
        lists.forEach(function (lst) {
            const row = document.createElement("button");
            row.type = "button";
            row.className = "list-picker-row";
            row.dataset.id = lst.id;
            row.innerHTML =
                '<span class="list-picker-name"></span>' +
                '<span class="list-picker-count"></span>' +
                '<span class="list-picker-plus"><i class="fas ' +
                (lst.contains ? "fa-check" : "fa-plus") + '"></i></span>';
            row.querySelector(".list-picker-name").textContent = lst.name;
            row.querySelector(".list-picker-count").textContent =
                lst.item_count ? lst.item_count + " Items" : "Empty";

            setRowState(row, lst.contains);

            row.addEventListener("click", async function () {
                const id = lst.id;
                const wasChecked = row.classList.contains("checked");
                setRowState(row, !wasChecked);
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
                    const name = row.querySelector(".list-picker-name").textContent;
                    toast(wasChecked ? "Removed from " + name : "Added to " + name);
                } catch (err) {
                    setRowState(row, wasChecked);
                    if (err && err.message === "Unauthorized") {
                        window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
                        return;
                    }
                    toast("Something went wrong. Try again.");
                } finally {
                    row.disabled = false;
                }
            });
            rowsEl.appendChild(row);
        });
        if (!lists.length) {
            rowsEl.innerHTML =
                '<div class="list-picker-empty">No lists yet — create one below.</div>';
        }
    }

    function setCounter(el, data) {
        if (el) el.textContent = data.count + " / " + (data.max || 10) + " Lists";
    }

    async function fetchLists(slug) {
        const res = await fetch(`/api/lists?slug=${encodeURIComponent(slug)}`);
        if (res.status === 401) {
            window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
            return null;
        }
        const data = await res.json();
        return data.success ? data : null;
    }

    function positionPopover(btn) {
        const rect = btn.getBoundingClientRect();
        const width = Math.min(320, window.innerWidth - 24);
        let left = rect.left + rect.width / 2 - width / 2;
        left = Math.max(12, Math.min(left, window.innerWidth - width - 12));
        let top = rect.bottom + 10;
        const height = picker.offsetHeight || 340;
        if (top + height > window.innerHeight - 12) {
            top = rect.top - height - 10;
        }
        picker.style.left = left + "px";
        picker.style.top = top + "px";
        picker.style.width = width + "px";
    }

    async function openPicker(btn, asModal) {
        const slug = btn.dataset.slug;
        const title = btn.dataset.title || "";

        closePicker();
        currentBtn = btn;
        currentSlug = slug;

        // Open the popover INSTANTLY with a loading row - don't wait for the
        // API round trip before showing anything (that was the "opens late"
        // feeling). Rows populate below when the lists arrive.
        let container = document.createElement("div");
        container.className = asModal ? "list-picker-overlay" : "list-picker-anchor";

        picker = document.createElement("div");
        picker.className = "list-picker-popover" + (asModal ? " modal" : "");
        picker.innerHTML =
            '<div class="list-picker-head">' +
            '   <div class="list-picker-title">Add to List</div>' +
            '   <button type="button" class="list-picker-close" aria-label="Close"><span class="modal-x"></span></button>' +
            '</div>' +
            (asModal
                ? '<div class="list-picker-sub">' + escapeHtml(title) + '</div>' +
                  '<div class="list-picker-toolbar">' +
                  '   <span class="list-picker-counter"></span>' +
                  '   <a href="/profile?tab=lists" class="list-picker-manage">Manage Lists</a>' +
                  '</div>'
                : '<div class="list-picker-sub">' + escapeHtml(title) + '</div>' +
                  '<div class="list-picker-counter"></div>') +
            '<div class="list-picker-rows"><div class="list-picker-empty">Loading your lists…</div></div>' +
            '<div class="list-picker-new">' +
            '   <input type="text" maxlength="50" placeholder="New list name…" autocomplete="off">' +
            '   <button type="button" class="list-picker-add"><i class="fas fa-plus"></i></button>' +
            '</div>';

        container.appendChild(picker);
        document.body.appendChild(container);
        if (asModal) container.addEventListener("click", function (e) {
            if (e.target === container) closePicker();
        });

        picker.querySelector(".list-picker-close").addEventListener("click", closePicker);
        picker.querySelector(".list-picker-new input").addEventListener("keydown", function (e) {
            if (e.key === "Enter") createNewList();
        });
        picker.querySelector(".list-picker-add").addEventListener("click", createNewList);

        if (asModal) {
            document.body.style.overflow = "hidden";
        } else {
            positionPopover(btn);
        }
        document.addEventListener("keydown", onKey, true);
        window.addEventListener("resize", closePicker);

        // Close when clicking anywhere outside (not on a picker button).
        setTimeout(function () {
            document.addEventListener("mousedown", function (e) {
                if (!picker) return;
                if (picker.contains(e.target)) return;
                if (closestBtn(e.target)) return;
                closePicker();
            }, { once: true });
        }, 0);

        // Populate rows as soon as the API answers (popover is already open).
        try {
            const data = await fetchLists(slug);
            if (!data) return; // 401 already redirected to login
            setCounter(picker.querySelector(".list-picker-counter"), data);
            renderRows(picker.querySelector(".list-picker-rows"), data.lists, slug);
            if (!asModal) positionPopover(btn);
        } catch (err) {
            const rowsEl = picker.querySelector(".list-picker-rows");
            if (rowsEl) rowsEl.innerHTML = '<div class="list-picker-empty">Couldn\'t load your lists.</div>';
        }
    }

    async function createNewList() {
        const input = picker.querySelector(".list-picker-new input");
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
                toast(data.error === "limit" ? "You've hit the 10-list max" : "Couldn't create the list");
                return;
            }
            input.value = "";
            const fresh = await fetch(`/api/lists?slug=${encodeURIComponent(currentSlug)}`).then(r => r.json());
            if (picker && fresh.success) {
                setCounter(picker.querySelector(".list-picker-counter"), fresh);
                renderRows(picker.querySelector(".list-picker-rows"), fresh.lists, currentSlug);
            }
            toast("List created");
        } catch (err) {
            toast("Something went wrong. Try again.");
        } finally {
            input.disabled = false;
        }
    }

    const escapeHtml = window.AnimeUtils.escapeHtml;

    function wire() {
        document.querySelectorAll(".card-list-btn, .anime-add-list-btn").forEach(function (btn) {
            if (btn.dataset.wired) return;
            btn.dataset.wired = "1";
            btn.addEventListener("click", function (e) {
                e.preventDefault();
                e.stopPropagation();
                if (picker && currentBtn === btn) {
                    closePicker();
                    return;
                }
                openPicker(btn, btn.classList.contains("anime-add-list-btn"));
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
