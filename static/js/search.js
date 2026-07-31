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

    const matches = animeList
.map(anime => {

    const title = anime.title.toLowerCase();
    const words = title.split(" ");

    let score = -1;

    // First word starts with search
    if(words[0].startsWith(value))
        score = 3;

    // Any other word starts with search
    else if(words.some(word => word.startsWith(value)))
        score = 2;

    // Search appears anywhere
    else if(title.includes(value))
        score = 1;

    return {...anime, score};

})
.filter(anime => anime.score > 0)
.sort((a,b)=>b.score-a.score);
    if(matches.length === 0){

        resultsBox.style.display = "none";
        return;

    }

    resultsBox.style.display = "grid";

    matches.slice(0,8).forEach(anime=>{

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