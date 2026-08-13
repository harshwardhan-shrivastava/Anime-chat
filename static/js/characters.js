/* ======================================================
   KNOW YOUR CHARACTERS
   Live search over every (anime, character) entry, character
   cards, and a modal showing the character + their Japanese
   (sub) and English (dub) voice actors.
====================================================== */

(function () {
    "use strict";

    const dataEl = document.getElementById("characters-data");
    const DATA = JSON.parse(dataEl.textContent || "{}");

    const grid = document.getElementById("charGrid");
    const gridTitle = document.getElementById("charGridTitle");
    const gridCount = document.getElementById("charGridCount");
    const loadingEl = document.getElementById("charGridLoading");
    const emptyEl = document.getElementById("charGridEmpty");
    const loadMoreBtn = document.getElementById("charLoadMore");
    const searchInput = document.getElementById("charSearch");
    const searchClear = document.getElementById("charSearchClear");
    const searchHint = document.getElementById("charSearchHint");
    const suggestBox = document.getElementById("charSuggestBox");

    const modal = document.getElementById("charModal");
    const modalClose = document.getElementById("charModalClose");
    const modalImage = document.getElementById("charModalImage");
    const modalName = document.getElementById("charModalName");
    const modalRole = document.getElementById("charModalRole");
    const modalAnime = document.getElementById("charModalAnime");
    const modalAnimeText = document.getElementById("charModalAnimeText");
    const modalDesc = document.getElementById("charModalDesc");
    const modalJp = document.getElementById("charModalJp");
    const modalEn = document.getElementById("charModalEn");

    const LIMIT = 60;

    // slug|id -> full entry, for opening the modal without refetching.
    const CACHE = {};
    (DATA.initial || []).forEach(function (e) {
        CACHE[e.slug + "|" + e.id] = e;
    });

    let currentQ = "";
    let offset = 0;
    let loading = false;

    // ---- helpers ----------------------------------------

    function esc(text) {
        const div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function keyFor(e) {
        return (e.slug || "") + "|" + (e.id || "");
    }

    function chipHTML(v, cls, title) {
        const img = v && v.image
            ? '<img class="va-avatar" src="' + esc(v.image) + '" alt="" loading="lazy">'
            : "";
        return '<span class="va-chip ' + cls + '" title="' + title + '">' + img + esc(v && v.name) + "</span>";
    }

    function cardHTML(e) {
        const role = e.role === "MAIN" ? "Main" : "Supporting";
        const roleCls = e.role === "MAIN" ? "char-role-main" : "char-role-supporting";
        let va = "";
        if ((e.jp && e.jp.length) || (e.en && e.en.length)) {
            let chips = "";
            (e.jp || []).slice(0, 2).forEach(function (v) {
                chips += chipHTML(v, "va-jp", "Japanese voice actor");
            });
            (e.en || []).slice(0, 2).forEach(function (v) {
                chips += chipHTML(v, "va-en", "English voice actor");
            });
            va = '<div class="char-va-chips">' + chips + "</div>";
        } else {
            va = '<span class="va-soon">No voice actor listed yet</span>';
        }
        return (
            '<article class="char-card" data-key="' + esc(keyFor(e)) + '">' +
                '<div class="char-card-img">' +
                    '<img src="' + esc(e.image || "") + '" alt="' + esc(e.name) + '" loading="lazy">' +
                    '<span class="char-role ' + roleCls + '">' + role + "</span>" +
                "</div>" +
                '<div class="char-card-body">' +
                    '<h3 class="char-name">' + esc(e.name) + "</h3>" +
                    '<p class="char-anime">' + esc(e.title) + "</p>" +
                    va +
                "</div>" +
            "</article>"
        );
    }

    function setGridTitle(query, shown) {
        if (query) {
            gridTitle.textContent = "Results for \u201c" + query + "\u201d";
            gridCount.textContent = shown + " shown";
        } else {
            gridTitle.textContent = "Most Popular Casts";
            gridCount.textContent =
                Number(DATA.total || 0).toLocaleString() + " entries \u00b7 " +
                Number(DATA.covered || 0).toLocaleString() + " anime covered";
        }
    }

    function renderCards(list) {
        let html = "";
        list.forEach(function (e) {
            CACHE[keyFor(e)] = e;
            html += cardHTML(e);
        });
        if (!list.length) {
            grid.innerHTML = "";
            grid.hidden = true;
            emptyEl.hidden = false;
        } else {
            emptyEl.hidden = true;
            grid.hidden = false;
            grid.innerHTML = html;
        }
        setGridTitle(currentQ, offset + list.length);
    }

    function setLoading(on) {
        loadingEl.hidden = !on;
    }

    // ---- live suggestion dropdown (like the homepage search) ----

    function renderSuggestions(list) {
        if (!list || !list.length) {
            suggestBox.hidden = true;
            return;
        }
        const top = list.slice(0, 8);
        let html = "";
        top.forEach(function (e) {
            CACHE[keyFor(e)] = e;
            const role = e.role === "MAIN" ? "Main" : "Supporting";
            const jp = e.jp && e.jp[0];
            const en = e.en && e.en[0];
            let va = "";
            if (jp || en) {
                va =
                    '<div class="char-suggest-va">' +
                    (jp ? '<span class="va-chip va-jp">🇯🇵 ' + (jp.image ? '<img class="va-avatar" src="' + esc(jp.image) + '" alt="" loading="lazy">' : "") + esc(jp.name) + "</span>" : "") +
                    (en ? '<span class="va-chip va-en">🇺🇸 ' + (en.image ? '<img class="va-avatar" src="' + esc(en.image) + '" alt="" loading="lazy">' : "") + esc(en.name) + "</span>" : "") +
                    "</div>";
            }
            html +=
                '<div class="char-suggest-item" data-key="' + esc(keyFor(e)) + '">' +
                    '<img src="' + esc(e.image || "") + '" alt="' + esc(e.name) + '" loading="lazy">' +
                    '<div class="char-suggest-info">' +
                        '<span class="char-suggest-name">' + esc(e.name) +
                            '<span class="char-suggest-role">' + role + "</span></span>" +
                        '<span class="char-suggest-anime">' + esc(e.title) + "</span>" +
                        va +
                    "</div>" +
                "</div>";
        });
        suggestBox.innerHTML = html;
        suggestBox.hidden = false;
    }

    // ---- search -----------------------------------------

    function doSearch(reset) {
        if (loading) return;
        loading = true;
        setLoading(true);
        loadMoreBtn.hidden = true;

        if (reset) {
            offset = 0;
            grid.innerHTML = "";
            emptyEl.hidden = true;
        }

        const url =
            "/api/characters/search?q=" + encodeURIComponent(currentQ) +
            "&offset=" + offset + "&limit=" + LIMIT;

        fetch(url)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                const results = data.results || [];
                if (reset) {
                    offset = 0;
                    renderCards(results);
                    renderSuggestions(results);
                } else {
                    // append for load-more
                    results.forEach(function (e) {
                        CACHE[keyFor(e)] = e;
                        grid.insertAdjacentHTML("beforeend", cardHTML(e));
                    });
                    offset += results.length;
                    setGridTitle(currentQ, offset);
                }
                loadMoreBtn.hidden = results.length < LIMIT;
            })
            .catch(function () {
                if (reset) {
                    grid.innerHTML = "";
                    grid.hidden = true;
                    emptyEl.hidden = false;
                    emptyEl.querySelector("p").textContent =
                        "Something went wrong while searching \u2014 try again in a moment.";
                    suggestBox.hidden = true;
                }
            })
            .finally(function () {
                loading = false;
                setLoading(false);
            });
    }

    function resetToPopular() {
        currentQ = "";
        offset = 0;
        grid.innerHTML = "";
        emptyEl.hidden = true;
        suggestBox.hidden = true;
        // Re-render from the initial payload so no request is needed.
        (DATA.initial || []).forEach(function (e) {
            grid.insertAdjacentHTML("beforeend", cardHTML(e));
        });
        setGridTitle("", DATA.initial.length);
        loadMoreBtn.hidden = false;
        searchHint.textContent =
            "Characters appear as they're indexed — the most popular shows land first.";
    }

    let debounceTimer = null;

    searchInput.addEventListener("input", function () {
        clearTimeout(debounceTimer);
        searchClear.hidden = !searchInput.value;
        const q = searchInput.value.trim();
        if (!q) {
            clearTimeout(debounceTimer);
            resetToPopular();
            return;
        }
        debounceTimer = setTimeout(function () {
            currentQ = q;
            searchHint.textContent = "Searching \u201c" + q + "\u201d across the whole library\u2026";
            doSearch(true);
        }, 220);
    });

    searchClear.addEventListener("click", function () {
        searchInput.value = "";
        searchClear.hidden = true;
        resetToPopular();
        searchInput.focus();
    });

    loadMoreBtn.addEventListener("click", function () {
        doSearch(false);
    });

    document.querySelectorAll(".char-suggest").forEach(function (btn) {
        btn.addEventListener("click", function () {
            searchInput.value = btn.dataset.q || "";
            searchClear.hidden = false;
            currentQ = searchInput.value.trim();
            doSearch(true);
        });
    });

    // ---- modal -------------------------------------------

    function vaPeople(list, emptyText, container) {
        if (!list || !list.length) {
            container.innerHTML = '<span class="va-none">' + esc(emptyText) + "</span>";
            return;
        }
        const shown = list.slice(0, 8);
        const extra = list.length - shown.length;
        let html = shown.map(function (v) {
            const img = v.image
                ? '<img class="va-person-img" src="' + esc(v.image) + '" alt="" loading="lazy">'
                : '<span class="va-person-img va-person-img-empty"><i class="fas fa-user"></i></span>';
            return '<div class="va-person">' + img +
                '<span class="va-person-name">' + esc(v.name) + "</span></div>";
        }).join("");
        if (extra > 0) {
            html += '<div class="va-person va-person-more">+ ' + extra + " more</div>";
        }
        container.innerHTML = html;
    }

    function openModal(e) {
        modalImage.src = e.image || "";
        modalImage.alt = e.name || "Character";
        modalName.textContent = e.name || "";
        modalRole.textContent = e.role === "MAIN" ? "Main Character" : "Supporting Character";
        modalAnime.href = "/anime/" + encodeURIComponent(e.slug || "");
        modalAnimeText.textContent = e.title || "";
        modalDesc.textContent =
            (e.desc && e.desc !== "N/A")
                ? e.desc
                : "No character description available yet — but you can still meet the voice behind them.";

        vaPeople(
            e.jp || [],
            "No Japanese voice actor listed for this character yet.",
            modalJp
        );
        vaPeople(
            e.en || [],
            "No English dub cast listed — this one may only be available subbed.",
            modalEn
        );

        modal.hidden = false;
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        modal.hidden = true;
        document.body.style.overflow = "";
    }

    // Event delegation: cards are re-rendered constantly, so listen once.
    grid.addEventListener("click", function (ev) {
        const card = ev.target.closest(".char-card");
        if (!card) return;
        const entry = CACHE[card.dataset.key];
        if (entry) openModal(entry);
    });

    modalClose.addEventListener("click", closeModal);

    modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal();
    });

    // Clicking a suggestion opens the character modal directly.
    suggestBox.addEventListener("click", function (ev) {
        const item = ev.target.closest(".char-suggest-item");
        if (!item) return;
        const entry = CACHE[item.dataset.key];
        if (entry) openModal(entry);
        suggestBox.hidden = true;
    });

    // Clicking anywhere outside the search box dismisses the dropdown.
    document.addEventListener("click", function (e) {
        if (!e.target.closest(".char-search-box")) {
            suggestBox.hidden = true;
        }
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") {
            suggestBox.hidden = true;
            if (!modal.hidden) closeModal();
        }
    });

    // ---- init --------------------------------------------

    setGridTitle("", DATA.initial.length);
})();
