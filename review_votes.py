"""Like/dislike voting for anime reviews (stored in the shared review_likes table)."""
from database import get_connection, add_xp


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
                add_xp(review_author_id, -10 if is_like else 5)
        else:
            # Switch vote
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (1 if is_like else 0, existing["id"]))
            user_vote = 1 if is_like else 0
            if review_author_id and review_author_id != user_id:
                add_xp(review_author_id, 15 if is_like else -15)
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like) VALUES (?, 'anime', ?, ?)",
            (user_id, review_id, 1 if is_like else 0),
        )
        user_vote = 1 if is_like else 0
        if review_author_id and review_author_id != user_id:
            add_xp(review_author_id, 10 if is_like else -5)

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
    if not review_ids or not user_id:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"SELECT review_id, is_like FROM review_likes "
        f"WHERE review_type='anime' AND user_id=? AND review_id IN ({placeholders})",
        [user_id] + list(review_ids),
    )
    result = {row["review_id"]: row["is_like"] for row in cursor.fetchall()}
    conn.close()
    return result
