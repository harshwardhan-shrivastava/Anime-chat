"""Like/dislike voting + reviewer ranks for anime reviews.

Uses the shared review_likes table. Rank tiers: everyone starts at D;
S+ is intentionally almost impossible (50,000 XP). A reviewer whose
received votes are overwhelmingly dislikes drops to F regardless of XP.
"""
from database import get_connection, recalculate_user_xp_preserving_rewards, get_user_xp, war_is_live, recalculate_user_xp
from dev_accounts import is_dev_username, DEV_USERNAMES

# Developer accounts read as S+ (their raw user_xp row lags behind the
# runtime dev boost), so vote pricing must honor them or a dev's like would
# only price at D tier. Matched case-insensitively against the username.
_DEV_USERNAMES = tuple(u.lower() for u in sorted(DEV_USERNAMES))


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
    # Developer accounts are always S+ (15000 XP) so the team can test every
    # gate on any environment.
    p_dev = ",".join("?" * len(user_ids))
    cursor.execute(f"SELECT id, username FROM users WHERE id IN ({p_dev})", list(user_ids))
    for row in cursor.fetchall():
        if is_dev_username(row["username"]):
            xp_map[row["id"]] = 15000
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
                recalculate_user_xp_preserving_rewards(review_author_id)
        else:
            # Switch vote: the row's point value changes with the new direction
            cursor.execute(
                "UPDATE review_likes SET is_like=?, points=? WHERE id=?",
                (1 if is_like else 0, vote_points_for_rank(_voter_rank(user_id), is_like), existing["id"]),
            )
            user_vote = 1 if is_like else 0
            if review_author_id and review_author_id != user_id:
                recalculate_user_xp_preserving_rewards(review_author_id)
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like, points) VALUES (?, 'anime', ?, ?, ?)",
            (user_id, review_id, 1 if is_like else 0, vote_points_for_rank(_voter_rank(user_id), is_like)),
        )
        user_vote = 1 if is_like else 0
        if review_author_id and review_author_id != user_id:
            recalculate_user_xp_preserving_rewards(review_author_id)

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


# Anime / episode XP ranks — the REAL ladder (same one shown on reviews):
# 500 = D, 1000 = C, 1500 = B, 2000 = A, 3000 = S, 5000 = S+ (below 500 = ungraded).
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
# scales with the VOTER's rank. An S+ like pours in +15 liquid points while
# a fresh D like adds just +3 -- high ranks steer a review, a low-rank mob
# can only nudge it. Displayed as a D->S+ level on the card (the review's own
# clout, separate from the reviewer's profile XP).

# "Liquid" exilar point scale, priced by the VOTER's rank tier. Dislikes
# stay C rank and above only, so a fresh-account mob has no dislike weapon
# at all (D prices a dislike at 0).
VOTE_SCALE = {
    "D": (+3, 0),
    "C": (+5, -2),
    "B": (+7, -3),
    "A": (+9, -4),
    "S": (+11, -5),
    "S+": (+15, -7),
}

CAN_DISLIKE_RANKS = ("C", "B", "A", "S", "S+")


def can_dislike(rank):
    """Dislikes (like replies) are C rank and above only."""
    return rank in CAN_DISLIKE_RANKS


def vote_points_for_rank(rank, is_like):
    """Point value of a like/dislike for a voter of the given rank."""
    like, dislike = VOTE_SCALE.get(rank, VOTE_SCALE["D"])
    return like if is_like else dislike


def review_vote_xp(likes, dislikes):
    """Net review XP priced at C tier (a mid-water fallback when per-voter
    points aren't computed)."""
    c_like, c_dislike = VOTE_SCALE["C"]
    return max(0, (likes or 0) * c_like + (dislikes or 0) * c_dislike)


def review_level_for_xp(review_xp):
    """Map a review's earned Review XP to its REAL rank badge, using the
    same ladder as anime ranks: 500 = D, 1000 = C, 1500 = B, 2000 = A,
    3000 = S, 5000 = S+. Below 500 the review is ungraded (None)."""
    if review_xp >= 5000:
        return "S+"
    if review_xp >= 3000:
        return "S"
    if review_xp >= 2000:
        return "A"
    if review_xp >= 1500:
        return "B"
    if review_xp >= 1000:
        return "C"
    if review_xp >= 500:
        return "D"
    return None


# =====================================================================
# Dislike reasons (anti-bombing): a dislike only counts against a review
# if the disliker posted a reason AND that reason has a positive community
# like/dislike ratio. Reason-less legacy dislikes are grandfathered in.
# =====================================================================

