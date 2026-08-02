const searchInput = document.getElementById("animeSearch");
const resultsBox = document.getElementById("searchResults");

if (!searchInput || !resultsBox) {
    // No search box on this page — nothing to wire up.
} else {

    let debounceTimer = null;

    searchInput.addEventListener("input", () => {

        clearTimeout(debounceTimer);

        const value = searchInput.value.trim();

        if (value === "") {

            resultsBox.innerHTML = "";
            resultsBox.style.display = "none";
            return;

        }

        // Debounce so we hit the API once per keystroke burst.
        debounceTimer = setTimeout(async () => {

            try {

                const res = await fetch(
                    `/api/search?q=${encodeURIComponent(value)}`
                );

                const data = await res.json();

                resultsBox.innerHTML = "";

                if (!data.success || !data.results || data.results.length === 0) {

                    resultsBox.style.display = "none";
                    return;

                }

                resultsBox.style.display = "grid";

                data.results.slice(0, 8).forEach(anime => {

                    const card = document.createElement("div");

                    card.className = "search-card";

                    const imgSrc = anime.image.startsWith("http")
                        ? anime.image
                        : `/static/images/anime/${anime.image}`;

                    const meta = [
                        anime.year ? anime.year : "",
                        anime.rating && anime.rating !== "N/A"
                            ? `★ ${anime.rating}`
                            : ""
                    ].filter(Boolean).join("  •  ");

                    card.innerHTML = `

                        <img src="${imgSrc}" loading="lazy">

                        <div class="search-info">

                            <h4>${anime.title}</h4>

                            ${meta ? `<span class="search-meta">${meta}</span>` : ""}

                        </div>

                    `;

                    // Clicking a result opens the anime page directly.
                    card.onclick = () => {

                        window.location.href = `/anime/${anime.slug}`;

                    };

                    resultsBox.appendChild(card);

                });

            } catch (err) {

                resultsBox.style.display = "none";

            }

        }, 200);

    });

    document.addEventListener("click", (e) => {

        if (!e.target.closest(".search-box")) {

            resultsBox.style.display = "none";

        }

    });

}
