"""Like/dislike voting + reviewer ranks for anime reviews.

Uses the shared review_likes table. Rank tiers: everyone starts at D;
S+ is intentionally almost impossible (50,000 XP). A reviewer whose
received votes are overwhelmingly dislikes drops to F regardless of XP.
"""
from database import get_connection, recalculate_user_xp


# ---- Rank tiers (authoritative for reviews) ----

def review_rank_for_xp(xp):
    if xp >= 15000:
        return "S+"
    if xp >= 5000:
        return "S"
    if xp >= 2000:
        return "A"
    if xp >= 1000:
        return "B"
    if xp >= 500:
        return "C"
    if xp >= 0:
        return "D"
    return "F"


RANK_COLORS = {
    "S+": "#FFD700",
    "S": "#FF9F43",
    "A": "#FECA57",
    "B": "#54A0FF",
    "C": "#a78bfa",
    "D": "#9ca3af",
    "F": "#ef4444",
}


def get_bulk_reviewer_ranks(user_ids):
    """Return {user_id: {rank, xp}} for reviewers.

    Rank is the XP tier, EXCEPT it becomes 'F' for reviewers whose received
    review votes are overwhelmingly dislikes (5+ votes, <=20% likes).
    """
    if not user_ids:
        return {}
    user_ids = list({uid for uid in user_ids if uid})
    if not user_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(user_ids))
    cursor.execute(
        f"SELECT user_id, xp FROM user_xp WHERE user_id IN ({placeholders})",
        user_ids,
    )
    xp_map = {row["user_id"]: row["xp"] for row in cursor.fetchall()}
    ranks = {uid: {"rank": review_rank_for_xp(xp_map.get(uid, 0)), "xp": xp_map.get(uid, 0)} for uid in user_ids}
    cursor.execute(
        f"""SELECT r.user_id,
        SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM review_likes rl JOIN reviews r ON r.id = rl.review_id
        WHERE rl.review_type='anime' AND r.user_id IN ({placeholders})
        GROUP BY r.user_id""",
        user_ids,
    )
    for row in cursor.fetchall():
        total = (row["likes"] or 0) + (row["dislikes"] or 0)
        if total >= 5 and (row["likes"] or 0) / total <= 0.2:
            ranks[row["user_id"]]["rank"] = "F"
    conn.close()
    return ranks


def toggle_anime_review_vote(user_id, review_id, is_like):
    """Toggle a like/dislike on an anime review.

    Returns (user_vote, removed, likes, dislikes) where user_vote is
    1 (liked), 0 (disliked) or None (no vote after toggle).
    Also adjusts the review author's XP (+10 like / -5 dislike, reversed on remove).
    """
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT user_id FROM reviews WHERE id=?", (review_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None, False, 0, 0
    review_author_id = row["user_id"]

    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type='anime' AND review_id=?",
        (user_id, review_id),
    )
    existing = cursor.fetchone()

    removed = False
    if existing:
        if existing["is_like"] == (1 if is_like else 0):
            # Same vote clicked again -> remove it
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
            removed = True
            user_vote = None
            if review_author_id and review_author_id != user_id:
                recalculate_user_xp(review_author_id)
        else:
            # Switch vote
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (1 if is_like else 0, existing["id"]))
            user_vote = 1 if is_like else 0
            if review_author_id and review_author_id != user_id:
                recalculate_user_xp(review_author_id)
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like) VALUES (?, 'anime', ?, ?)",
            (user_id, review_id, 1 if is_like else 0),
        )
        user_vote = 1 if is_like else 0
        if review_author_id and review_author_id != user_id:
            recalculate_user_xp(review_author_id)

    conn.commit()

    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type='anime' AND review_id=?",
        (review_id,),
    )
    counts = cursor.fetchone()
    conn.close()

    likes = counts["likes"] or 0
    dislikes = counts["dislikes"] or 0
    return user_vote, removed, likes, dislikes


def get_user_anime_review_votes(review_ids, user_id):
    """Return {review_id: 1 (liked) | 0 (disliked)} for votes the user cast."""
    return get_user_review_votes("anime", review_ids, user_id)


