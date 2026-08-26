import os
import sqlite3
import random
import threading
import time

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
    print("OTAKUL DB: using Turso (persistent) database")
    # The bundled driver posts every statement over a brand-new urllib
    # connection (fresh TCP + TLS handshake per call), which turns each
    # small query into a full round trip. Patch its transport to reuse a
    # pooled keep-alive connection so chat polls, list saves and page
    # loads stay fast on the deployed site.
    import json as _json
    import requests as _requests
    from turso_serverless.session import Session as _TursoSession
    from turso_serverless.protocol import ProtocolError as _TursoProtocolError

    _turso_http = _requests.Session()

    def _turso_post_keepalive(self, path, body):
        url = f"{self._base_url}{path}"
        data = _json.dumps(body, allow_nan=False).encode("utf-8")
        try:
            resp = _turso_http.post(url, data=data, headers=self._headers(), timeout=30)
        except _requests.exceptions.RequestException as e:
            self._reset_stream()
            raise _TursoProtocolError(f"request to {url} failed: {e!r}") from None
        if resp.status_code != 200:
            self._reset_stream()
            message = None
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    for key in ("error", "message"):
                        if isinstance(parsed.get(key), str):
                            message = parsed[key]
                            break
            except ValueError:
                pass
            if message is not None:
                raise _TursoProtocolError(f"HTTP status {resp.status_code}: {message}") from None
            raise _TursoProtocolError(f"HTTP status {resp.status_code}") from None
        return resp.content

    _TursoSession._post = _turso_post_keepalive
else:
    print("OTAKUL DB: using local file animechat.db (accounts/history/lists can be lost if the app folder is recreated)")


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
        # Don't close the persistent Turso connection — it's reused
        # across requests to avoid re-establishing TLS + auth each time.
        if isinstance(self._connection, sqlite3.Connection):
            try:
                return self._connection.close()
            except Exception:
                pass


# Persistent Turso connection — reuse instead of creating a new one
# per request, which eliminates the TLS handshake + auth round-trip
# that was making every page take 5-10+ seconds.
_turso_conn = None
_turso_conn_lock = threading.Lock()

