import os

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, g, url_for
from anime_data import anime_database
from database import create_tables, get_connection, get_anime_stats, get_all_anime_stats, add_review

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


@app.template_filter("anime_img")
def anime_img(image):
    """Templates call {{ image | anime_img }}. Local filenames resolve to
    /static/images/anime/<name>; full URLs (AniList CDN) pass through."""
    if image.startswith(("http://", "https://")):
        return image
    return url_for("static", filename="images/anime/" + image)


# The full catalog lives in anime_data.py (now 1000+ titles, auto-generated
# by scripts/fetch_anime_catalog.py). The home page grid is built from it.
anime_list = [
    {
        "slug": slug,
        "title": entry.get("title", slug),
        "image": entry.get("image", ""),
    }
    for slug, entry in anime_database.items()
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
    anime_data_list = []

    all_stats = get_all_anime_stats()

    for anime in anime_list:
        anime_copy = anime.copy()
        slug = anime["slug"]

        stats = all_stats.get(slug, {"votes": 0, "average": 0})
        anime_copy["live_rating"] = stats["average"] if stats["votes"] > 0 else anime_database.get(slug, {}).get("rating", "N/A")
        anime_copy["live_votes"] = stats["votes"]

        anime_data_list.append(anime_copy)

    return render_template(
        "index.html",
        anime_list=anime_data_list
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
        if item["slug"] == anime_slug:
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
    # Bind to 0.0.0.0 and honor the PORT env var so the managed preview can
    # reach the dev server. The reloader subprocess is disabled because the
    # platform manages the process lifecycle.
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        use_reloader=False,
    )


