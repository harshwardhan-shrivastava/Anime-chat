import sqlite3

DATABASE = "animechat.db"


def get_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    # Kept for backward compatibility with any older data, no longer
    # written to directly -- all rating math is now derived from `reviews`.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS anime_ratings(
        anime_slug TEXT PRIMARY KEY,
        total_rating INTEGER DEFAULT 0,
        total_votes INTEGER DEFAULT 0
    )
    """)

    # Single source of truth: every submission is one row here.
    # rating (1-5) is required, comment is optional.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS reviews(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_slug TEXT NOT NULL,
        username TEXT NOT NULL DEFAULT 'Anonymous',
        rating INTEGER NOT NULL,
        comment TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS episode_ratings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_slug TEXT,
        season_name TEXT,
        episode_number INTEGER,
        total_rating INTEGER DEFAULT 0,
        total_votes INTEGER DEFAULT 0
    )
    """)

    conn.commit()
    conn.close()


def get_anime_stats(anime_slug):
    """Returns average rating, vote count, star breakdown, and all reviews
    for a given anime, computed live from the reviews table."""

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        "SELECT rating, COUNT(*) as count FROM reviews WHERE anime_slug=? GROUP BY rating",
        (anime_slug,)
    )
    breakdown = {str(n): 0 for n in range(1, 6)}
    for row in cursor.fetchall():
        breakdown[str(row["rating"])] = row["count"]

    total_votes = sum(breakdown.values())

    cursor.execute(
        "SELECT AVG(rating) as avg_rating FROM reviews WHERE anime_slug=?",
        (anime_slug,)
    )
    avg_row = cursor.fetchone()
    average = round(avg_row["avg_rating"], 2) if avg_row["avg_rating"] is not None else 0

    cursor.execute(
        """
        SELECT username, rating, comment, created_at
        FROM reviews
        WHERE anime_slug=?
        ORDER BY id DESC
        """,
        (anime_slug,)
    )
    reviews = [
        {
            "username": row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        }
        for row in cursor.fetchall()
    ]

    conn.close()

    return {
        "average": average,
        "votes": total_votes,
        "breakdown": breakdown,
        "reviews": reviews,
    }


def add_review(anime_slug, username, rating, comment):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO reviews (anime_slug, username, rating, comment)
        VALUES (?, ?, ?, ?)
        """,
        (anime_slug, username or "Anonymous", rating, comment or "")
    )

    conn.commit()
    conn.close()