def submit_review_dislike(user_id, review_type, review_id, reason):
    """Create a dislike WITH a mandatory reason (one per user per review).

    C rank (500 XP) and above only — D-rank accounts can only like, same
    as the reply rule, so a fresh-account mob has no dislike weapon at all.
    Returns (ok, error, counts) where counts = {likes, dislikes} after.
    """
    if not can_dislike(_voter_rank(user_id)):
        return False, "Dislikes require C rank (500 XP) — D-rank accounts can only like.", None
    reason = (reason or "").strip()[:500]
    if len(reason) < 2:
        return False, "Please give a short reason for your dislike.", None
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT id FROM review_reasons WHERE user_id=? AND review_type=? AND review_id=?",
            (user_id, review_type, review_id),
        )
        if cursor.fetchone():
            conn.close()
            return False, "You already posted a reason for this review.", None
        cursor.execute(
            "INSERT INTO review_reasons (review_id, review_type, user_id, reason) VALUES (?, ?, ?, ?)",
            (review_id, review_type, user_id, reason),
        )
        # Place (or switch to) the dislike vote with rank-weighted points.
        cursor.execute(
            "SELECT id FROM review_likes WHERE user_id=? AND review_type=? AND review_id=?",
            (user_id, review_type, review_id),
        )
        existing = cursor.fetchone()
        points = vote_points_for_rank(_voter_rank(user_id), False)
        if existing:
            cursor.execute(
                "UPDATE review_likes SET is_like=0, points=? WHERE id=?",
                (points, existing["id"]),
            )
        else:
            cursor.execute(
                "INSERT INTO review_likes (user_id, review_type, review_id, is_like, points) VALUES (?, ?, ?, 0, ?)",
                (user_id, review_type, review_id, points),
            )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        conn.close()
        return False, str(exc), None
    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type=? AND review_id=?",
        (review_type, review_id),
    )
    counts = cursor.fetchone()
    conn.close()
    return True, None, {"likes": counts["likes"] or 0, "dislikes": counts["dislikes"] or 0}


def toggle_reason_vote(user_id, reason_id, is_like):
    """Vote on a dislike-reason. Returns (user_vote, likes, dislikes)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type='reason' AND review_id=?",
        (user_id, reason_id),
    )
    existing = cursor.fetchone()
    user_vote = None
    if existing:
        if existing["is_like"] == (1 if is_like else 0):
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
        else:
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (1 if is_like else 0, existing["id"]))
            user_vote = 1 if is_like else 0
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like, points) VALUES (?, 'reason', ?, ?, 0)",
            (user_id, reason_id, 1 if is_like else 0),
        )
        user_vote = 1 if is_like else 0
    conn.commit()
    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type='reason' AND review_id=?",
        (reason_id,),
    )
    counts = cursor.fetchone()
    conn.close()
    return user_vote, counts["likes"] or 0, counts["dislikes"] or 0


def toggle_war_vote(user_id, entry_id, is_like):
    """Vote on a reply-war entry (any logged-in user).

    Returns (user_vote, likes, dislikes). Votes live in review_likes with
    review_type='war' and price rank-weighted like every other vote. A
    war's votes close when it ends (24h) — the podium is final then.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT review_type, review_id FROM reply_war WHERE id=?",
        (entry_id,),
    )
    entry = cursor.fetchone()
    if not entry:
        conn.close()
        raise ValueError("Entry not found")
    if not war_is_live(entry["review_type"], entry["review_id"]):
        conn.close()
        raise PermissionError("This war is over — votes are closed.")
    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type='war' AND review_id=?",
        (user_id, entry_id),
    )
    existing = cursor.fetchone()
    user_vote = None
    if existing:
        if existing["is_like"] == (1 if is_like else 0):
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
        else:
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (1 if is_like else 0, existing["id"]))
            user_vote = 1 if is_like else 0
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like, points) VALUES (?, 'war', ?, ?, 0)",
            (user_id, entry_id, 1 if is_like else 0),
        )
        user_vote = 1 if is_like else 0
    conn.commit()
    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type='war' AND review_id=?",
        (entry_id,),
    )
    counts = cursor.fetchone()
    conn.close()
    return user_vote, counts["likes"] or 0, counts["dislikes"] or 0


def get_review_reasons(review_type, review_ids, user_id):
    """Return {review_id: [reason dicts]} with vote counts + my vote."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT r.id, r.review_id, r.user_id, r.reason, r.created_at,
        u.username, u.avatar, u.avatar_color,
        SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM review_reasons r
        LEFT JOIN users u ON u.id = r.user_id
        LEFT JOIN review_likes rl ON rl.review_type='reason' AND rl.review_id = r.id
        WHERE r.review_type=? AND r.review_id IN ({placeholders})
        GROUP BY r.id ORDER BY r.id""",
        [review_type] + list(review_ids),
    )
    rows = cursor.fetchall()
    ids = [row["id"] for row in rows]
    my_votes = {}
    if user_id and ids:
        p2 = ",".join("?" * len(ids))
        cursor.execute(
            f"SELECT review_id, is_like FROM review_likes WHERE review_type='reason' AND user_id=? AND review_id IN ({p2})",
            [user_id] + ids,
        )
        my_votes = {row["review_id"]: row["is_like"] for row in cursor.fetchall()}
    conn.close()
    out = {}
    for row in rows:
        out.setdefault(row["review_id"], []).append({
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"] or "user",
            "avatar": row["avatar"],
            "avatar_color": row["avatar_color"] or "#374151",
            "reason": row["reason"],
            "created_at": row["created_at"],
            "likes": row["likes"] or 0,
            "dislikes": row["dislikes"] or 0,
            "my_vote": my_votes.get(row["id"]),
            "ratio_ok": (row["likes"] or 0) > (row["dislikes"] or 0),
        })
    return out


