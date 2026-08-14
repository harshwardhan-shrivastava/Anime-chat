"""
Threads — the NEW messaging engine for AnimeChat.

Completely separate from the legacy chat system (chat.py / chat_messages /
chat_presence). Everything lives in its own `thr_*` tables inside the same
animechat.db file, so the old system keeps working untouched.

One unified `thr_messages` table serves all three contexts:
    context_type = 'dm'      -> context_id = thr_conversations.id
    context_type = 'group'   -> context_id = thr_conversations.id
    context_type = 'channel' -> context_id = thr_channels.id   (Communities, Phase 2)
Edit / pin / thread / mention / typing logic is shared across all of them.
"""

import os
import re
import sqlite3
import time
from datetime import datetime, timezone

DATABASE = "animechat.db"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{3,20})")

CONTEXT_TYPES = ("dm", "group", "channel")
ROLES = ("owner", "admin", "member")
MESSAGE_KINDS = ("text", "gif", "image", "video", "system")


# ---------------------------------------------------------------------------
# Connection / schema
# ---------------------------------------------------------------------------

def get_connection():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


def _utcnow():
    """Second-precision UTC timestamp, matching SQLite CURRENT_TIMESTAMP."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def create_tables():
    """Create every Threads table. Idempotent — safe to call on every boot."""
    conn = get_connection()
    cur = conn.cursor()

    # ---- Conversations (DMs + group chats) --------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_conversations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL CHECK(type IN ('dm','group')),
            name TEXT,
            avatar_color TEXT,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_activity_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_conversation_members(
            conversation_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT DEFAULT 'member' CHECK(role IN ('owner','admin','member')),
            last_read_message_id INTEGER DEFAULT 0,
            muted INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (conversation_id, user_id),
            FOREIGN KEY (conversation_id) REFERENCES thr_conversations(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # ---- Unified message engine -------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_type TEXT NOT NULL CHECK(context_type IN ('dm','group','channel')),
            context_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            kind TEXT DEFAULT 'text' CHECK(kind IN ('text','gif','image','video','system')),
            content TEXT NOT NULL DEFAULT '',
            attachment_url TEXT,
            attachment_preview TEXT,
            parent_message_id INTEGER,
            is_pinned INTEGER DEFAULT 0,
            pinned_at TEXT,
            edited_at TEXT,
            deleted_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (sender_id) REFERENCES users(id),
            FOREIGN KEY (parent_message_id) REFERENCES thr_messages(id)
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_thr_messages_ctx "
        "ON thr_messages(context_type, context_id, id)"
    )

    # ---- @mentions ----------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_mentions(
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, user_id)
        )
    """)

    # ---- Typing indicators --------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_typing(
            context_type TEXT NOT NULL,
            context_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_typed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (context_type, context_id, user_id)
        )
    """)

    # ---- Presence ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_presence(
            user_id INTEGER PRIMARY KEY,
            status TEXT DEFAULT 'online',
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---- Per-user settings ---------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_user_settings(
            user_id INTEGER PRIMARY KEY,
            read_receipts INTEGER DEFAULT 1,
            typing_indicators INTEGER DEFAULT 1
        )
    """)

    # ---- Communities (Phase 2 — tables created now so no migration later) ---
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_communities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            slug TEXT NOT NULL UNIQUE,
            description TEXT,
            genre TEXT,
            icon_color TEXT,
            is_public INTEGER DEFAULT 1,
            owner_id INTEGER NOT NULL,
            rules TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_community_members(
            community_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT DEFAULT 'member' CHECK(role IN ('owner','moderator','member')),
            muted INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (community_id, user_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_channels(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            topic TEXT,
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_channel_reads(
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            last_read_message_id INTEGER DEFAULT 0,
            PRIMARY KEY (channel_id, user_id)
        )
    """)

    # ---- Polls ---------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_polls(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            question TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            multiple_choice INTEGER DEFAULT 0,
            closes_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_poll_options(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            text TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_poll_votes(
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (poll_id, user_id)
        )
    """)

    # ---- Watch parties ---------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_watch_parties(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id INTEGER NOT NULL,
            anime_id TEXT NOT NULL,
            host_user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            scheduled_time TEXT NOT NULL,
            announced INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_watch_party_rsvps(
            party_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (party_id, user_id)
        )
    """)

    # ---- Moderation ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_mod_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            community_id INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            target_user_id INTEGER,
            target_message_id INTEGER,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_community_bans(
            community_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            banned_by INTEGER,
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (community_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_user_blocks(
            user_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, blocked_id)
        )
    """)

    # ---- Notifications -----------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            context_type TEXT,
            context_id INTEGER,
            message_id INTEGER,
            from_user_id INTEGER,
            read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# User search (reuses the users table through database.py)
# ---------------------------------------------------------------------------

def search_users(query, exclude_id, limit=10):
    from database import get_connection as _site_conn

    conn = _site_conn()
    cur = conn.cursor()
    q = f"%{query.strip()}%"
    cur.execute(
        """
        SELECT id, username, avatar_color
        FROM users
        WHERE (username LIKE ? OR email LIKE ?) AND id != ?
        ORDER BY username ASC
        LIMIT ?
        """,
        (q, q, exclude_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def _conversation_exists(cur, conv_id):
    cur.execute("SELECT id FROM thr_conversations WHERE id = ?", (conv_id,))
    return cur.fetchone() is not None


def get_or_create_dm(user_id, other_id):
    """Find the existing 2-person DM between the two users, or create it."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id FROM thr_conversations c
        JOIN thr_conversation_members a ON a.conversation_id = c.id AND a.user_id = ?
        JOIN thr_conversation_members b ON b.conversation_id = c.id AND b.user_id = ?
        WHERE c.type = 'dm'
        """,
        (user_id, other_id),
    )
    row = cur.fetchone()
    if row:
        conv_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO thr_conversations (type, created_by) VALUES ('dm', ?)",
            (user_id,),
        )
        conv_id = cur.lastrowid
        cur.execute(
            "INSERT INTO thr_conversation_members (conversation_id, user_id, role) VALUES (?, ?, 'member')",
            (conv_id, user_id),
        )
        cur.execute(
            "INSERT INTO thr_conversation_members (conversation_id, user_id, role) VALUES (?, ?, 'member')",
            (conv_id, other_id),
        )
    conn.commit()
    conn.close()
    return conv_id


def create_group(name, created_by, member_ids, avatar_color=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO thr_conversations (type, name, avatar_color, created_by) VALUES ('group', ?, ?, ?)",
        (name, avatar_color or "#8b5cf6", created_by),
    )
    conv_id = cur.lastrowid
    members = [created_by] + [m for m in member_ids if m != created_by]
    for uid in members:
        cur.execute(
            "INSERT OR IGNORE INTO thr_conversation_members (conversation_id, user_id, role) VALUES (?, ?, 'member')",
            (conv_id, uid),
        )
    cur.execute(
        "UPDATE thr_conversation_members SET role = 'owner' WHERE conversation_id = ? AND user_id = ?",
        (conv_id, created_by),
    )
    conn.commit()
    conn.close()
    return conv_id


def get_conversation(conv_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_conversations WHERE id = ?", (conv_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def is_conversation_member(conv_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM thr_conversation_members WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def get_conversation_members(conv_id):
    """Members of a conversation with their user fields and read state."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username, u.avatar_color,
               cm.role, cm.muted, cm.joined_at, cm.last_read_message_id
        FROM thr_conversation_members cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.conversation_id = ?
        ORDER BY cm.joined_at ASC
        """,
        (conv_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_user_conversations(user_id):
    """Left-panel list: every conversation the user is in, newest activity
    first, with last-message preview, unread count and mute state."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.type, c.name, c.avatar_color, c.last_activity_at,
               cm.role, cm.muted, cm.last_read_message_id,
               (SELECT COUNT(*) FROM thr_messages m
                 WHERE m.context_type = c.type AND m.context_id = c.id
                   AND m.id > cm.last_read_message_id
                   AND m.sender_id != ?) AS unread,
               (SELECT m.content FROM thr_messages m
                 WHERE m.context_type = c.type AND m.context_id = c.id
                   AND m.deleted_at IS NULL
                 ORDER BY m.id DESC LIMIT 1) AS last_content,
               (SELECT m.kind FROM thr_messages m
                 WHERE m.context_type = c.type AND m.context_id = c.id
                 ORDER BY m.id DESC LIMIT 1) AS last_kind,
               (SELECT m.sender_id FROM thr_messages m
                 WHERE m.context_type = c.type AND m.context_id = c.id
                 ORDER BY m.id DESC LIMIT 1) AS last_sender_id,
               (SELECT m.created_at FROM thr_messages m
                 WHERE m.context_type = c.type AND m.context_id = c.id
                 ORDER BY m.id DESC LIMIT 1) AS last_at
        FROM thr_conversations c
        JOIN thr_conversation_members cm
          ON cm.conversation_id = c.id AND cm.user_id = ?
        WHERE c.id IN (SELECT conversation_id FROM thr_conversation_members WHERE user_id = ?)
        ORDER BY c.last_activity_at DESC, c.id DESC
        """,
        (user_id, user_id, user_id),
    )
    convs = [dict(r) for r in cur.fetchall()]

    # Attach the other party for DMs and a member summary for groups.
    out = []
    for c in convs:
        item = {
            "id": c["id"],
            "type": c["type"],
            "name": c["name"],
            "avatar_color": c["avatar_color"],
            "last_activity_at": c["last_activity_at"],
            "role": c["role"],
            "muted": bool(c["muted"]),
            "unread": c["unread"] or 0,
            "last_message": {
                "content": c["last_content"] or "",
                "kind": c["last_kind"] or "",
                "sender_id": c["last_sender_id"],
                "created_at": c["last_at"],
            },
        }
        if c["type"] == "dm":
            cur.execute(
                """
                SELECT u.id, u.username, u.avatar_color
                FROM thr_conversation_members cm
                JOIN users u ON u.id = cm.user_id
                WHERE cm.conversation_id = ? AND cm.user_id != ?
                """,
                (c["id"], user_id),
            )
            other = cur.fetchone()
            item["other"] = dict(other) if other else None
        else:
            cur.execute(
                """
                SELECT u.id, u.username, u.avatar_color
                FROM thr_conversation_members cm
                JOIN users u ON u.id = cm.user_id
                WHERE cm.conversation_id = ?
                ORDER BY cm.joined_at ASC
                """,
                (c["id"],),
            )
            item["members"] = [dict(r) for r in cur.fetchall()]
        out.append(item)
    conn.close()
    return out


def add_conversation_member(conv_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO thr_conversation_members (conversation_id, user_id, role) VALUES (?, ?, 'member')",
        (conv_id, user_id),
    )
    conn.commit()
    conn.close()


def remove_conversation_member(conv_id, user_id):
    """Leave a group. If the leaver was owner, promote the earliest remaining
    member. If no members remain, delete the conversation."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_conversation_members WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    role = row["role"]
    cur.execute(
        "DELETE FROM thr_conversation_members WHERE conversation_id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    cur.execute(
        "SELECT user_id FROM thr_conversation_members WHERE conversation_id = ? ORDER BY joined_at ASC LIMIT 1",
        (conv_id,),
    )
    nxt = cur.fetchone()
    if role == "owner" and nxt:
        cur.execute(
            "UPDATE thr_conversation_members SET role = 'owner' WHERE conversation_id = ? AND user_id = ?",
            (conv_id, nxt["user_id"]),
        )
    if nxt is None:
        cur.execute("DELETE FROM thr_conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()
    return True


def rename_conversation(conv_id, name):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE thr_conversations SET name = ? WHERE id = ?", (name, conv_id))
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok


def set_conversation_muted(conv_id, user_id, muted):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_conversation_members SET muted = ? WHERE conversation_id = ? AND user_id = ?",
        (1 if muted else 0, conv_id, user_id),
    )
    conn.commit()
    conn.close()


def touch_conversation(conv_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_conversations SET last_activity_at = ? WHERE id = ?",
        (_utcnow(), conv_id),
    )
    conn.commit()
    conn.close()


def mark_conversation_read(conv_id, user_id, message_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_conversation_members SET last_read_message_id = ? WHERE conversation_id = ? AND user_id = ?",
        (message_id, conv_id, user_id),
    )
    conn.commit()
    conn.close()


def set_conversation_role(conv_id, user_id, role):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_conversation_members SET role = ? WHERE conversation_id = ? AND user_id = ?",
        (role, conv_id, user_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Messages — the unified engine
# ---------------------------------------------------------------------------

def parse_mentions(content):
    """Usernames mentioned in content via @username."""
    return set(MENTION_RE.findall(content or ""))


def add_message(context_type, context_id, sender_id, kind="text", content="",
                attachment_url=None, attachment_preview=None,
                parent_message_id=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_messages
        (context_type, context_id, sender_id, kind, content,
         attachment_url, attachment_preview, parent_message_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (context_type, context_id, sender_id, kind, content or "",
         attachment_url, attachment_preview, parent_message_id),
    )
    msg_id = cur.lastrowid

    if context_type in ("dm", "group"):
        cur.execute(
            "UPDATE thr_conversations SET last_activity_at = ? WHERE id = ?",
            (_utcnow(), context_id),
        )

    # @mentions
    mentioned = []
    if kind == "text" and content:
        names = parse_mentions(content)
        if names:
            placeholders = ",".join("?" for _ in names)
            cur.execute(
                f"SELECT id, username FROM users WHERE username IN ({placeholders})",
                tuple(names),
            )
            for row in cur.fetchall():
                if row["id"] != sender_id:
                    mentioned.append(row["id"])
                    cur.execute(
                        "INSERT OR IGNORE INTO thr_mentions (message_id, user_id) VALUES (?, ?)",
                        (msg_id, row["id"]),
                    )
                    cur.execute(
                        """
                        INSERT INTO thr_notifications
                        (user_id, type, context_type, context_id, message_id, from_user_id)
                        VALUES (?, 'mention', ?, ?, ?, ?)
                        """,
                        (row["id"], context_type, context_id, msg_id, sender_id),
                    )

    # DM / reply notifications
    if context_type == "dm":
        cur.execute(
            """
            INSERT INTO thr_notifications
            (user_id, type, context_type, context_id, message_id, from_user_id)
            SELECT user_id, 'dm', ?, ?, ?, ?
            FROM thr_conversation_members
            WHERE conversation_id = ? AND user_id != ?
            """,
            (context_type, context_id, msg_id, sender_id, context_id, sender_id),
        )
    elif parent_message_id:
        cur.execute(
            "SELECT sender_id FROM thr_messages WHERE id = ?", (parent_message_id,)
        )
        parent = cur.fetchone()
        if parent and parent["sender_id"] != sender_id:
            cur.execute(
                """
                INSERT INTO thr_notifications
                (user_id, type, context_type, context_id, message_id, from_user_id)
                VALUES (?, 'reply', ?, ?, ?, ?)
                """,
                (parent["sender_id"], context_type, context_id, msg_id, sender_id),
            )

    conn.commit()
    cur.execute("SELECT * FROM thr_messages WHERE id = ?", (msg_id,))
    row = dict(cur.fetchone())
    conn.close()
    return row


def get_message(msg_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_messages WHERE id = ?", (msg_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_messages(context_type, context_id, before_id=None, limit=60):
    """History: the last `limit` messages, newest first in SQL, returned
    oldest-first."""
    conn = get_connection()
    cur = conn.cursor()
    if before_id:
        cur.execute(
            """
            SELECT * FROM (
                SELECT * FROM thr_messages
                WHERE context_type = ? AND context_id = ? AND id < ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (context_type, context_id, before_id, limit),
        )
    else:
        cur.execute(
            """
            SELECT * FROM (
                SELECT * FROM thr_messages
                WHERE context_type = ? AND context_id = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (context_type, context_id, limit),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_messages_after(context_type, context_id, after_id, limit=200):
    """Incremental poll: every message newer than after_id, oldest first."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM thr_messages
        WHERE context_type = ? AND context_id = ? AND id > ?
        ORDER BY id ASC LIMIT ?
        """,
        (context_type, context_id, after_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_pinned_messages(context_type, context_id, limit=50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM thr_messages
        WHERE context_type = ? AND context_id = ? AND is_pinned = 1
          AND deleted_at IS NULL
        ORDER BY pinned_at DESC LIMIT ?
        """,
        (context_type, context_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def edit_message(msg_id, user_id, content):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE thr_messages
        SET content = ?, edited_at = ?
        WHERE id = ? AND sender_id = ? AND deleted_at IS NULL
        """,
        (content, _utcnow(), msg_id, user_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def soft_delete_message(msg_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_messages SET deleted_at = ? WHERE id = ? AND sender_id = ?",
        (_utcnow(), msg_id, user_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def toggle_pin(msg_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_messages SET is_pinned = 1 - is_pinned, pinned_at = ? "
        "WHERE id = ? AND deleted_at IS NULL",
        (_utcnow(), msg_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def can_access_context(context_type, context_id, user_id):
    """Membership check shared by every message route (Phase 2 extends this
    to channels via community membership)."""
    if context_type in ("dm", "group"):
        return is_conversation_member(context_id, user_id)
    if context_type == "channel":
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT 1 FROM thr_channels ch
            JOIN thr_community_members m ON m.community_id = ch.community_id
            WHERE ch.id = ? AND m.user_id = ?
            """,
            (context_id, user_id),
        )
        ok = cur.fetchone() is not None
        conn.close()
        return ok
    return False


# ---------------------------------------------------------------------------
# Typing indicators
# ---------------------------------------------------------------------------

def set_typing(context_type, context_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_typing (context_type, context_id, user_id, last_typed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(context_type, context_id, user_id)
        DO UPDATE SET last_typed_at = excluded.last_typed_at
        """,
        (context_type, context_id, user_id, _utcnow()),
    )
    conn.commit()
    conn.close()


def get_typing_users(context_type, context_id, exclude_user_id, within_seconds=8):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username FROM thr_typing t
        JOIN users u ON u.id = t.user_id
        WHERE t.context_type = ? AND t.context_id = ? AND t.user_id != ?
          AND datetime(t.last_typed_at) >= datetime('now', ?)
        """,
        (context_type, context_id, exclude_user_id, f"-{within_seconds} seconds"),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def touch_presence(user_id, status="online"):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_presence (user_id, status, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET last_seen = excluded.last_seen,
                      status = CASE WHEN excluded.status = 'away' THEN 'away'
                                    ELSE thr_presence.status END
        """,
        (user_id, status, _utcnow()),
    )
    conn.commit()
    conn.close()


def get_presence(user_ids):
    """user_id -> {status, online} — online means seen within 60s."""
    if not user_ids:
        return {}
    conn = get_connection()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in user_ids)
    cur.execute(
        f"""
        SELECT user_id, status, last_seen,
               CASE WHEN datetime(last_seen) >= datetime('now', '-60 seconds')
                    THEN 1 ELSE 0 END AS online
        FROM thr_presence
        WHERE user_id IN ({placeholders})
        """,
        tuple(user_ids),
    )
    out = {}
    for r in cur.fetchall():
        out[r["user_id"]] = {
            "status": r["status"] if r["online"] else "offline",
            "online": bool(r["online"]),
        }
    conn.close()
    return out


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {"read_receipts": 1, "typing_indicators": 1}


def get_settings(user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_user_settings WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return dict(DEFAULT_SETTINGS)
    return {
        "read_receipts": bool(row["read_receipts"]),
        "typing_indicators": bool(row["typing_indicators"]),
    }


def save_settings(user_id, read_receipts=None, typing_indicators=None):
    cur_settings = get_settings(user_id)
    rr = int(read_receipts) if read_receipts is not None else int(cur_settings["read_receipts"])
    ti = int(typing_indicators) if typing_indicators is not None else int(cur_settings["typing_indicators"])
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_user_settings (user_id, read_receipts, typing_indicators)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET read_receipts = excluded.read_receipts,
                      typing_indicators = excluded.typing_indicators
        """,
        (user_id, rr, ti),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def get_notifications(user_id, limit=30):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT n.*, u.username AS from_username, u.avatar_color AS from_color
        FROM thr_notifications n
        LEFT JOIN users u ON u.id = n.from_user_id
        WHERE n.user_id = ?
        ORDER BY n.id DESC LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def unread_notification_count(user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM thr_notifications WHERE user_id = ? AND read = 0",
        (user_id,),
    )
    n = cur.fetchone()["n"]
    conn.close()
    return n


def mark_notifications_read(user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_notifications SET read = 1 WHERE user_id = ?", (user_id,)
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Threads helpers (Phase 2 — available now)
# ---------------------------------------------------------------------------

def get_thread_message_counts(parent_id):
    """Replies count for a message thread."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM thr_messages WHERE parent_message_id = ? AND deleted_at IS NULL",
        (parent_id,),
    )
    n = cur.fetchone()["n"]
    conn.close()
    return n