def get_connection():
    global _turso_conn
    if TURSO_ENABLED and not TURSO_BROKEN:
        with _turso_conn_lock:
            if _turso_conn is not None:
                return _turso_conn
        try:
            connection = turso_serverless.connect(
                TURSO_DATABASE_URL,
                auth_token=TURSO_AUTH_TOKEN,
                isolation_level=None,
            )
            conn = CompatConnection(connection)
            with _turso_conn_lock:
                _turso_conn = conn
            return conn
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

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS community_members(
            anime_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (anime_slug, user_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Migration: replies need a reply_to column on chat_messages.
    cols = [row[1] for row in cursor.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "reply_to" not in cols:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN reply_to INTEGER")

    # Migration: reviews may have user_id column
    review_cols = [row[1] for row in cursor.execute("PRAGMA table_info(reviews)").fetchall()]
    if "user_id" not in review_cols:
        cursor.execute("ALTER TABLE reviews ADD COLUMN user_id INTEGER DEFAULT NULL")

    # Migration: users may have is_public column
    user_cols = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
    if "is_public" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN is_public INTEGER DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_xp(
            user_id INTEGER PRIMARY KEY,
            xp INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_likes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            review_type TEXT NOT NULL,
            review_id INTEGER NOT NULL,
            is_like INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, review_type, review_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            review_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        )
    """)

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
    if changed:
        _drop_user_cache(user_id)
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
    if changed:
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        if row:
            _drop_user_cache(row["id"])
    conn.close()
    return changed


def get_user_by_email(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
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


# ---- Per-request user cache -------------------------------------------------
# Every request loads the logged-in user (app.before_request) and chat
# messages resolve their senders, so user rows are the hottest reads in the
# app. On the remote Turso DB each lookup is a network round trip; caching
# them briefly turns those into in-memory hits. Profile updates invalidate
# the entry immediately.
_USER_CACHE_TTL = 20
_user_cache = {}
_user_cache_lock = threading.Lock()


def _cache_user(user_id, row):
    with _user_cache_lock:
        _user_cache[user_id] = (time.time() + _USER_CACHE_TTL, row)


def _drop_user_cache(user_id):
    with _user_cache_lock:
        _user_cache.pop(user_id, None)


def get_user_by_id(user_id):
    if not user_id:
        return None
    now = time.time()
    with _user_cache_lock:
        hit = _user_cache.get(user_id)
        if hit and hit[0] > now:
            return dict(hit[1]) if hit[1] is not None else None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    result = dict(row) if row else None
    _cache_user(user_id, result)
    return result


def mark_user_verified(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    _drop_user_cache(user_id)


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
    """Insert a new chat message and return the fully-formed message dict.

    Everything runs on a single DB connection (zero extra round trips):
      1. Validate reply_to (one query)
      2. INSERT the message (one query)
      3. Fetch the user's avatar (one query — same connection)
      4. Fetch reply snippet if applicable (one query — same connection)

    Reactions are skipped entirely: a brand-new message always has zero
    reactions, so querying chat_reactions was pure waste (~800 ms over Turso).
    """
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
    message_id = cursor.lastrowid

    # Fetch the user avatar in the same connection.
    cursor.execute("SELECT avatar FROM users WHERE id = ?", (user_id,))
    avatar_row = cursor.fetchone()
    avatar = (dict(avatar_row)["avatar"] if avatar_row else "profile1.png")

    # Build the message dict directly — no need to re-SELECT the row we just
    # inserted; we already know every column value.
    from datetime import datetime, timezone
    msg = {
        "id": message_id,
        "anime_slug": anime_slug,
        "user_id": user_id,
        "username": username,
        "avatar_color": avatar_color,
        "avatar": avatar,
        "kind": kind,
        "content": content,
        "reply_to": reply_to,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "reactions": [],
        "my_reactions": [],
    }

    # Attach reply snippet in the same connection if needed.
    if reply_to:
        cursor.execute(
            "SELECT username, content, kind FROM chat_messages WHERE id = ?",
            (reply_to,),
        )
        rrow = cursor.fetchone()
        if rrow:
            rc = dict(rrow)
            text = rc["content"] or ""
            if len(text) > 180:
                text = text[:180] + "…"
            msg["reply_to_username"] = rc["username"]
            msg["reply_to_content"] = text
            msg["reply_to_kind"] = rc["kind"]
        else:
            msg["reply_to_username"] = None
            msg["reply_to_content"] = None
            msg["reply_to_kind"] = None
    else:
        msg["reply_to_username"] = None
        msg["reply_to_content"] = None
        msg["reply_to_kind"] = None

    conn.close()
    return msg


def add_chat_message_with_presence(anime_slug, user_id, username, avatar_color, kind, content, reply_to=None):
    """Insert a chat message AND touch presence in a single DB connection.

    Same as add_chat_message but also updates the user's presence in the
    same connection — avoids the extra Turso round-trip that a separate
    touch_presence() call would cost (~800 ms).
    """
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
    message_id = cursor.lastrowid

    # Fetch the user avatar in the same connection.
    cursor.execute("SELECT avatar FROM users WHERE id = ?", (user_id,))
    avatar_row = cursor.fetchone()
    avatar = (dict(avatar_row)["avatar"] if avatar_row else "profile1.png")

    # Touch presence in the same connection.
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

    from datetime import datetime, timezone
    msg = {
        "id": message_id,
        "anime_slug": anime_slug,
        "user_id": user_id,
        "username": username,
        "avatar_color": avatar_color,
        "avatar": avatar,
        "kind": kind,
        "content": content,
        "reply_to": reply_to,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "reactions": [],
        "my_reactions": [],
    }

    if reply_to:
        cursor.execute(
            "SELECT username, content, kind FROM chat_messages WHERE id = ?",
            (reply_to,),
        )
        rrow = cursor.fetchone()
        if rrow:
            rc = dict(rrow)
            text = rc["content"] or ""
            if len(text) > 180:
                text = text[:180] + "…"
            msg["reply_to_username"] = rc["username"]
            msg["reply_to_content"] = text
            msg["reply_to_kind"] = rc["kind"]
        else:
            msg["reply_to_username"] = None
            msg["reply_to_content"] = None
            msg["reply_to_kind"] = None
    else:
        msg["reply_to_username"] = None
        msg["reply_to_content"] = None
        msg["reply_to_kind"] = None

    conn.close()
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
            SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind,
                   u.avatar AS avatar
            FROM chat_messages m
            LEFT JOIN chat_messages r ON r.id = m.reply_to
            LEFT JOIN users u ON u.id = m.user_id
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
                SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind,
                       u.avatar AS avatar
                FROM chat_messages m
                LEFT JOIN chat_messages r ON r.id = m.reply_to
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.anime_slug = ?
                ORDER BY m.id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (anime_slug, limit)
        )
    rows = [dict(row) for row in cursor.fetchall()]
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
    # Inline reactions query on the SAME connection — avoids opening a
    # second Turso connection (~800 ms round-trip saved per poll).
    if rows and user_id:
        ids = [m["id"] for m in rows]
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
        for rr in cursor.fetchall():
            grouped.setdefault(rr["message_id"], []).append(
                {"emoji": rr["emoji"], "count": rr["cnt"]}
            )
        my = {}
        cursor.execute(
            f"""
            SELECT message_id, emoji
            FROM chat_reactions
            WHERE message_id IN ({marks}) AND user_id = ?
            """,
            ids + [user_id],
        )
        for rr in cursor.fetchall():
            my.setdefault(rr["message_id"]).append(rr["emoji"]) if rr["message_id"] in my else my.setdefault(rr["message_id"], [rr["emoji"]])
        for m in rows:
            m["reactions"] = grouped.get(m["id"], [])
            m["my_reactions"] = my.get(m["id"], [])
    elif rows:
        for m in rows:
            m["reactions"] = []
            m["my_reactions"] = []
    conn.close()
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
        SELECT p.username, p.avatar_color, u.avatar AS avatar
        FROM chat_presence p
        LEFT JOIN users u ON u.id = p.user_id
        WHERE p.anime_slug = ?
        AND datetime(p.last_seen) >= datetime('now', ?)
        ORDER BY p.last_seen DESC
        """,
        (anime_slug, f"-{active_seconds} seconds")
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# ------------------------------------------------------------------
#  Community membership
# ------------------------------------------------------------------

def join_community(anime_slug, user_id):
    """Add a user to a community. Idempotent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO community_members (anime_slug, user_id)
        VALUES (?, ?)
        """,
        (anime_slug, user_id),
    )
    conn.commit()
    joined = cursor.rowcount > 0
    conn.close()
    return joined


def leave_community(anime_slug, user_id):
    """Remove a user from a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM community_members WHERE anime_slug = ? AND user_id = ?",
        (anime_slug, user_id),
    )
    conn.commit()
    left = cursor.rowcount > 0
    conn.close()
    return left


def is_community_member(anime_slug, user_id):
    """Check if a user has joined a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM community_members WHERE anime_slug = ? AND user_id = ?",
        (anime_slug, user_id),
    )
    found = cursor.fetchone() is not None
    conn.close()
    return found


def get_community_members(anime_slug, limit=200):
    """Return the list of members who joined a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.username, u.avatar, u.avatar_color, cm.joined_at
        FROM community_members cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.anime_slug = ?
        ORDER BY cm.joined_at DESC
        LIMIT ?
        """,
        (anime_slug, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_community_member_count(anime_slug):
    """Return the number of members in a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM community_members WHERE anime_slug = ?",
        (anime_slug,),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


# Rating aggregates are re-rendered on every catalog page; cache them
# briefly so browse/home loads don't each re-scan the whole reviews table
# over the remote DB.
_STATS_CACHE_TTL = 10
_stats_cache = {"at": 0.0, "data": None}


def get_all_anime_stats():
    now = time.time()
    if now - _stats_cache["at"] < _STATS_CACHE_TTL and _stats_cache["data"] is not None:
        return _stats_cache["data"]
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
    result = {
        row["anime_slug"]: {
            "votes": row["votes"],
            "average": round(row["avg_rating"], 2) if row["avg_rating"] is not None else 0,
        }
        for row in rows
    }
    _stats_cache["at"] = time.time()
    _stats_cache["data"] = result
    return result


# Simple in-memory cache for anime stats (avoids repeated Turso queries)
_anime_stats_cache = {}
_anime_stats_cache_ttl = 120  # seconds
_anime_stats_cache_times = {}

def get_anime_stats(anime_slug):
    now = time.time()
    cached = _anime_stats_cache.get(anime_slug)
    if cached and now - _anime_stats_cache_times.get(anime_slug, 0) < _anime_stats_cache_ttl:
        return cached
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
        SELECT r.id, r.username, r.rating, r.comment, r.created_at, r.user_id,
               u.avatar, u.avatar_color
        FROM reviews r
        LEFT JOIN users u ON u.id = r.user_id
        WHERE r.anime_slug=?
        ORDER BY r.id DESC
        """,
        (anime_slug,)
    )
    reviews = [
        {
            "username": row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "avatar": row["avatar"] if "avatar" in row.keys() else None,
            "avatar_color": row["avatar_color"] if "avatar_color" in row.keys() else None,
        }
        for row in cursor.fetchall()
    ]
    result = {
        "average": average,
        "votes": total_votes,
        "breakdown": breakdown,
        "reviews": reviews,
    }
    _anime_stats_cache[anime_slug] = result
    _anime_stats_cache_times[anime_slug] = time.time()
    return result


def _invalidate_anime_stats_cache(anime_slug):
    """Drop cached stats so new/deleted reviews show up immediately."""
    _anime_stats_cache.pop(anime_slug, None)
    _anime_stats_cache_times.pop(anime_slug, None)


def add_review(anime_slug, username, rating, comment, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO reviews (anime_slug, username, rating, comment, user_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (anime_slug, username or "Anonymous", rating, comment or "", user_id)
    )
    conn.commit()
    conn.close()
    _invalidate_anime_stats_cache(anime_slug)


def get_all_reviews(limit=200):
    """Return the most recent reviews across ALL anime (for the /reviews page)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.id, r.anime_slug, r.username, r.rating, r.comment,
               r.created_at, r.user_id, u.avatar, u.avatar_color
        FROM reviews r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (limit,)
    )
    reviews = [
        {
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "username": row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "avatar": row["avatar"] if "avatar" in row.keys() else None,
            "avatar_color": row["avatar_color"] if "avatar_color" in row.keys() else None,
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return reviews


def get_user_review(anime_slug, user_id):
    """Return the user's existing review for an anime, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, rating, comment, created_at FROM reviews "
        "WHERE anime_slug=? AND user_id=? LIMIT 1",
        (anime_slug, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "rating": row["rating"],
        "comment": row["comment"] or "",
        "created_at": row["created_at"],
    }


def delete_user_review(review_id, user_id):
    """Delete a review only if it belongs to the given user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT anime_slug FROM reviews WHERE id=? AND user_id=?",
        (review_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        return False
    cursor.execute(
        "DELETE FROM reviews WHERE id=? AND user_id=?",
        (review_id, user_id),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        _invalidate_anime_stats_cache(row["anime_slug"])
    return deleted


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
    result = {
        "average": average,
        "votes": total_votes,
        "breakdown": breakdown,
        "reviews": reviews,
    }
    _anime_stats_cache[anime_slug] = result
    _anime_stats_cache_times[anime_slug] = time.time()
    return result


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


def ensure_and_get_lists(user_id):
    """Seed default lists if needed AND return them — one DB connection.

    The old path opened two separate Turso connections (ensure → close →
    get → close), costing ~1.6 s of round-trip latency on every list
    picker open. This fuses them into a single connection.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] == 0:
        for i in range(1, MAX_USER_LISTS + 1):
            cursor.execute(
                "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
                (user_id, f"List {i}"),
            )
        conn.commit()

    # Now fetch lists in the same connection.
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
    """Add an anime to a list owned by the user.

    Returns True if newly added, False if it was already in the list, or
    None if the list doesn't exist / isn't owned by the user.

    The ownership check is folded into the INSERT (SELECT ... WHERE EXISTS)
    so the common case costs one remote round trip instead of three.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_list_items (list_id, anime_slug)
        SELECT ?, ?
        WHERE EXISTS (SELECT 1 FROM user_lists WHERE id = ? AND user_id = ?)
        """,
        (list_id, anime_slug, list_id, user_id),
    )
    inserted = cursor.rowcount
    if inserted == 0:
        # rowcount 0 means duplicate row OR unowned list; disambiguate once.
        cursor.execute(
            "SELECT id FROM user_lists WHERE id = ? AND user_id = ?",
            (list_id, user_id),
        )
        if not cursor.fetchone():
            conn.close()
            return None
    _touch_list(list_id, cursor)
    conn.commit()
    conn.close()
    return bool(inserted)


