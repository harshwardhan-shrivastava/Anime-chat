import os
import sqlite3
import random

DATABASE = "animechat.db"
AVATAR_COLORS = ["#00c16a", "#3b82f6", "#f59e0b", "#ec4899", "#9333ea", "#06b6d4", "#ef4444"]

# ==========================================================
# Turso (remote SQLite) support
#
# By default the app stores everything in a local animechat.db file. On
# hosting that recreates the app folder (free hosts, container recycling,
# redeploys), that file gets wiped and every account + history + list is
# lost. To keep data permanently, create a free Turso database
# (https://turso.tech) and set:
#
#     TURSO_DATABASE_URL   e.g. libsql://your-db-org.turso.io
#     TURSO_AUTH_TOKEN     the database token
#
# The app then stores everything in the remote database instead and all
# existing SQL keeps working unchanged. If Turso is unreachable, the app
# falls back to the local file so the site keeps working.
# ==========================================================
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
try:
    import turso_serverless
except ImportError:
    turso_serverless = None
TURSO_ENABLED = bool(
    turso_serverless is not None
    and TURSO_DATABASE_URL
    and TURSO_AUTH_TOKEN
)
TURSO_BROKEN = False

if TURSO_ENABLED:
    print("ANIMECHAT DB: using Turso (persistent) database")
else:
    print("ANIMECHAT DB: using local file animechat.db (accounts/history/lists can be lost if the app folder is recreated)")


def _mark_turso_broken(error):
    global TURSO_BROKEN
    if TURSO_ENABLED and not TURSO_BROKEN:
        TURSO_BROKEN = True
        print("CRITICAL: TURSO DATABASE UNREACHABLE - %r" % (error,))
        print("CRITICAL: Falling back to local animechat.db. DATA WILL BE LOST ON REDEPLOY.")
        print("CRITICAL: Check TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in the hosting environment.")


class RowView:
    """sqlite3.Row-style row: supports row["name"] and row[index]."""

    def __init__(self, keys, values):
        self._keys = tuple(keys)
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._keys.index(key)]

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return "<RowView %r>" % (dict(zip(self._keys, self._values)),)


class CompatCursor:
    """Wraps a sqlite3-style cursor (turso's Cursor) and normalizes rows so
    row["column"] works exactly like sqlite3.Row."""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    @property
    def description(self):
        return getattr(self._cursor, "description", None)

    def _column_names(self):
        description = getattr(self._cursor, "description", None)
        if not description:
            return None
        try:
            return [column[0] for column in description]
        except Exception:
            return None

    def _normalize(self, row):
        if row is None or isinstance(row, (RowView, sqlite3.Row, dict)):
            return row
        keys = self._column_names()
        if keys is None:
            return row
        return RowView(keys, row)

    def execute(self, sql, parameters=()):
        self._cursor.execute(sql, parameters)
        return self

    def fetchone(self):
        return self._normalize(self._cursor.fetchone())

    def fetchall(self):
        return [self._normalize(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield self._normalize(row)


def _new_local_connection():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


class CompatConnection:
    """Wraps a sqlite3-style connection (turso's Connection) so conn.execute,
    conn.cursor, commit and close all behave like the local sqlite3 API.

    turso's connect() is lazy: it only talks to the network on the first
    query. If any call fails, the wrapper marks Turso broken and re-runs the
    operation against a fresh local connection, so a Turso outage degrades
    to the local file transparently instead of 500ing the request."""

    def __init__(self, connection):
        self._connection = connection

    def _fallback_local(self):
        if not isinstance(self._connection, sqlite3.Connection):
            self._connection = _new_local_connection()
        return self._connection

    def execute(self, sql, parameters=()):
        try:
            return CompatCursor(self._connection.execute(sql, parameters))
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().execute(sql, parameters))

    def cursor(self):
        try:
            return CompatCursor(self._connection.cursor())
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().cursor())

    def executemany(self, sql, seq_of_parameters):
        try:
            return CompatCursor(self._connection.executemany(sql, seq_of_parameters))
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().executemany(sql, seq_of_parameters))

    def commit(self):
        try:
            return self._connection.commit()
        except Exception as error:
            _mark_turso_broken(error)
            return self._fallback_local().commit()

    def rollback(self):
        try:
            return self._connection.rollback()
        except Exception as error:
            _mark_turso_broken(error)
            return self._fallback_local().rollback()

    def close(self):
        try:
            return self._connection.close()
        except Exception as error:
            _mark_turso_broken(error)
            return self._fallback_local().close()


