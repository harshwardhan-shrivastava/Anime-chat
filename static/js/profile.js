/* Profile page: create / rename / delete custom anime lists. */
(function () {
    "use strict";

    const createModal = document.getElementById("createModal");
    const renameModal = document.getElementById("renameModal");
    const newListName = document.getElementById("newListName");
    const renameListName = document.getElementById("renameListName");
    let renameTargetId = null;

    function openModal(modal) {
        if (!modal) return;
        modal.hidden = false;
        document.body.style.overflow = "hidden";
        setTimeout(function () {
            const input = modal.querySelector("input[type=text]");
            if (input) input.focus();
        }, 50);
    }

    function closeAllModals() {
        if (createModal) createModal.hidden = true;
        if (renameModal) renameModal.hidden = true;
        document.body.style.overflow = "";
    }

    document.querySelectorAll("[data-close-modal]").forEach(function (btn) {
        btn.addEventListener("click", closeAllModals);
    });
    [createModal, renameModal].forEach(function (modal) {
        if (!modal) return;
        modal.addEventListener("click", function (e) {
            if (e.target === modal) closeAllModals();
        });
    });
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") closeAllModals();
    });

    // Create
    function wireCreate(trigger) {
        if (!trigger) return;
        trigger.addEventListener("click", function () {
            if (newListName) newListName.value = "";
            openModal(createModal);
        });
    }
    wireCreate(document.getElementById("createListBtn"));
    wireCreate(document.getElementById("emptyCreateBtn"));

    const confirmCreateBtn = document.getElementById("confirmCreateBtn");
    if (confirmCreateBtn) {
        confirmCreateBtn.addEventListener("click", async function () {
            const name = newListName.value.trim();
            if (!name) {
                newListName.focus();
                return;
            }
            confirmCreateBtn.disabled = true;
            try {
                const res = await fetch("/api/lists", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ name: name })
                });
                const data = await res.json();
                if (!res.ok) {
                    alert(data.error === "limit"
                        ? "You've reached the 10-list maximum."
                        : "Couldn't create that list.");
                    return;
                }
                window.location.href = "/profile?tab=lists";
            } catch (err) {
                alert("Something went wrong. Try again.");
            } finally {
                confirmCreateBtn.disabled = false;
            }
        });
    }

    // Rename
    const confirmRenameBtn = document.getElementById("confirmRenameBtn");
    if (confirmRenameBtn) {
        confirmRenameBtn.addEventListener("click", async function () {
            const name = renameListName.value.trim();
            if (!name || renameTargetId === null) return;
            confirmRenameBtn.disabled = true;
            try {
                const res = await fetch(`/api/lists/${renameTargetId}`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "rename", name: name })
                });
                const data = await res.json();
                if (!res.ok) {
                    alert("Couldn't rename that list.");
                    return;
                }
                window.location.reload();
            } catch (err) {
                alert("Something went wrong. Try again.");
            } finally {
                confirmRenameBtn.disabled = false;
            }
        });
    }

    // 3-dot menus on each list card
    document.querySelectorAll(".profile-list-card").forEach(function (card) {
        const menuBtn = card.querySelector(".profile-list-menu-btn");
        const menu = card.querySelector(".profile-list-menu");
        if (!menuBtn || !menu) return;

        menuBtn.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            const open = menu.hidden === false;
            document.querySelectorAll(".profile-list-menu").forEach(function (m) {
                m.hidden = true;
            });
            menu.hidden = open;
        });

        menu.addEventListener("click", function (e) {
            const item = e.target.closest("[data-action]");
            if (!item) return;
            const action = item.dataset.action;
            const listId = card.dataset.listId;
            if (action === "rename") {
                const nameEl = card.querySelector("h3");
                renameTargetId = listId;
                renameListName.value = nameEl ? nameEl.textContent.trim() : "";
                openModal(renameModal);
            } else if (action === "delete") {
                if (!confirm("Delete this list? The anime inside won't be affected.")) return;
                (async function () {
                    try {
                        const res = await fetch(`/api/lists/${listId}`, {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ action: "delete" })
                        });
                        if (!res.ok) {
                            alert("Couldn't delete that list.");
                            return;
                        }
                        card.style.transition = "opacity .3s, transform .3s";
                        card.style.opacity = "0";
                        card.style.transform = "scale(.92)";
                        setTimeout(function () { window.location.href = "/profile?tab=lists"; }, 300);
                    } catch (err) {
                        alert("Something went wrong. Try again.");
                    }
                })();
            }
        });

        // Clicking anywhere else closes the menu
        document.addEventListener("click", function (e) {
            if (!menu.contains(e.target) && !menuBtn.contains(e.target)) {
                menu.hidden = true;
            }
        });
    });
})();