def remove_from_user_list(list_id, user_id, anime_slug):
    """Remove an anime from a list the user owns. Returns True when a row
    was actually deleted, False otherwise (not in the list or not owned)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM user_list_items
        WHERE anime_slug = ?
          AND list_id IN (SELECT id FROM user_lists WHERE id = ? AND user_id = ?)
        """,
        (anime_slug, list_id, user_id),
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


def get_taste_slugs(user_id, limit=80):
    """Slugs the user has shown interest in, most recent first.

    Merges view history with everything saved in their lists in a single
    query/connection so the homepage stays one round trip. List entries
    count as a stronger signal than a passive view, which the caller uses
    to weight genres.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT anime_slug, MAX(at) AS at, MAX(saved) AS saved FROM (
            SELECT h.anime_slug AS anime_slug, h.viewed_at AS at, 0 AS saved
            FROM view_history h
            WHERE h.user_id = ?
            UNION ALL
            SELECT i.anime_slug AS anime_slug, i.added_at AS at, 1 AS saved
            FROM user_list_items i
            JOIN user_lists l ON l.id = i.list_id
            WHERE l.user_id = ?
        )
        GROUP BY anime_slug
        ORDER BY at DESC
        LIMIT ?
        """,
        (user_id, user_id, limit),
    )
    rows = [{"slug": r["anime_slug"], "saved": bool(r["saved"])} for r in cursor.fetchall()]
    conn.close()
    return rows


