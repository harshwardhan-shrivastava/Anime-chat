import sqlite3
import random

DATABASE = "animechat.db"

AVATAR_COLORS = ["#00c16a", "#3b82f6", "#f59e0b", "#ec4899", "#9333ea", "#06b6d4", "#ef4444"]


def get_connection():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass  # WAL not available on some filesystems -- busy timeout is enough
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

    # Real accounts -- one row per registered person. is_verified flips to 1
    # once they click the link we email them.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        avatar_color TEXT NOT NULL,
        is_verified INTEGER NOT NULL DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # Every chat bubble sent in a community, tied to a real logged-in user.
    # This is what makes two different accounts (e.g. you + your brother)
    # actually see each other's messages -- it's shared, persisted state,
    # not something rendered only in one browser tab.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        anime_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        avatar_color TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'text',
        content TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # Lightweight "who's online" tracking -- updated every time a logged-in
    # user's browser polls a community. A user counts as online if their
    # last_seen is within the last 60 seconds.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS chat_presence(
        anime_slug TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        avatar_color TEXT NOT NULL,
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (anime_slug, user_id)
    )
    """)

    conn.commit()
    conn.close()


# ===============================================================
# USERS / AUTH
# ===============================================================

def create_user(username, email, password_hash):
    conn = get_connection()
    cursor = conn.cursor()

    avatar_color = random.choice(AVATAR_COLORS)

    cursor.execute(
        """
        INSERT INTO users (username, email, password_hash, avatar_color, is_verified)
        VALUES (?, ?, ?, ?, 0)
        """,
        (username, email, password_hash, avatar_color)
    )

    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


def get_user_by_email(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_username(username):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_user_verified(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


# ===============================================================
# COMMUNITY CHAT
# ===============================================================

def add_chat_message(anime_slug, user_id, username, avatar_color, kind, content):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (anime_slug, user_id, username, avatar_color, kind, content)
    )

    conn.commit()
    message_id = cursor.lastrowid

    cursor.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row)


def get_chat_messages(anime_slug, after_id=0, limit=200):
    conn = get_connection()
    cursor = conn.cursor()

    if after_id:
        # Incremental: everything newer than the last id the client has.
        cursor.execute(
            """
            SELECT * FROM chat_messages
            WHERE anime_slug = ? AND id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (anime_slug, after_id, limit)
        )
    else:
        # Fresh page load: show the newest `limit` messages (the live
        # conversation), not the oldest ones from the start of the chat.
        cursor.execute(
            """
            SELECT * FROM (
                SELECT * FROM chat_messages
                WHERE anime_slug = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (anime_slug, limit)
        )

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def touch_presence(anime_slug, user_id, username, avatar_color):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO chat_presence (anime_slug, user_id, username, avatar_color, last_seen)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(anime_slug, user_id)
        DO UPDATE SET last_seen = CURRENT_TIMESTAMP, username = excluded.username, avatar_color = excluded.avatar_color
        """,
        (anime_slug, user_id, username, avatar_color)
    )

    conn.commit()
    conn.close()


def get_online_users(anime_slug, active_seconds=60):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT username, avatar_color FROM chat_presence
        WHERE anime_slug = ?
        AND datetime(last_seen) >= datetime('now', ?)
        ORDER BY last_seen DESC
        """,
        (anime_slug, f"-{active_seconds} seconds")
    )

    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def get_all_anime_stats():
    """Returns {slug: {"votes": n, "average": x}} for every anime in one
    query -- used by the home page so a 1000+ title catalog doesn't fire
    thousands of separate SQLite queries."""

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT anime_slug, COUNT(*) AS votes, AVG(rating) AS avg_rating
        FROM reviews
        GROUP BY anime_slug
        """
    )

    rows = cursor.fetchall()
    conn.close()

    return {
        row["anime_slug"]: {
            "votes": row["votes"],
            "average": round(row["avg_rating"], 2) if row["avg_rating"] is not None else 0,
        }
        for row in rows
    }


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
