from flask import Flask, render_template, request, jsonify
from anime_data import anime_database
from database import create_tables, get_connection

app = Flask(__name__)

anime_list = [
    {"title": "Demon Slayer", "image": "demon_slayer.jpg"},
    {"title": "One Piece", "image": "one_piece.jpg"},
    {"title": "Naruto", "image": "naruto.jpg"},
    {"title": "Dragon Ball Z", "image": "dragon_ball_z.jpg"},
    {"title": "Dragon Ball Super", "image": "dragon_ball_super.jpg"},
    {"title": "Bleach", "image": "bleach.jpg"},
    {"title": "Fairy Tail", "image": "fairy_tail.jpg"},
    {"title": "Attack on Titan", "image": "attack_on_titan.jpg"},
    {"title": "Death Note", "image": "death_note.jpg"},
    {"title": "Fullmetal Alchemist Brotherhood", "image": "fullmetal_alchemist_brotherhood.jpg"},
    {"title": "Code Geass", "image": "code_geass.jpg"},
    {"title": "Sword Art Online", "image": "sword_art_online.jpg"},
    {"title": "Hunter x Hunter", "image": "hunter_x_hunter.jpg"},
    {"title": "Tokyo Ghoul", "image": "tokyo_ghoul.jpg"},
    {"title": "Blue Exorcist", "image": "blue_ex.jpg"},
    {"title": "Soul Eater", "image": "soul_eater.jpg"},
    {"title": "Black Butler", "image": "black_butler.jpg"},
    {"title": "Steins Gate", "image": "steins_gate.jpg"},
    {"title": "Angel Beats", "image": "angel_beats.jpg"},
    {"title": "Clannad", "image": "clannad.jpg"},
    {"title": "Clannad After Story", "image": "clannad_after_story.jpg"},
    {"title": "Toradora", "image": "toradora.jpg"},
    {"title": "No Game No Life", "image": "no_game_no_life.jpg"},
    {"title": "Akame ga Kill", "image": "akame_ga_kill.jpg"},
    {"title": "Parasyte", "image": "parasyte.jpg"},
    {"title": "Psycho Pass", "image": "psycho_pass.jpg"},
    {"title": "Fate Zero", "image": "fate_zero.jpg"},
    {"title": "Fate UBW", "image": "fate_stay_night.jpg"},
    {"title": "Noragami", "image": "noragami.jpg"},
    {"title": "Gintama", "image": "gintama.jpg"},
    {"title": "Inuyasha", "image": "inuyasha.jpg"},
    {"title": "Yu Yu Hakusho", "image": "yu_yu_hakusho.jpg"},
    {"title": "Rurouni Kenshin", "image": "rurouni_kenshin.jpg"},
    {"title": "Haruhi Suzumiya", "image": "haruhi_suzumiya.jpg"},
    {"title": "Durarara", "image": "durarara.jpg"},
    {"title": "Baccano", "image": "baccano.jpg"},
    {"title": "Kuroko's Basketball", "image": "kurokos_basketball.jpg"},
    {"title": "Haikyuu", "image": "haikyuu.jpg"},
    {"title": "Initial D", "image": "initial_d.jpg"},
    {"title": "Hajime no Ippo", "image": "hajime_no_ippo.jpg"},
    {"title": "Beelzebub", "image": "beelzebub.jpg"},
    {"title": "Hitman Reborn", "image": "hitman_reborn.jpg"},
    {"title": "D.Gray-man", "image": "d_gray_man.jpg"},
    {"title": "Magi", "image": "magi.jpg"},
    {"title": "Seven Deadly Sins", "image": "seven_deadly_sins.jpg"},
    {"title": "Highschool DxD", "image": "highschool_dxd.jpg"},
    {"title": "Highschool of the Dead", "image": "highschool_of_the_dead.jpg"},
    {"title": "Elfen Lied", "image": "elfen_lied.jpg"},
    {"title": "Another", "image": "another.jpg"},
    {"title": "Mirai Nikki", "image": "mirai_nikki.jpg"},
    {"title": "Hellsing Ultimate", "image": "hellsing_ultimate.jpg"},
    {"title": "Black Lagoon", "image": "black_lagoon.jpg"},
    {"title": "Ergo Proxy", "image": "ergo_proxy.jpg"},
    {"title": "Monster", "image": "monster.jpg"},
    {"title": "Evangelion", "image": "evangelion.jpg"},
    {"title": "Trigun", "image": "trigun.jpg"},
    {"title": "Samurai Champloo", "image": "samurai_champloo.jpg"}
]


