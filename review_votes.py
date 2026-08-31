"""Like/dislike voting + reviewer ranks for anime reviews.

Uses the shared review_likes table. Rank tiers: everyone starts at D;
S+ is intentionally almost impossible (50,000 XP). A reviewer whose
received votes are overwhelmingly dislikes drops to F regardless of XP.
"""
from database import get_connection, recalculate_user_xp, get_user_xp


def _voter_rank(user_id):
    """Current rank tier of a voter, used to price their like/dislike."""
    try:
        return review_rank_for_xp(get_user_xp(user_id))
    except Exception:
        return "D"


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
            # Switch vote: the row's point value changes with the new direction
            cursor.execute(
                "UPDATE review_likes SET is_like=?, points=? WHERE id=?",
                (1 if is_like else 0, vote_points_for_rank(_voter_rank(user_id), is_like), existing["id"]),
            )
            user_vote = 1 if is_like else 0
            if review_author_id and review_author_id != user_id:
                recalculate_user_xp(review_author_id)
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like, points) VALUES (?, 'anime', ?, ?, ?)",
            (user_id, review_id, 1 if is_like else 0, vote_points_for_rank(_voter_rank(user_id), is_like)),
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

# Ranks that shape the headline grade. C and up count (per product call:
# the pool starts at C), weighted by rank tier AND by each reviewer's XP so
# a high-XP S+ voice outweighs a fresh B -- same 10/10, more XP, bigger impact.
TRUSTED_RANKS = frozenset({"C", "B", "A", "S", "S+"})

# XP multiplier applied on top of the rank weight (1x at 0 XP up to 2x at
# 30k+ XP). This is the "XP boss" factor: among equal grades, the anime
# backed by heavier XP wins tie-breaks and pulls the average more.
def _xp_weight_factor(xp):
    xp = max(0, int(xp or 0))
    return 1.0 + min(xp, 30000) / 30000.0

GRADE_ORDER = ["D", "C", "B", "A", "S", "S+"]

# Star rating (1-10) -> the letter grade that reviewer effectively gave.
# Ratings are now out of 10: 10-9 = S+, 8 = S, 7 = A, 6 = B, 5 = C, <5 = D.
def grade_for_stars(stars):
    stars = int(stars or 0)
    return grade_for_score(stars) or "D"


# Anime / episode XP tiers (from combined trusted-XP behind the grade):
# 500 = D, 1000 = C, 1500 = B, 2000 = A, 3000 = S, 5000 = S+.
ANIME_XP_TIERS = [
    (5000, "S+"),
    (3000, "S"),
    (2000, "A"),
    (1500, "B"),
    (1000, "C"),
    (500, "D"),
]


def anime_xp_tier(trusted_xp):
    """Map combined trusted XP to the anime's own XP rank tier (or None)."""
    for threshold, tier in ANIME_XP_TIERS:
        if (trusted_xp or 0) >= threshold:
            return tier
    return None


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
    trusted_xp = 0
    aud_sum = 0.0
    aud_n = 0

    for r in reviews:
        stars = max(1, min(10, int(r.get("rating") or 0)))
        val = float(stars)  # ratings are now 1-10, which IS the /10 score
        aud_sum += val
        aud_n += 1
        dist[grade_for_stars(stars)] += 1

        rid = r.get("user_id")
        rinfo = rank_map.get(rid, {}) if rid else {}
        rank = (rinfo.get("rank") if isinstance(rinfo, dict) else rinfo) or "D"
        if rank in TRUSTED_RANKS:
            reviewer_xp = rinfo.get("xp") if isinstance(rinfo, dict) else 0
            w = RANK_WEIGHTS.get(rank, 1) * _xp_weight_factor(reviewer_xp)
            trusted_w += w
            trusted_sum += val * w
            trusted_n += 1
            trusted_xp += max(0, int(reviewer_xp or 0))

    audience = round(aud_sum / aud_n, 1) if aud_n else None
    trusted = round(trusted_sum / trusted_w, 1) if trusted_w else None
    # Trusted quorum: one B+ reviewer's 10/10 can't solo-crown an anime.
    # S+ needs >=2 trusted members at >=9.0; the elite S+ 10/10 tag needs
    # >=3 trusted members at >=9.5, so it stays rare and credible.
    grade = None
    elite = False
    if trusted is not None and trusted_n > 0:
        if trusted >= 9.0 and trusted_n >= 2:
            grade = "S+"
            elite = trusted >= 9.5 and trusted_n >= 3
        elif trusted >= 8.0:
            grade = "S"
        elif trusted >= 7.0:
            grade = "A"
        elif trusted >= 6.0:
            grade = "B"
        elif trusted >= 5.0:
            grade = "C"
        else:
            grade = "D"

    total = aud_n or 1
    dist_pct = {g: round(100 * c / total) for g, c in dist.items()}

    # Combined XP of every reviewer shaping the headline grade. Among two
    # anime with the same grade/score, the one backed by more trusted XP is
    # the stronger "10/10" -- surfaced as the Vs-battle tiebreaker.
    if trusted_xp >= 1000:
        trusted_xp_label = f"{trusted_xp / 1000:.1f}k"
    else:
        trusted_xp_label = str(trusted_xp)

    # Anime's own XP rank tier (500 D ... 5000 S+).
    xp_tier = anime_xp_tier(trusted_xp)

    # Hidden gem: the crowd loves it (audience >= 8/10) but no trusted (C+)
    # reviewer has weighed in yet. Don't crown it, don't bury it -- surface it
    # so real reviewers are prompted to lock in its badge.
    hidden_gem = trusted_n == 0 and audience is not None and audience >= 8.0

    return {
        "trusted_score": trusted,
        "audience_score": audience,
        "trusted_count": trusted_n,
        "audience_count": aud_n,
        "trusted_xp": trusted_xp,
        "trusted_xp_label": trusted_xp_label,
        "grade": grade,
        "elite": elite,
        "xp_tier": xp_tier,
        "hidden_gem": hidden_gem,
        "trusted_label": f"{trusted:.1f}" if trusted is not None else "—",
        "audience_label": f"{audience:.1f}" if audience is not None else "—",
        "distribution": dist_pct,
    }


