const searchInput = document.getElementById("animeSearch");
const resultsBox = document.getElementById("searchResults");

const cards = document.querySelectorAll(".anime-card");

const animeList = [];

cards.forEach(card => {

    animeList.push({

        title: card.dataset.title,
        image: card.dataset.image,
        element: card

    });

});

searchInput.addEventListener("input", () => {

    const value = searchInput.value.toLowerCase().trim();

    resultsBox.innerHTML = "";

    if(value === ""){

        resultsBox.style.display = "none";
        return;

    }

    const matches = animeList.filter(anime => {

    const firstWord = anime.title
        .toLowerCase()
        .split(" ")[0];

    return firstWord.startsWith(value);

});

    if(matches.length === 0){

        resultsBox.style.display = "none";
        return;

    }

    resultsBox.style.display = "grid";

    matches.slice(0,12).forEach(anime=>{

        const card=document.createElement("div");

        card.className="search-card";

        card.innerHTML=`

            <img src="/static/images/anime/${anime.image}">

            <h4>${anime.title}</h4>

        `;

        card.onclick=()=>{

            anime.element.scrollIntoView({

                behavior:"smooth",
                block:"center"

            });

            anime.element.style.boxShadow="0 0 35px #8F5BFF";

            setTimeout(()=>{

                anime.element.style.boxShadow="";

            },1800);

            searchInput.value=anime.title;

            resultsBox.style.display="none";

        };

        resultsBox.appendChild(card);

    });

});

document.addEventListener("click",(e)=>{

    if(!e.target.closest(".search-box")){

        resultsBox.style.display="none";

    }

});