@app.route("/")
def home():

    anime_data = []

    for anime in anime_list:

        anime_copy = anime.copy()

        anime_copy["slug"] = (
            anime["title"]
            .lower()
            .replace(" ", "-")
            .replace(".", "")
            .replace("'", "")
        )

        anime_data.append(anime_copy)

    return render_template(
        "index.html",
        anime_list=anime_data
    )


@app.route("/anime/<anime_slug>")
def anime(anime_slug):

    anime = anime_database.get(anime_slug)

    if anime is None:

        return "Anime not found", 404

    return render_template(
        "anime.html",
        anime=anime
    )




@app.route("/community/<anime_slug>")
def community(anime_slug):

    anime_name = anime_slug.replace("-", " ").title()

    anime = None

    for item in anime_list:

        slug = (
            item["title"]
            .lower()
            .replace(" ", "-")
            .replace(".", "")
            .replace("'", "")
        )

        if slug == anime_slug:

            anime = item

            break

    if anime is None:

        return "Anime not found", 404

    return render_template(
        "community.html",
        anime_name=anime["title"],
        anime_image=anime["image"]
    )

@app.route("/rate-anime", methods=["POST"])
def rate_anime():

    data = request.get_json()

    anime_slug = data.get("anime_slug")
    rating = int(data.get("rating"))

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT * FROM anime_ratings WHERE anime_slug=?",
        (anime_slug,)
    )

    existing = cursor.fetchone()

    if existing:

        cursor.execute(
            """
            UPDATE anime_ratings
            SET total_rating = total_rating + ?,
                total_votes = total_votes + 1
            WHERE anime_slug = ?
            """,
            (rating, anime_slug)
        )

    else:

        cursor.execute(
            """
            INSERT INTO anime_ratings
            (anime_slug,total_rating,total_votes)
            VALUES(?,?,?)
            """,
            (anime_slug, rating, 1)
        )

    conn.commit()

    cursor.execute(
        "SELECT total_rating,total_votes FROM anime_ratings WHERE anime_slug=?",
        (anime_slug,)
    )

    row = cursor.fetchone()

    average = round(
        row["total_rating"] / row["total_votes"],
        2
    )

    conn.close()

    return jsonify({

        "success": True,
        "average": average,
        "votes": row["total_votes"]

    })



@app.route("/post-review", methods=["POST"])
def post_review():

    data = request.get_json()

    anime_slug = data.get("anime_slug")
    username = data.get("username", "Anonymous")
    rating = int(data.get("rating"))
    comment = data.get("comment", "").strip()

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO reviews
        (anime_slug, username, rating, comment)
        VALUES (?, ?, ?, ?)
        """,
        (anime_slug, username, rating, comment)
    )

    conn.commit()

    review_id = cursor.lastrowid

    conn.close()

    return jsonify({

        "success": True,
        "id": review_id

    })


if __name__ == "__main__":

    create_tables()

    app.run(debug=True)

@app.route("/rate-episode", methods=["POST"])
def rate_episode():

    data = request.get_json()

    anime_slug = data.get("anime_slug")
    season_name = data.get("season_name")
    episode_number = int(data.get("episode_number"))
    rating = int(data.get("rating"))

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT *
        FROM episode_ratings
        WHERE anime_slug=?
        AND season_name=?
        AND episode_number=?
        """,
        (anime_slug, season_name, episode_number)
    )

    existing = cursor.fetchone()

    if existing:

        cursor.execute(
            """
            UPDATE episode_ratings
            SET total_rating = total_rating + ?,
                total_votes = total_votes + 1
            WHERE anime_slug=?
            AND season_name=?
            AND episode_number=?
            """,
            (
                rating,
                anime_slug,
                season_name,
                episode_number
            )
        )

    else:

        cursor.execute(
            """
            INSERT INTO episode_ratings
            (
                anime_slug,
                season_name,
                episode_number,
                total_rating,
                total_votes
            )
            VALUES (?,?,?,?,?)
            """,
            (
                anime_slug,
                season_name,
                episode_number,
                rating,
                1
            )
        )

    conn.commit()

    cursor.execute(
        """
        SELECT total_rating,total_votes
        FROM episode_ratings
        WHERE anime_slug=?
        AND season_name=?
        AND episode_number=?
        """,
        (
            anime_slug,
            season_name,
            episode_number
        )
    )

    row = cursor.fetchone()

    average = round(
        row["total_rating"] / row["total_votes"],
        2
    )

    conn.close()

    return jsonify({

        "success": True,
        "average": average,
        "votes": row["total_votes"]

    })