# Point value of one vote, computed from the VOTER's current rank tier at
# read time (no reliance on a stored points column, so every vote path --
# including the legacy toggle -- prices votes correctly). Mirrors the
# VOTE_SCALE above: D like +3, C +5/-2, B +7/-3, A +9/-4, S +11/-5, S+ +15/-7.
# Dislikes below C price at 0 (D accounts can't dislike).
# Effective voter XP for pricing: developers always price as S+ (15000),
# mirroring the runtime get_user_xp boost; real users use their stored tier.
_EFF_XP_SQL = """
CASE WHEN LOWER(COALESCE(u.username,'')) IN {dev} THEN 15000
     ELSE COALESCE(ux.xp, 0) END
""".format(dev=tuple(_DEV_USERNAMES))

_VOTE_PTS_SQL = """
CASE WHEN rl.is_like=1 THEN
  CASE WHEN ({eff}) >= 15000 THEN 15
       WHEN ({eff}) >= 5000  THEN 11
       WHEN ({eff}) >= 2000  THEN 9
       WHEN ({eff}) >= 1000  THEN 7
       WHEN ({eff}) >= 500   THEN 5
       ELSE 3 END
ELSE
  CASE WHEN ({eff}) >= 15000 THEN -7
       WHEN ({eff}) >= 5000  THEN -5
       WHEN ({eff}) >= 2000  THEN -4
       WHEN ({eff}) >= 1000  THEN -3
       WHEN ({eff}) >= 500   THEN -2
       ELSE 0 END
END
""".format(eff=_EFF_XP_SQL)


def get_dislike_gating(review_type, review_ids):
    """Return {review_id: {effective_dislike_points, contested}}.

    A dislike's points count only when it has no reason row (legacy) or its
    reason's community ratio is positive. Reasons with a bad ratio make the
    dislike 'contested' (no effect on the review). Points are priced from the
    voter's current rank tier.
    """
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT rl.review_id,
        SUM(CASE
            WHEN rr.id IS NULL THEN {_VOTE_PTS_SQL}
            WHEN (rv.likes - rv.dislikes) > 0 THEN {_VOTE_PTS_SQL}
            ELSE 0 END) as effective,
        SUM(CASE WHEN rr.id IS NOT NULL AND (rv.likes - rv.dislikes) <= 0 THEN 1 ELSE 0 END) as contested
        FROM review_likes rl
        LEFT JOIN users u ON u.id = rl.user_id
        LEFT JOIN user_xp ux ON ux.user_id = rl.user_id
        LEFT JOIN review_reasons rr
            ON rr.user_id = rl.user_id AND rr.review_type = rl.review_type AND rr.review_id = rl.review_id
        LEFT JOIN (
            SELECT review_id as rid, SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes,
                   SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes
            FROM review_likes WHERE review_type='reason' GROUP BY review_id
        ) rv ON rv.rid = rr.id
        WHERE rl.review_type=? AND rl.is_like=0 AND rl.review_id IN ({placeholders})
        GROUP BY rl.review_id""",
        [review_type] + list(review_ids),
    )
    rows = cursor.fetchall()
    conn.close()
    return {
        row["review_id"]: {"effective_dislike_points": row["effective"] or 0, "contested": row["contested"] or 0}
        for row in rows
    }


def get_bulk_review_points(review_type, review_ids):
    """Return {review_id: {like_points, dislike_points, contested}} for reviews.

    Points are priced from each voter's current rank tier (D +3 like, C +5/-2,
    S+ +15/-7). D accounts can't dislike, so their dislike prices at 0. Dislikes
    are plain C+ votes -- no reason gate needed, C-rank entry alone stops
    fresh-account mobs.
    """
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT rl.review_id,
        SUM(CASE WHEN rl.is_like=1 THEN {_VOTE_PTS_SQL} ELSE 0 END) as like_points,
        SUM(CASE WHEN rl.is_like=0 THEN {_VOTE_PTS_SQL} ELSE 0 END) as dislike_points
        FROM review_likes rl
        LEFT JOIN users u ON u.id = rl.user_id
        LEFT JOIN user_xp ux ON ux.user_id = rl.user_id
        WHERE rl.review_type=? AND rl.review_id IN ({placeholders})
        GROUP BY rl.review_id""",
        [review_type] + list(review_ids),
    )
    rows = cursor.fetchall()
    conn.close()
    result = {
        row["review_id"]: {"like_points": row["like_points"] or 0, "dislike_points": row["dislike_points"] or 0, "contested": 0}
        for row in rows
    }
    # Dislikes are now plain C+ votes (no reason gate) — every dislike
    # counts at its full rank-weighted value. The old anti-bombing reason
    # gate is retired; C-rank entry alone stops fresh-account mobs.
    return result


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