_query_cache = {}
_query_cache_time = {}
_QUERY_CACHE_TTL = 60  # seconds

def _cached_query(key, sql, params=(), one=False):
    """Run a query with a short TTL cache to avoid repeated Turso round-trips."""
    now = time.time()
    if key in _query_cache and now - _query_cache_time.get(key, 0) < _QUERY_CACHE_TTL:
        return _query_cache[key]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    result = cursor.fetchone() if one else cursor.fetchall()
    _query_cache[key] = result
    _query_cache_time[key] = now
    return result

def get_history_count(user_id):
    """Fast COUNT(*) for the profile header."""
    row = _cached_query(
        f"hcount:{user_id}",
        "SELECT COUNT(*) AS n FROM view_history WHERE user_id = ?",
        (user_id,),
        one=True,
    )
    return row["n"] if row else 0


def get_view_history(user_id, limit=60):
    key = f"vhist:{user_id}:{limit}"
    now = time.time()
    if key in _query_cache and now - _query_cache_time.get(key, 0) < _QUERY_CACHE_TTL:
        return _query_cache[key]
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
    _query_cache[key] = rows
    _query_cache_time[key] = now
    return rows


def get_all_community_member_counts():
    """Return {slug: count} for every community that has members."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT anime_slug, COUNT(*) AS cnt FROM community_members GROUP BY anime_slug"
    )
    result = {row["anime_slug"]: row["cnt"] for row in cursor.fetchall()}
    conn.close()
    return result


# ------------------------------------------------------------------
#  Site-wide real stats (for homepage / community hero sections)
# ------------------------------------------------------------------

def get_site_stats():
    """Return real counts: total_users, total_messages, total_communities."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM chat_messages")
    total_messages = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT anime_slug) FROM chat_messages")
    total_communities = cursor.fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "total_messages": total_messages,
        "total_communities": total_communities,
    }


