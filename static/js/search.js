const searchInput = document.getElementById("animeSearch");
const resultsBox = document.getElementById("searchResults");

const cards = document.querySelectorAll(".anime-card");

let animeList = [];

cards.forEach(card => {

    animeList.push({

        title: card.dataset.title,
        element: card

    });

});

searchInput.addEventListener("input", function () {

    const value = this.value.toLowerCase().trim();

    resultsBox.innerHTML = "";

    if (value === "") {

        resultsBox.style.display = "none";
        return;

    }

    const matches = animeList.filter(anime =>
        anime.title.toLowerCase().includes(value)
    );

    if (matches.length === 0) {

        resultsBox.style.display = "none";
        return;

    }

    resultsBox.style.display = "block";

    matches.slice(0, 6).forEach(anime => {

        const item = document.createElement("div");

        item.className = "search-item";

        item.innerHTML = `
            <strong>${anime.title}</strong><br>
            <span>Anime Community</span>
        `;

        item.onclick = () => {

            anime.element.scrollIntoView({

                behavior: "smooth",
                block: "center"

            });

            anime.element.style.boxShadow = "0 0 30px #ff4da6";

            setTimeout(() => {

                anime.element.style.boxShadow = "";

            }, 1800);

            searchInput.value = anime.title;

            resultsBox.style.display = "none";

        };

        resultsBox.appendChild(item);

    });

});

document.addEventListener("click", function (e) {

    if (!e.target.closest(".search-box")) {

        resultsBox.style.display = "none";

    }

});