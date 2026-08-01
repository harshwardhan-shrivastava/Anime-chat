import os

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, g
from anime_data import anime_database
from database import create_tables, get_connection, get_anime_stats, add_review

from auth import auth, load_logged_in_user
from chat import chat_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

app.register_blueprint(auth)
app.register_blueprint(chat_bp)


@app.before_request
def _attach_user():
    load_logged_in_user()


@app.context_processor
def _inject_user():
    return {"current_user": g.get("user")}

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


def slugify(title):
    return (
        title
        .lower()
        .replace(" ", "-")
        .replace(".", "")
        .replace("'", "")
    )


@app.route("/")
def home():
    anime_data = []

    for anime in anime_list:
        anime_copy = anime.copy()
        slug = slugify(anime["title"])
        anime_copy["slug"] = slug

        stats = get_anime_stats(slug)
        anime_copy["live_rating"] = stats["average"] if stats["votes"] > 0 else anime_database.get(slug, {}).get("rating", "N/A")
        anime_copy["live_votes"] = stats["votes"]

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
    anime = None

    for item in anime_list:
        slug = slugify(item["title"])

        if slug == anime_slug:
            anime = item
            break

    if anime is None:
        return "Anime not found", 404

    return render_template(
        "community.html",
        anime_name=anime["title"],
        anime_image=anime["image"],
        anime_slug=anime_slug
    )


@app.route("/anime-reviews/<anime_slug>", methods=["GET"])
def anime_reviews(anime_slug):
    """Returns the live average rating, vote breakdown, and every review
    written for this anime, computed straight from the database."""

    if anime_slug not in anime_database:
        return jsonify({"success": False, "error": "Anime not found"}), 404

    stats = get_anime_stats(anime_slug)

    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
        "reviews": stats["reviews"],
    })


@app.route("/rate-anime", methods=["POST"])
def rate_anime():
    """Accepts a star rating (1-5) plus an optional username and review
    text, stores it, and returns the freshly recalculated average across
    every rating submitted so far."""

    data = request.get_json(silent=True) or {}

    anime_slug = data.get("anime_slug")
    rating = data.get("rating")
    username = (data.get("username") or "Anonymous").strip()[:40] or "Anonymous"
    comment = (data.get("comment") or "").strip()[:1000]

    if not anime_slug or anime_slug not in anime_database:
        return jsonify({"success": False, "error": "Unknown anime"}), 404

    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Rating must be a number"}), 400

    if rating < 1 or rating > 5:
        return jsonify({"success": False, "error": "Rating must be between 1 and 5"}), 400

    add_review(anime_slug, username, rating, comment)

    stats = get_anime_stats(anime_slug)

    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
        "reviews": stats["reviews"],
    })



@app.route("/find-mood")
def find_mood():
    return render_template("find_mood.html")

if __name__ == "__main__":
    create_tables()
    app.run(debug=True)


