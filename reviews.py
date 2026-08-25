from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for, g

from anime_data import anime_database
from database import (
    get_connection,
    get_anime_stats,
    add_review,
    add_xp,
    get_user_xp,
    get_user_rank,
    toggle_review_like,
    get_review_likes,
    get_bulk_review_likes,
)

reviews_bp = Blueprint("reviews", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ADMIN_USERNAME = "harshwardhan-shrivastava"


def _get_latest_reviews(limit=50):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.id, r.anime_slug, r.username, r.user_id, r.rating, r.comment,
               r.created_at, u.avatar_color, u.avatar
        FROM reviews r
        LEFT JOIN users u ON r.user_id = u.id
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Enrich with anime title + image
    for row in rows:
        entry = anime_database.get(row["anime_slug"])
        row["anime_title"] = (entry.get("title") if entry else None) or row["anime_slug"]
        row["anime_image"] = entry.get("image", "") if entry else ""

    # Attach vote counts
    review_ids = [r["id"] for r in rows]
    likes_map = get_bulk_review_likes("anime", review_ids)
    for row in rows:
        lk = likes_map.get(row["id"], {"likes": 0, "dislikes": 0})
        row["likes"] = lk["likes"]
        row["dislikes"] = lk["dislikes"]

    # Attach ranks
    user_ids = list({r["user_id"] for r in rows if r.get("user_id")})
    if user_ids:
        from database import get_all_user_ranks
        ranks = get_all_user_ranks(user_ids)
        for row in rows:
            uid = row.get("user_id")
            info = ranks.get(uid, {"xp": 0, "rank": "D"})
            row["xp"] = info["xp"]
            row["rank"] = info["rank"]
    else:
        for row in rows:
            row["xp"] = 0
            row["rank"] = "D"

    return rows


def _get_review_xp_change(likes, dislikes):
    """Calculate XP change based on like ratio."""
    total = likes + dislikes
    if total == 0:
        return 0
    ratio = likes / total
    if ratio >= 0.9:
        return 20
    elif ratio >= 0.7:
        return 10
    elif ratio >= 0.5:
        return 5
    elif ratio >= 0.3:
        return -5
    elif ratio >= 0.1:
        return -15
    else:
        return -30


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@reviews_bp.route("/reviews")
def reviews_page():
    reviews = _get_latest_reviews(limit=50)
    return render_template("reviews.html", reviews=reviews)


@reviews_bp.route("/rate_anime/<anime_slug>", methods=["GET", "POST"])
def rate_anime(anime_slug):
    anime = anime_database.get(anime_slug)
    if anime is None:
        flash("Anime not found.", "error")
        return redirect(url_for("home"))

    if request.method == "POST":
        user = g.get("user")
        if user is None:
            flash("Log in to rate this anime.", "error")
            return redirect(url_for("auth.login", next=request.path))

        try:
            rating = int(request.form.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        if rating < 1 or rating > 5:
            flash("Please pick a rating between 1 and 5.", "error")
            return redirect(url_for("reviews.rate_anime", anime_slug=anime_slug))

        comment = (request.form.get("comment") or "").strip()[:2000]
        episode_id = (request.form.get("episode_id") or "").strip()

        add_review(
            anime_slug,
            user["username"],
            rating,
            comment,
            user_id=user["id"],
        )
        # Award XP for creating a review
        add_xp(user["id"], 5)
        flash(f"Review submitted for {anime.get('title', anime_slug)}!", "success")
        return redirect(url_for("anime", anime_slug=anime_slug))

    stats = get_anime_stats(anime_slug)
    return render_template(
        "rate_anime.html",
        anime=anime,
        anime_slug=anime_slug,
        stats=stats,
    )


@reviews_bp.route("/api/review/<int:review_id>/vote", methods=["POST"])
def vote_review(review_id):
    user = g.get("user")
    if user is None:
        return jsonify({"success": False, "error": "Log in to vote"}), 401

    data = request.get_json(silent=True) or {}
    vote_type = data.get("vote_type")
    if vote_type not in ("like", "dislike"):
        return jsonify({"success": False, "error": "Invalid vote type"}), 400

    is_like = 1 if vote_type == "like" else 0
    new_is_like, removed = toggle_review_like(user["id"], "anime", review_id, is_like)

    # Get updated counts
    likes_data = get_review_likes("anime", review_id)
    return jsonify({
        "success": True,
        "likes": likes_data["likes"],
        "dislikes": likes_data["dislikes"],
        "user_vote": None if removed else ("like" if new_is_like else "dislike"),
    })


@reviews_bp.route("/reviews/claim/<int:review_id>", methods=["POST"])
def submit_claim(review_id):
    user = g.get("user")
    if user is None:
        flash("Log in to submit a claim.", "error")
        return redirect(url_for("auth.login", next=request.path))

    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Please provide a reason for your claim.", "error")
        return redirect(url_for("reviews.reviews_page"))

    conn = get_connection()
    cursor = conn.cursor()
    # Verify review exists
    cursor.execute("SELECT id FROM reviews WHERE id = ?", (review_id,))
    if cursor.fetchone() is None:
        conn.close()
        flash("Review not found.", "error")
        return redirect(url_for("reviews.reviews_page"))

    cursor.execute(
        """
        INSERT INTO claims (user_id, review_id, reason, status)
        VALUES (?, ?, ?, 'pending')
        """,
        (user["id"], review_id, reason),
    )
    conn.commit()
    conn.close()
    flash("Claim submitted. An admin will review it.", "success")
    return redirect(url_for("reviews.reviews_page"))


@reviews_bp.route("/admin/claims")
def admin_claims():
    user = g.get("user")
    if user is None or user.get("username") != ADMIN_USERNAME:
        flash("Admin access required.", "error")
        return redirect(url_for("home"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.user_id, c.review_id, c.reason, c.status, c.created_at,
               u.username,
               r.anime_slug, r.rating, r.comment, r.username AS review_author
        FROM claims c
        JOIN users u ON c.user_id = u.id
        JOIN reviews r ON c.review_id = r.id
        ORDER BY c.id DESC
        """
    )
    claims = [dict(row) for row in cursor.fetchall()]
    conn.close()

    # Enrich with anime titles
    for claim in claims:
        entry = anime_database.get(claim.get("anime_slug"))
        claim["anime_title"] = (entry.get("title") if entry else None) or claim.get("anime_slug", "")

    return render_template("admin_claims.html", claims=claims)


@reviews_bp.route("/admin/claim/<int:claim_id>/action", methods=["POST"])
def admin_claim_action(claim_id):
    user = g.get("user")
    if user is None or user.get("username") != ADMIN_USERNAME:
        flash("Admin access required.", "error")
        return redirect(url_for("home"))

    action = request.form.get("action")
    if action not in ("approve", "reject"):
        flash("Invalid action.", "error")
        return redirect(url_for("reviews.admin_claims"))

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM claims WHERE id = ?", (claim_id,))
    claim = cursor.fetchone()
    if claim is None:
        conn.close()
        flash("Claim not found.", "error")
        return redirect(url_for("reviews.admin_claims"))

    new_status = "approved" if action == "approve" else "rejected"
    cursor.execute(
        "UPDATE claims SET status = ? WHERE id = ?",
        (new_status, claim_id),
    )
    conn.commit()
    conn.close()

    flash(f"Claim #{claim_id} {new_status}.", "success")
    return redirect(url_for("reviews.admin_claims"))