def get_community_chat_stats(anime_slug):
    """Return real member + message counts for one community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE anime_slug = ?",
        (anime_slug,),
    )
    message_count = cursor.fetchone()[0]
    conn.close()
    member_count = get_community_member_count(anime_slug)
    return {"message_count": message_count, "member_count": member_count}

def insert_system_message(anime_slug, content, user_id=1):
    """Insert a system message (e.g. 'X joined the community') into chat."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content)
        VALUES (?, ?, 'System', '#6b7280', 'system', ?)
        """,
        (anime_slug, user_id, content),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


# ================================================================
#  RANKINGS — Show & Episode Leaderboards
# ================================================================

def get_show_rankings(limit=50, genre=None, season=None):
    """Return top anime ranked by average episode rating."""
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT
            er.anime_slug,
            COUNT(DISTINCT er.id) AS total_reviews,
            AVG(er.rating) AS avg_rating
        FROM episode_reviews er
        GROUP BY er.anime_slug
        HAVING total_reviews >= 1
    """
    params = []
    if genre:
        query += " HAVING total_reviews >= 1 AND er.anime_slug IN (SELECT slug FROM anime_list WHERE genre LIKE ?)"
        params.append(f"%{genre}%")

    query += " ORDER BY avg_rating DESC, total_reviews DESC"
    query += " LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        slug = row["anime_slug"]
        entry = anime_database.get(slug)
        if not entry:
            continue
        results.append({
            "slug": slug,
            "title": entry.get("title") or slug,
            "image": entry.get("image") or "",
            "genre": entry.get("genre") or "",
            "rating": entry.get("rating") or "N/A",
            "avg_rating": round(row["avg_rating"], 1),
            "total_reviews": row["total_reviews"],
        })
    conn.close()
    return results