# =====================================================================
# Grade engine (trusted vs audience two-score system)
#
# Design decided with the user:
#  * Every review maps to a letter grade a reviewer gave (5-star rating).
#  * The HEADLINE grade / badge is computed ONLY from trusted reviewers
#    (B-rank and above), weighted by reviewer rank: S+=15, S=10, A=8, B=5.
#    A flood of low-rank dislikes can mathematically never move it.
#  * The AUDIENCE score is a separate plain average of everyone, so the
#    crowd is never silenced -- it just can't hijack the badge.
#  * "S+ 10/10" is the rare elite tag, only reachable with real S-tier
#    weight behind it (trusted score >= 9.5).
# =====================================================================

# Weight a reviewer's vote by their own rank tier.
RANK_WEIGHTS = {"S+": 15, "S": 10, "A": 8, "B": 5, "C": 2, "D": 1, "F": 1}

# Only these ranks count toward the trusted headline score.
TRUSTED_RANKS = frozenset({"B", "A", "S", "S+"})

GRADE_ORDER = ["D", "C", "B", "A", "S", "S+"]

# Star rating (1-5) -> the letter grade that reviewer effectively gave.
def grade_for_stars(stars):
    stars = int(stars or 0)
    return {5: "S", 4: "A", 3: "B", 2: "C", 1: "D"}.get(stars, "D")


def grade_for_score(score10):
    """Map a 0-10 score to a headline grade letter."""
    if score10 is None:
        return None
    if score10 >= 9.0:
        return "S+"
    if score10 >= 8.0:
        return "S"
    if score10 >= 7.0:
        return "A"
    if score10 >= 6.0:
        return "B"
    if score10 >= 5.0:
        return "C"
    return "D"


def anime_grade_engine(reviews, rank_map):
    """Compute the two-score grade model for one anime's reviews.

    reviews: list of dicts each with a 1-5 'rating' (and optionally user_id).
    rank_map: {user_id: {'rank': 'S+'/'S'/..., 'xp': int}} from
              get_bulk_reviewer_ranks().

    Returns a dict with an anime-wide grade, the trusted/audience scores,
    the elite S+ 10/10 flag, and the grade distribution bars.
    """
    dist = {g: 0 for g in GRADE_ORDER}
    trusted_w = 0
    trusted_sum = 0.0
    trusted_n = 0
    aud_sum = 0.0
    aud_n = 0

    for r in reviews:
        stars = max(1, min(5, int(r.get("rating") or 0)))
        val = stars * 2.0  # 1-5 stars -> 2-10 /10 score
        aud_sum += val
        aud_n += 1
        dist[grade_for_stars(stars)] += 1

        rid = r.get("user_id")
        rinfo = rank_map.get(rid, {})
        rank = (rinfo.get("rank") if isinstance(rinfo, dict) else rinfo) or "D"
        if rank in TRUSTED_RANKS:
            w = RANK_WEIGHTS.get(rank, 1)
            trusted_w += w
            trusted_sum += val * w
            trusted_n += 1

    audience = round(aud_sum / aud_n, 1) if aud_n else None
    trusted = round(trusted_sum / trusted_w, 1) if trusted_w else None
    grade = grade_for_score(trusted)
    elite = grade == "S+" and trusted is not None and trusted >= 9.5

    total = aud_n or 1
    dist_pct = {g: round(100 * c / total) for g, c in dist.items()}

    return {
        "trusted_score": trusted,
        "audience_score": audience,
        "trusted_count": trusted_n,
        "audience_count": aud_n,
        "grade": grade,
        "elite": elite,
        "trusted_label": f"{trusted:.1f}" if trusted is not None else "—",
        "audience_label": f"{audience:.1f}" if audience is not None else "—",
        "distribution": dist_pct,
    }


def get_user_review_votes(review_type, review_ids, user_id):
    """Return {review_id: 1 (liked) | 0 (disliked)} for votes the user cast
    on a given review type ('anime' or 'episode')."""
    if not review_ids or not user_id:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"SELECT review_id, is_like FROM review_likes "
        f"WHERE review_type=? AND user_id=? AND review_id IN ({placeholders})",
        [review_type, user_id] + list(review_ids),
    )
    result = {row["review_id"]: row["is_like"] for row in cursor.fetchall()}
    conn.close()
    return result
