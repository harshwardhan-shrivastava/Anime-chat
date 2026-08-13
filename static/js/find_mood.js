/* ======================================================
   FIND YOUR MOOD
   Single mood select -> one random pick from the real
   catalog, shown in a modal popup. Surprise Me picks
   from the whole library.
====================================================== */

(function () {
    "use strict";

    const moodCards = document.querySelectorAll(".mood-card");
    const pickBtn = document.getElementById("pickBtn");
    const surpriseBtn = document.getElementById("surpriseBtn");
    const modal = document.getElementById("resultModal");
    const modalClose = document.getElementById("modalClose");
    const modalAgain = document.getElementById("modalAgain");
    const modalPoster = document.getElementById("modalPoster");
    const modalTitle = document.getElementById("modalTitle");
    const modalTag = document.getElementById("modalTag");
    const modalMeta = document.getElementById("modalMeta");
    const modalSynopsis = document.getElementById("modalSynopsis");
    const modalView = document.getElementById("modalView");

    // Data injected by the server (built from the real catalog).
    const dataEl = document.getElementById("mood-data");
    const DATA = JSON.parse(dataEl.textContent || "{}");
    const pools = DATA.pools || {};
    const surprise = DATA.surprise || [];
    const labels = DATA.labels || {};

    let selectedMood = null;
    let lastPicked = null; // avoid showing the same show twice in a row
    let lastSource = null; // "mood" or "surprise" for Try Another

    function pickRandom(list) {
        if (!list || !list.length) return null;
        let item = list[Math.floor(Math.random() * list.length)];
        // If there's more than one option, avoid an immediate repeat.
        if (list.length > 1 && item.slug === lastPicked) {
            item = list[Math.floor(Math.random() * list.length)];
        }
        lastPicked = item.slug;
        return item;
    }

    function esc(text) {
        const div = document.createElement("div");
        div.textContent = text == null ? "" : String(text);
        return div.innerHTML;
    }

    function openModal(item, source) {
        if (!item) {
            alert("No anime found for that pick — try another mood!");
            return;
        }
        lastSource = source;

        modalPoster.src = item.image || "";
        modalPoster.alt = item.title || "";
        modalTitle.textContent = item.title || "";

        if (source === "surprise") {
            modalTag.textContent = "🎲 Surprise pick from the catalog";
        } else {
            const label = labels[selectedMood] || "Your Mood";
            modalTag.textContent = "✨ Picked for: " + label;
        }

        let meta = "";
        if (item.year) {
            meta += '<span><i class="fas fa-calendar-alt"></i>' + esc(item.year) + "</span>";
        }
        if (item.rating && item.rating !== "N/A") {
            meta += '<span class="meta-rating"><i class="fas fa-star"></i>' + esc(item.rating) + "</span>";
        }
        if (item.genre) {
            meta += "<span>" + esc(item.genre) + "</span>";
        }
        modalMeta.innerHTML = meta;

        modalSynopsis.textContent =
            item.synopsis && item.synopsis !== "N/A"
                ? item.synopsis
                : "A great pick from the AnimeChat catalog — open it to see episodes, chat and more.";

        modalView.href = "/anime/" + encodeURIComponent(item.slug || "");
        modal.hidden = false;
        document.body.style.overflow = "hidden";
    }

    function closeModal() {
        modal.hidden = true;
        document.body.style.overflow = "";
    }

    // ---- Mood cards: single select only -------------------

    moodCards.forEach(function (card) {
        card.addEventListener("click", function () {
            const mood = card.dataset.mood;

            if (card.classList.contains("selected")) {
                // Tapping the selected card again deselects it.
                card.classList.remove("selected");
                selectedMood = null;
            } else {
                moodCards.forEach(function (c) { c.classList.remove("selected"); });
                card.classList.add("selected");
                selectedMood = mood;
            }

            pickBtn.disabled = !selectedMood;
        });
    });

    // ---- Get My Pick ---------------------------------------

    function getMoodPick() {
        if (!selectedMood) return;
        openModal(pickRandom(pools[selectedMood] || []), "mood");
    }

    pickBtn.addEventListener("click", getMoodPick);

    // ---- Surprise Me ---------------------------------------

    surpriseBtn.addEventListener("click", function () {
        selectedMood = null;
        moodCards.forEach(function (c) { c.classList.remove("selected"); });
        pickBtn.disabled = true;
        openModal(pickRandom(surprise), "surprise");
    });

    // ---- Modal controls ------------------------------------

    modalClose.addEventListener("click", closeModal);

    modalAgain.addEventListener("click", function () {
        if (lastSource === "surprise") {
            openModal(pickRandom(surprise), "surprise");
        } else if (selectedMood) {
            openModal(pickRandom(pools[selectedMood] || []), "mood");
        } else {
            openModal(pickRandom(surprise), "surprise");
        }
    });

    modal.addEventListener("click", function (e) {
        if (e.target === modal) closeModal();
    });

    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !modal.hidden) closeModal();
    });
})();