def get_episode_rankings_for_anime(anime_slug, limit=20):
    """Return top-rated episodes for a specific anime."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT season_name, episode_number, AVG(rating) AS avg_rating, COUNT(*) AS total_votes
        FROM episode_reviews
        WHERE anime_slug = ?
        GROUP BY season_name, episode_number
        HAVING total_votes >= 1
        ORDER BY avg_rating DESC, total_votes DESC
        LIMIT ?""",
        (anime_slug, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "season_name": r["season_name"],
            "episode_number": r["episode_number"],
            "avg_rating": round(r["avg_rating"], 1),
            "total_votes": r["total_votes"],
        }
        for r in rows
    ]


def get_user_episode_rating(user_id, anime_slug, season_name, episode_number):
    """Return the user's existing rating for an episode, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT rating FROM episode_reviews
        WHERE user_id=? AND anime_slug=? AND season_name=? AND episode_number=?""",
        (user_id, anime_slug, season_name, episode_number),
    )
    row = cursor.fetchone()
    conn.close()
    return row["rating"] if row else None


def rate_episode(user_id, username, avatar_color, anime_slug, season_name, episode_number, rating):
    """Insert or update a user's episode rating."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id FROM episode_reviews
        WHERE user_id=? AND anime_slug=? AND season_name=? AND episode_number=?""",
        (user_id, anime_slug, season_name, episode_number),
    )
    existing = cursor.fetchone()
    if existing:
        cursor.execute(
            "UPDATE episode_reviews SET rating=? WHERE id=?",
            (rating, existing["id"]),
        )
    else:
        cursor.execute(
            """INSERT INTO episode_reviews
            (anime_slug, season_name, episode_number, user_id, username, avatar_color, rating)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (anime_slug, season_name, episode_number, user_id, username, avatar_color, rating),
        )
    conn.commit()
    conn.close()


# ================================================================
#  XP & RANK SYSTEM
# ================================================================

# Rank thresholds: XP required to reach each rank
RANK_THRESHOLDS = {
    "F": -1,    # Below 0 XP
    "D": 0,     # New users start here
    "C": 100,
    "B": 500,
    "A": 1500,
    "S": 5000,
    "S+": 15000,
}

def get_xp_tier(xp):
    """Return the rank tier string for a given XP value."""
    if xp >= 15000:
        return "S+"
    elif xp >= 5000:
        return "S"
    elif xp >= 1500:
        return "A"
    elif xp >= 500:
        return "B"
    elif xp >= 100:
        return "C"
    elif xp >= 0:
        return "D"
    else:
        return "F"

# Rank boundaries: (lower_threshold, upper_threshold)
_RANK_RANGES = {
    "F": (-999, 0),
    "D": (0, 100),
    "C": (100, 500),
    "B": (500, 1500),
    "A": (1500, 5000),
    "S": (5000, 15000),
    "S+": (15000, 15000),
}

def xp_progress(xp):
    """Return (rank, progress_pct) where progress is 0-100 toward the next rank."""
    rank = get_xp_tier(xp)
    lo, hi = _RANK_RANGES.get(rank, (0, 100))
    if hi <= lo:
        return rank, 100
    pct = int(min(100, max(0, (xp - lo) / (hi - lo) * 100)))
    return rank, pct


def get_user_xp(user_id):
    """Get a user's current XP. Returns 0 if no record exists."""
    row = _cached_query(
        f"xp:{user_id}",
        "SELECT xp FROM user_xp WHERE user_id=?",
        (user_id,),
        one=True,
    )
    return row["xp"] if row else 0


def get_user_rank(user_id):
    """Return the rank tier string for a user."""
    return get_xp_tier(get_user_xp(user_id))


def add_xp(user_id, amount):
    """Add (or subtract) XP for a user. Creates record if needed."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT xp FROM user_xp WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row:
        new_xp = row["xp"] + amount
        cursor.execute("UPDATE user_xp SET xp=? WHERE user_id=?", (new_xp, user_id))
    else:
        cursor.execute("INSERT INTO user_xp (user_id, xp) VALUES (?, ?)", (user_id, amount))
    conn.commit()
    conn.close()


def get_all_user_ranks(user_ids):
    """Return {user_id: {xp, rank}} for a list of user IDs.

    Every requested user gets an entry -- those without a user_xp row
    default to 0 XP / rank D so badges always render.
    """
    if not user_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(user_ids))
    cursor.execute(f"SELECT user_id, xp FROM user_xp WHERE user_id IN ({placeholders})", user_ids)
    xp_map = {row["user_id"]: row["xp"] for row in cursor.fetchall()}
    conn.close()
    result = {}
    for uid in user_ids:
        xp = xp_map.get(uid, 0)
        result[uid] = {"xp": xp, "rank": get_xp_tier(xp)}
    return result


def toggle_review_like(user_id, review_type, review_id, is_like):
    """Toggle a like/dislike on a review. Returns (new_is_like, removed)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type=? AND review_id=?",
        (user_id, review_type, review_id),
    )
    existing = cursor.fetchone()

    # Find the review author to adjust XP
    review_author_id = None
    if review_type == "episode":
        cursor.execute("SELECT user_id FROM episode_reviews WHERE id=?", (review_id,))
        r = cursor.fetchone()
        if r:
            review_author_id = r["user_id"]
    elif review_type == "anime":
        cursor.execute("SELECT user_id FROM anime_ratings WHERE id=?", (review_id,))
        r = cursor.fetchone()
        if r:
            review_author_id = r["user_id"]

    removed = False
    new_is_like = is_like

    if existing:
        if existing["is_like"] == is_like:
            # Same vote → remove it
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
            removed = True
            if review_author_id:
                add_xp(review_author_id, 10 if is_like else 5)  # Undo: reverse the penalty/bonus
        else:
            # Different vote → switch
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (is_like, existing["id"]))
            if review_author_id:
                add_xp(review_author_id, 15 if is_like else -15)  # Swing from dislike to like or vice versa
    else:
        # New vote
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like) VALUES (?, ?, ?, ?)",
            (user_id, review_type, review_id, is_like),
        )
        if review_author_id:
            add_xp(review_author_id, 10 if is_like else -5)

    conn.commit()
    conn.close()
    return new_is_like, removed