def get_connection():
    if TURSO_ENABLED and not TURSO_BROKEN:
        try:
            # isolation_level=None runs every statement in autocommit mode, so
            # writes are saved immediately and no BEGIN/COMMIT transaction is
            # sent through the remote HTTP protocol.
            connection = turso_serverless.connect(
                TURSO_DATABASE_URL,
                auth_token=TURSO_AUTH_TOKEN,
                isolation_level=None,
            )
            return CompatConnection(connection)
        except Exception as error:
            _mark_turso_broken(error)

    return _new_local_connection()


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anime_ratings(
            anime_slug TEXT PRIMARY KEY,
            total_rating INTEGER DEFAULT 0,
            total_votes INTEGER DEFAULT 0
        )
    """)

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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            season_name TEXT NOT NULL,
            episode_number INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL DEFAULT '#00c16a',
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, anime_slug, season_name, episode_number)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            avatar TEXT NOT NULL DEFAULT 'profile1.png',
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: existing databases don't have the avatar column yet.
    user_cols = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
    if "avatar" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT NOT NULL DEFAULT 'profile1.png'")

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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            answers TEXT NOT NULL,
            top_genres TEXT NOT NULL,
            result_slugs TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_lists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_list_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            anime_slug TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (list_id, anime_slug),
            FOREIGN KEY(list_id) REFERENCES user_lists(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS view_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            anime_slug TEXT NOT NULL,
            viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, anime_slug),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_reactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (message_id, user_id, emoji),
            FOREIGN KEY(message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
        )
    """)

    # Migration: replies need a reply_to column on chat_messages.
    cols = [row[1] for row in cursor.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "reply_to" not in cols:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN reply_to INTEGER")

    conn.commit()
    conn.close()


def create_user(username, email, password_hash, avatar="profile1.png"):
    conn = get_connection()
    cursor = conn.cursor()
    avatar_color = random.choice(AVATAR_COLORS)
    cursor.execute(
        """
        INSERT INTO users (username, email, password_hash, avatar_color, avatar, is_verified)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (username, email, password_hash, avatar_color, avatar)
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


def update_user_profile(user_id, username, avatar):
    """Update a user's username and/or avatar image. Returns True on success."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET username = ?, avatar = ? WHERE id = ?",
        (username, avatar, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def update_password(email, password_hash):
    """Set a new password hash for the account with the given email."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (password_hash, email),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


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


def _attach_reply_info(messages):
    """Add reply_to_username / reply_to_content / reply_to_kind to messages."""
    if not messages:
        return messages
    conn = get_connection()
    cursor = conn.cursor()
    for m in messages:
        m["reply_to_username"] = None
        m["reply_to_content"] = None
        m["reply_to_kind"] = None
        if m.get("reply_to"):
            cursor.execute(
                "SELECT username, content, kind FROM chat_messages WHERE id = ?",
                (m["reply_to"],)
            )
            row = cursor.fetchone()
            if row:
                content = row["content"]
                if len(content) > 180:
                    content = content[:180] + "…"
                m["reply_to_username"] = row["username"]
                m["reply_to_content"] = content
                m["reply_to_kind"] = row["kind"]
    conn.close()
    return messages


def _attach_reactions(messages, user_id=None):
    """Attach reaction counts + the requesting user's reactions to messages."""
    if not messages:
        return messages
    conn = get_connection()
    cursor = conn.cursor()
    ids = [m["id"] for m in messages]
    marks = ",".join("?" * len(ids))

    cursor.execute(
        f"""
        SELECT message_id, emoji, COUNT(*) AS cnt
        FROM chat_reactions
        WHERE message_id IN ({marks})
        GROUP BY message_id, emoji
        ORDER BY cnt DESC
        """,
        ids,
    )
    grouped = {}
    for row in cursor.fetchall():
        grouped.setdefault(row["message_id"], []).append(
            {"emoji": row["emoji"], "count": row["cnt"]}
        )

    my = {}
    if user_id:
        cursor.execute(
            f"""
            SELECT message_id, emoji
            FROM chat_reactions
            WHERE message_id IN ({marks}) AND user_id = ?
            """,
            ids + [user_id],
        )
        for row in cursor.fetchall():
            my.setdefault(row["message_id"], []).append(row["emoji"])
    conn.close()

    for m in messages:
        m["reactions"] = grouped.get(m["id"], [])
        m["my_reactions"] = my.get(m["id"], [])
    return messages


def add_chat_message(anime_slug, user_id, username, avatar_color, kind, content, reply_to=None):
    conn = get_connection()
    cursor = conn.cursor()
    if reply_to:
        cursor.execute("SELECT id FROM chat_messages WHERE id = ?", (reply_to,))
        if cursor.fetchone() is None:
            reply_to = None
    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
    )
    conn.commit()
    message_id = cursor.lastrowid
    cursor.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
    row = cursor.fetchone()
    conn.close()
    msg = dict(row)
    _attach_reply_info([msg])
    _attach_reactions([msg], user_id)
    return msg


def get_chat_message(message_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_chat_messages(anime_slug, after_id=0, limit=200, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    if after_id:
        cursor.execute(
            """
            SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind
            FROM chat_messages m
            LEFT JOIN chat_messages r ON r.id = m.reply_to
            WHERE m.anime_slug = ? AND m.id > ?
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (anime_slug, after_id, limit)
        )
    else:
        cursor.execute(
            """
            SELECT * FROM (
                SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind
                FROM chat_messages m
                LEFT JOIN chat_messages r ON r.id = m.reply_to
                WHERE m.anime_slug = ?
                ORDER BY m.id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (anime_slug, limit)
        )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    for m in rows:
        if m.get("reply_username"):
            content = m.get("reply_content") or ""
            if len(content) > 180:
                content = content[:180] + "…"
            m["reply_to_username"] = m.pop("reply_username")
            m["reply_to_content"] = content
            m["reply_to_kind"] = m.pop("reply_kind")
        else:
            m["reply_to_username"] = None
            m["reply_to_content"] = None
            m["reply_to_kind"] = None
    _attach_reactions(rows, user_id)
    return rows


def toggle_reaction(message_id, user_id, emoji):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
        (message_id, user_id, emoji),
    )
    existing = cursor.fetchone()
    reaction_id = None
    if existing:
        cursor.execute("DELETE FROM chat_reactions WHERE id = ?", (existing["id"],))
        added = False
    else:
        cursor.execute(
            "INSERT INTO chat_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
            (message_id, user_id, emoji),
        )
        reaction_id = cursor.lastrowid
        added = True
    conn.commit()
    conn.close()
    return {"added": added, "reaction_id": reaction_id}


def get_message_reactions(message_id, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT emoji, COUNT(*) AS cnt
        FROM chat_reactions
        WHERE message_id = ?
        GROUP BY emoji
        ORDER BY cnt DESC
        """,
        (message_id,),
    )
    reactions = [{"emoji": r["emoji"], "count": r["cnt"]} for r in cursor.fetchall()]
    my = []
    if user_id:
        cursor.execute(
            "SELECT emoji FROM chat_reactions WHERE message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
        my = [r["emoji"] for r in cursor.fetchall()]
    conn.close()
    return reactions, my


def get_reactions_since(after_id, limit=100, user_id=None):
    """Reactions inserted after a given id, with current counts (idempotent)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, message_id, user_id, emoji FROM chat_reactions WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    latest = max((r["id"] for r in rows), default=after_id)
    updates = []
    for r in rows:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM chat_reactions WHERE message_id = ? AND emoji = ?",
            (r["message_id"], r["emoji"]),
        )
        updates.append({
            "message_id": r["message_id"],
            "emoji": r["emoji"],
            "count": cursor.fetchone()["cnt"],
            "mine": user_id is not None and r["user_id"] == user_id,
        })
    conn.close()
    return {"latest_id": latest, "updates": updates}


def get_chat_gifs(anime_slug, limit=150):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, username, content AS url, created_at
        FROM chat_messages
        WHERE anime_slug = ? AND kind = 'gif'
        ORDER BY id DESC
        LIMIT ?
        """,
        (anime_slug, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
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


def add_episode_review(anime_slug, season_name, episode_number, user_id,
                       username, avatar_color, rating, comment):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO episode_reviews
        (anime_slug, season_name, episode_number, user_id, username,
         avatar_color, rating, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, anime_slug, season_name, episode_number)
        DO UPDATE SET
            rating = excluded.rating,
            comment = excluded.comment,
            created_at = CURRENT_TIMESTAMP
        """,
        (anime_slug, season_name, episode_number, user_id,
         username or "Anonymous", avatar_color or "#00c16a",
         rating, comment or "")
    )
    conn.commit()
    conn.close()


def get_episode_stats(anime_slug, season_name, episode_number):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT rating, COUNT(*) as count FROM episode_reviews
        WHERE anime_slug=? AND season_name=? AND episode_number=?
        GROUP BY rating""",
        (anime_slug, season_name, episode_number)
    )
    breakdown = {str(n): 0 for n in range(1, 11)}
    for row in cursor.fetchall():
        breakdown[str(row["rating"])] = row["count"]

    total_votes = sum(breakdown.values())
    cursor.execute(
        """SELECT AVG(rating) as avg_rating FROM episode_reviews
        WHERE anime_slug=? AND season_name=? AND episode_number=?""",
        (anime_slug, season_name, episode_number)
    )
    avg_row = cursor.fetchone()
    average = round(avg_row["avg_rating"], 1) if avg_row["avg_rating"] is not None else 0

    cursor.execute(
        """SELECT username, avatar_color, rating, comment, created_at
        FROM episode_reviews
        WHERE anime_slug=? AND season_name=? AND episode_number=?
        ORDER BY id DESC""",
        (anime_slug, season_name, episode_number)
    )
    reviews = [
        {
            "username": row["username"],
            "avatar_color": row["avatar_color"],
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


def get_user_episode_review(anime_slug, season_name, episode_number, user_id):
    if not user_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT rating, comment FROM episode_reviews
        WHERE anime_slug=? AND season_name=? AND episode_number=? AND user_id=?""",
        (anime_slug, season_name, episode_number, user_id)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_episode_stats(anime_slug):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT season_name, episode_number, AVG(rating) as avg_rating,
        COUNT(*) as votes
        FROM episode_reviews WHERE anime_slug=?
        GROUP BY season_name, episode_number""",
        (anime_slug,)
    )
    out = {}
    for row in cursor.fetchall():
        season = out.setdefault(row["season_name"], {})
        season[row["episode_number"]] = {
            "average": round(row["avg_rating"], 1) if row["avg_rating"] is not None else 0,
            "votes": row["votes"],
        }
    conn.close()
    return out


def save_quiz_result(user_id, answers, top_genres, result_slugs):
    import json

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO quiz_results (user_id, answers, top_genres, result_slugs)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            json.dumps(answers),
            json.dumps(top_genres),
            json.dumps(result_slugs),
        ),
    )
    conn.commit()
    conn.close()


def get_latest_quiz_result(user_id):
    import json

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM quiz_results
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["answers"] = json.loads(result["answers"])
    result["top_genres"] = json.loads(result["top_genres"])
    result["result_slugs"] = json.loads(result["result_slugs"])
    return result


# ---------------------------------------------------------------------------
# User anime lists (max 10, Crunchyroll-style) + view history
# ---------------------------------------------------------------------------

MAX_USER_LISTS = 10


def create_user_list(user_id, name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] >= MAX_USER_LISTS:
        conn.close()
        return None
    cursor.execute(
        "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
        (user_id, name),
    )
    conn.commit()
    list_id = cursor.lastrowid
    cursor.execute(
        "SELECT * FROM user_lists WHERE id = ?", (list_id,)
    )
    row = dict(cursor.fetchone())
    row["slugs"] = []
    conn.close()
    return row


def ensure_default_lists(user_id):
    """First time a user has any list access, seed the 10 default lists
    ("List 1" .. "List 10") so the add-to-list picker always has somewhere
    to put an anime. Users rename them into their own lists later."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] > 0:
        conn.close()
        return
    for i in range(1, MAX_USER_LISTS + 1):
        cursor.execute(
            "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
            (user_id, f"List {i}"),
        )
    conn.commit()
    conn.close()


def get_user_lists(user_id):
    """All of a user's lists with their anime slugs attached.

    One LEFT JOIN query instead of one query per list: the remote Turso DB
    is hit over HTTP, so the old per-list loop cost N+1 network round trips
    every time the "Add to List" picker opened.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT l.id, l.name, l.created_at, l.updated_at, i.anime_slug
        FROM user_lists l
        LEFT JOIN user_list_items i ON i.list_id = l.id
        WHERE l.user_id = ?
        ORDER BY l.updated_at DESC, l.id ASC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    by_id, order = {}, []
    for r in rows:
        lst_id = r["id"]
        if lst_id not in by_id:
            by_id[lst_id] = {
                "id": lst_id,
                "name": r["name"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "slugs": [],
            }
            order.append(lst_id)
        slug = r["anime_slug"]
        if slug:
            by_id[lst_id]["slugs"].append(slug)
    return [by_id[i] for i in order]


def get_user_list(list_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    lst = dict(row)
    cursor.execute(
        "SELECT anime_slug FROM user_list_items WHERE list_id = ?",
        (lst["id"],),
    )
    lst["slugs"] = [r["anime_slug"] for r in cursor.fetchall()]
    conn.close()
    return lst


def rename_user_list(list_id, user_id, name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE user_lists SET name = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
        """,
        (name, list_id, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def delete_user_list(list_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_list_items WHERE list_id = ?",
        (list_id,),
    )
    cursor.execute(
        "DELETE FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def _touch_list(list_id, cursor):
    cursor.execute(
        "UPDATE user_lists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (list_id,),
    )


def add_to_user_list(list_id, user_id, anime_slug):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    if not cursor.fetchone():
        conn.close()
        return False
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_list_items (list_id, anime_slug)
        VALUES (?, ?)
        """,
        (list_id, anime_slug),
    )
    added = cursor.rowcount > 0
    _touch_list(list_id, cursor)
    conn.commit()
    conn.close()
    return added


def remove_from_user_list(list_id, user_id, anime_slug):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    if not cursor.fetchone():
        conn.close()
        return False
    cursor.execute(
        "DELETE FROM user_list_items WHERE list_id = ? AND anime_slug = ?",
        (list_id, anime_slug),
    )
    removed = cursor.rowcount > 0
    if removed:
        _touch_list(list_id, cursor)
    conn.commit()
    conn.close()
    return removed


def record_view(user_id, anime_slug):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO view_history (user_id, anime_slug, viewed_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, anime_slug)
        DO UPDATE SET viewed_at = CURRENT_TIMESTAMP
        """,
        (user_id, anime_slug),
    )
    conn.commit()
    conn.close()


def get_history_count(user_id):
    """Fast COUNT(*) for the profile header - avoids fetching every row
    just to show a number (that was a huge query on heavy watchers)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS n FROM view_history WHERE user_id = ?",
        (user_id,),
    )
    n = cursor.fetchone()["n"]
    conn.close()
    return n


def get_view_history(user_id, limit=60):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT anime_slug, viewed_at FROM view_history
        WHERE user_id = ?
        ORDER BY viewed_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows