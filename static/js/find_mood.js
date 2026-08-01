/* ======================================================
                FIND YOUR MOOD
                PART 1
====================================================== */

const moodCards = document.querySelectorAll(".mood-card");

const recommendationGrid =
    document.getElementById("recommendationGrid");

const emptyState =
    document.getElementById("emptyState");


const moodDatabase = {

    happy: [

        {
            title: "Spy x Family",
            genre: "Comedy",
            episodes: "25 Episodes",
            image: "/static/images/anime/spy_x_family.jpg",
            description:
            "A wholesome family adventure packed with comedy and heartwarming moments."
        },

        {
            title: "K-On!",
            genre: "Slice of Life",
            episodes: "39 Episodes",
            image: "/static/images/anime/k_on.jpg",
            description:
            "Relaxing music, friendship and smiles from beginning to end."
        }

    ],

    sad: [

        {

            title:"Your Lie in April",

            genre:"Drama",

            episodes:"22 Episodes",

            image:"/static/images/anime/your_lie_in_april.jpg",

            description:
            "An emotional masterpiece that will stay with you forever."

        },

        {

            title:"Clannad After Story",

            genre:"Drama",

            episodes:"24 Episodes",

            image:"/static/images/anime/clannad_after_story.jpg",

            description:
            "One of the most emotional anime ever created."

        }

    ],

    action:[

        {

            title:"Attack on Titan",

            genre:"Action",

            episodes:"89 Episodes",

            image:"/static/images/anime/attack_on_titan.jpg",

            description:
            "Epic battles, incredible animation and unforgettable twists."

        },

        {

            title:"Demon Slayer",

            genre:"Action",

            episodes:"55 Episodes",

            image:"/static/images/anime/demon_slayer.jpg",

            description:
            "Beautiful visuals mixed with breathtaking sword fights."

        }

    ],

    romance:[

        {

            title:"Horimiya",

            genre:"Romance",

            episodes:"13 Episodes",

            image:"/static/images/anime/horimiya.jpg",

            description:
            "A sweet romance filled with lovable characters."

        },

        {

            title:"Toradora!",

            genre:"Romance",

            episodes:"25 Episodes",

            image:"/static/images/anime/toradora.jpg",

            description:
            "A classic romantic comedy with emotional depth."

        }

    ],

    horror:[

        {

            title:"Another",

            genre:"Horror",

            episodes:"12 Episodes",

            image:"/static/images/anime/another.jpg",

            description:
            "A suspenseful mystery where nobody is truly safe."

        },

        {

            title:"Tokyo Ghoul",

            genre:"Dark Fantasy",

            episodes:"48 Episodes",

            image:"/static/images/anime/tokyo_ghoul.jpg",

            description:
            "A dark journey through monsters, survival and identity."

        }

    ],

    fantasy:[

        {

            title:"Frieren",

            genre:"Fantasy",

            episodes:"28 Episodes",

            image:"/static/images/anime/frieren.jpg",

            description:
            "A breathtaking fantasy adventure with emotional storytelling."

        },

        {

            title:"Mushoku Tensei",

            genre:"Fantasy",

            episodes:"48 Episodes",

            image:"/static/images/anime/mushoku_tensei.jpg",

            description:
            "One of the best modern fantasy anime."

        }

    ]

};

/* ======================================================
                FIND YOUR MOOD
                PART 2
====================================================== */

function createAnimeCard(anime){

    return `

        <div class="recommend-card">

            <img src="${anime.image}"
                 alt="${anime.title}">

            <div class="recommend-content">

                <h3>

                    ${anime.title}

                </h3>

                <div class="recommend-meta">

                    <span>

                        ${anime.genre}

                    </span>

                    <span>

                        ${anime.episodes}

                    </span>

                </div>

                <p>

                    ${anime.description}

                </p>

                <button class="recommend-btn">

                    View Anime

                </button>

            </div>

        </div>

    `;

}



function showRecommendations(mood){

    recommendationGrid.innerHTML = "";

    const animeList = moodDatabase[mood];

    if(!animeList){

        recommendationGrid.innerHTML = `

            <div class="empty-state">

                <div class="empty-icon">

                    😢

                </div>

                <h3>

                    No recommendations found.

                </h3>

                <p>

                    We'll add recommendations for this mood soon.

                </p>

            </div>

        `;

        return;

    }



    animeList.forEach(anime=>{

        recommendationGrid.innerHTML +=
            createAnimeCard(anime);

    });



    recommendationGrid.scrollIntoView({

        behavior:"smooth",

        block:"start"

    });

}



moodCards.forEach(card=>{

    card.addEventListener("click",()=>{

        moodCards.forEach(c=>{

            c.classList.remove("selected");

        });

        card.classList.add("selected");

        const mood = card.dataset.mood;

        showRecommendations(mood);

    });

});