def get_review_likes(review_type, review_id):
    """Return {likes: N, dislikes: N, user_vote: 1|-1|0} for a review."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type=? AND review_id=?",
        (review_type, review_id),
    )
    row = cursor.fetchone()
    conn.close()
    return {
        "likes": row["likes"] or 0,
        "dislikes": row["dislikes"] or 0,
    }


def get_bulk_review_likes(review_type, review_ids):
    """Return {review_id: {likes, dislikes}} for multiple reviews."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT review_id,
        SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM review_likes WHERE review_type=? AND review_id IN ({placeholders})
        GROUP BY review_id""",
        [review_type] + list(review_ids),
    )
    result = {}
    for row in cursor.fetchall():
        result[row["review_id"]] = {"likes": row["likes"] or 0, "dislikes": row["dislikes"] or 0}
    conn.close()
    return result


def get_user_review_history(user_id, limit=50):
    """Return all reviews by a user (episode + anime)."""
    conn = get_connection()
    cursor = conn.cursor()
    reviews = []
    # Episode reviews
    cursor.execute(
        "SELECT id, anime_slug, season_name, episode_number, rating, comment, created_at "
        "FROM episode_reviews WHERE user_id=? ORDER BY id DESC LIMIT ?",
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
    # Anime reviews
    cursor.execute(
        "SELECT id, anime_slug, rating, comment, created_at "
        "FROM anime_ratings WHERE user_id=? ORDER BY id DESC LIMIT ?",
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


def set_profile_public(user_id, is_public):
    """Set a user profile visibility."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_public=? WHERE id=?", (1 if is_public else 0, user_id))
    conn.commit()
    conn.close()
