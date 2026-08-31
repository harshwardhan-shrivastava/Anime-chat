"""Runtime patch: get_user_review_history crashed every profile page.

Two pre-existing bugs in the original: the episode query declared two
placeholders but passed no parameter tuple (sqlite3.ProgrammingError),
and the anime query selected id/rating/comment from anime_ratings, which
is an aggregate table with no such columns. The real per-user anime
reviews live in the `reviews` table. This replaces the function with the
corrected version at import time so profile routes use the fixed one.
"""

import database
from anime_data import anime_database

_ORIG = database.get_user_review_history


def _fixed(user_id, limit=50):
    conn = database.get_connection()
    cursor = conn.cursor()
    reviews = []
    # Episode reviews
    cursor.execute(
        "SELECT id, anime_slug, season_name, episode_number, rating, comment, created_at "
        "FROM episode_reviews WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    for row in cursor.fetchall():
        entry = anime_database.get(row["anime_slug"])
        reviews.append({
            "type": "episode",
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "anime_title": (entry.get("title") if entry else None) or row["anime_slug"],
            "season_name": row["season_name"],
            "episode_number": row["episode_number"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        })
    # Anime reviews (per-user rows live in `reviews`, not anime_ratings)
    cursor.execute(
        "SELECT id, anime_slug, rating, comment, created_at "
        "FROM reviews WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    for row in cursor.fetchall():
        entry = anime_database.get(row["anime_slug"])
        reviews.append({
            "type": "anime",
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "anime_title": (entry.get("title") if entry else None) or row["anime_slug"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        })
    conn.close()
    reviews.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return reviews[:limit]


def apply_review_history_fix():
    database.get_user_review_history = _fixed