# ---- Per-review XP (a review's own reputation, NOT the author's profile XP) ----
#
# A review earns XP from the votes it attracts, and every vote's point value
# scales with the VOTER's rank: a like is +10 x rank-weight, a dislike is
# -5 x rank-weight. So an S+ like is worth 15x a D like -- high ranks decide,
# low-rank spam can't move anything. Displayed as a D->S+ level on the card
# (the review's own clout, separate from the reviewer's profile XP).

# like = +10 * RANK_WEIGHTS, dislike = -5 * RANK_WEIGHTS, EXCEPT D which is
# nerfed hard so even 100 D dislikes (~ -300) are covered by ~4 S+ dislikes
# (-300): low-rank spam can't out-shout high ranks by volume.
VOTE_LIKE_POINTS = {r: 10 * w for r, w in RANK_WEIGHTS.items()}
VOTE_LIKE_POINTS["D"] = 5
VOTE_DISLIKE_POINTS = {r: -5 * w for r, w in RANK_WEIGHTS.items()}
VOTE_DISLIKE_POINTS["D"] = -3


def vote_points_for_rank(rank, is_like):
    """Point value of a like/dislike cast by a reviewer of the given rank."""
    if is_like:
        return VOTE_LIKE_POINTS.get(rank, 10)
    return VOTE_DISLIKE_POINTS.get(rank, -5)


def review_vote_xp(likes, dislikes):
    """Net review XP = likes*10 - dislikes*5 (floor at 0 for display).
    Legacy flat fallback; live reviews use rank-weighted vote points."""
    return max(0, (likes or 0) * 10 - (dislikes or 0) * 5)


def review_level_for_xp(review_xp):
    """Map a review's earned XP to a D->S+ level badge."""
    if review_xp >= 80:
        return "S+"
    if review_xp >= 50:
        return "S"
    if review_xp >= 30:
        return "A"
    if review_xp >= 15:
        return "B"
    if review_xp >= 5:
        return "C"
    if review_xp >= 0:
        return "D"
    return "F"


def get_bulk_review_points(review_type, review_ids):
    """Return {review_id: {like_points, dislike_points}} for multiple reviews.

    Points are the rank-weighted values stored on each vote at vote time
    (like = +10 x voter-rank weight, dislike = -5 x weight). Falls back to
    flat +10/-5 if the points column is missing on an old database.
    """
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    try:
        cursor.execute(
            f"""SELECT review_id,
            SUM(CASE WHEN is_like=1 THEN points ELSE 0 END) as like_points,
            SUM(CASE WHEN is_like=0 THEN points ELSE 0 END) as dislike_points
            FROM review_likes WHERE review_type=? AND review_id IN ({placeholders})
            GROUP BY review_id""",
            [review_type] + list(review_ids),
        )
        rows = cursor.fetchall()
    except Exception:
        # Old schema without the points column: fall back to flat values.
        cursor.execute(
            f"""SELECT review_id,
            SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as like_points,
            SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislike_points
            FROM review_likes WHERE review_type=? AND review_id IN ({placeholders})
            GROUP BY review_id""",
            [review_type] + list(review_ids),
        )
        rows = cursor.fetchall()
        flat = [
            {
                "review_id": row["review_id"],
                "like_points": (row["like_points"] or 0) * 10,
                "dislike_points": (row["dislike_points"] or 0) * -5,
            }
            for row in rows
        ]
        conn.close()
        return {r["review_id"]: {"like_points": r["like_points"], "dislike_points": r["dislike_points"]} for r in flat}
    conn.close()
    return {
        row["review_id"]: {"like_points": row["like_points"] or 0, "dislike_points": row["dislike_points"] or 0}
        for row in rows
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
