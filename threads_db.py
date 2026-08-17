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

USERNAME_RE = re.compile(r"^.{1,100}$")
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
    cur.execute("""
        CREATE TABLE IF NOT EXISTS thr_reports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            reporter_id INTEGER NOT NULL,
            reason TEXT,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (message_id) REFERENCES thr_messages(id)
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_thr_reports_status ON thr_reports(status)")

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


def get_messages(context_type, context_id, before_id=None, limit=60, exclude_user_ids=None):
    """History: the last `limit` messages, newest first in SQL, returned
    oldest-first. Pass exclude_user_ids (e.g. people you blocked) to hide
    their messages."""
    block_sql = ""
    block_args = ()
    if exclude_user_ids:
        placeholders = ",".join("?" for _ in exclude_user_ids)
        block_sql = f" AND sender_id NOT IN ({placeholders})"
        block_args = tuple(exclude_user_ids)
    conn = get_connection()
    cur = conn.cursor()
    if before_id:
        cur.execute(
            """
            SELECT * FROM (
                SELECT * FROM thr_messages
                WHERE context_type = ? AND context_id = ? AND id < ?%s
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """ % block_sql,
            (context_type, context_id, before_id) + block_args + (limit,),
        )
    else:
        cur.execute(
            """
            SELECT * FROM (
                SELECT * FROM thr_messages
                WHERE context_type = ? AND context_id = ?%s
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """ % block_sql,
            (context_type, context_id) + block_args + (limit,),
        )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def get_messages_after(context_type, context_id, after_id, limit=200, exclude_user_ids=None):
    """Incremental poll: every message newer than after_id, oldest first."""
    block_sql = ""
    block_args = ()
    if exclude_user_ids:
        placeholders = ",".join("?" for _ in exclude_user_ids)
        block_sql = f" AND sender_id NOT IN ({placeholders})"
        block_args = tuple(exclude_user_ids)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT * FROM thr_messages
        WHERE context_type = ? AND context_id = ? AND id > ?%s
        ORDER BY id ASC LIMIT ?
        """ % block_sql,
        (context_type, context_id, after_id) + block_args + (limit,),
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
        # Membership required AND not banned from the community.
        cur.execute(
            """
            SELECT 1 FROM thr_channels ch
            JOIN thr_community_members m ON m.community_id = ch.community_id
            LEFT JOIN thr_community_bans b
              ON b.community_id = ch.community_id AND b.user_id = ?
            WHERE ch.id = ? AND m.user_id = ? AND b.user_id IS NULL
            """,
            (user_id, context_id, user_id),
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
# ===========================================================================
# Communities (Phase 2)
# ===========================================================================

DEFAULT_CHANNELS = [
    ("general", "General discussion for the community"),
    ("spoilers", "Manga / future-episode spoilers only"),
    ("fan-art", "Art, edits and memes"),
    ("episode-discussion", "Live reactions to episodes"),
]

DEFAULT_RULES = (
    "1. Be kind and respectful.\n"
    "2. Keep spoilers in #spoilers.\n"
    "3. No harassment, hate speech or NSFW content.\n"
    "4. Follow the moderators' instructions."
)

COMMUNITY_ROLES = ("owner", "moderator", "member")


def get_community(cid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_communities WHERE id = ?", (cid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_channel(chid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_channels WHERE id = ?", (chid,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def community_member_count(cid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) AS n FROM thr_community_members WHERE community_id = ?",
        (cid,),
    )
    n = cur.fetchone()["n"]
    conn.close()
    return n


def create_community(name, description, genre, owner_id, icon_color=None):
    """Create a community owned by the caller, with the four default channels."""
    conn = get_connection()
    cur = conn.cursor()
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        slug = "community"
    base = slug
    n = 1
    while True:
        cur.execute("SELECT id FROM thr_communities WHERE slug = ?", (slug,))
        if cur.fetchone() is None:
            break
        n += 1
        slug = f"{base}-{n}"
    cur.execute(
        """
        INSERT INTO thr_communities
        (name, slug, description, genre, icon_color, is_public, owner_id, rules)
        VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (name.strip(), slug, (description or "").strip(), (genre or "").strip(),
         icon_color or "#8b5cf6", owner_id, DEFAULT_RULES),
    )
    cid = cur.lastrowid
    cur.execute(
        "INSERT INTO thr_community_members (community_id, user_id, role) VALUES (?, ?, 'owner')",
        (cid, owner_id),
    )
    for ch_name, topic in DEFAULT_CHANNELS:
        cur.execute(
            "INSERT INTO thr_channels (community_id, name, topic, is_default) VALUES (?, ?, ?, 1)",
            (cid, ch_name, topic),
        )
    conn.commit()
    conn.close()
    return cid


def _channel_unread(cur, ch, user_id):
    """Unread message count for one channel row (caller holds the cursor)."""
    cur.execute(
        """
        SELECT COALESCE((
            SELECT COUNT(*) FROM thr_messages m
            WHERE m.context_type = 'channel' AND m.context_id = ?
              AND m.id > COALESCE((SELECT last_read_message_id
                                   FROM thr_channel_reads
                                   WHERE channel_id = ? AND user_id = ?), 0)
              AND m.sender_id != ? AND m.deleted_at IS NULL
        ), 0) AS n
        """,
        (ch["id"], ch["id"], user_id, user_id),
    )
    return cur.fetchone()["n"] or 0


def get_community_channels(cid, user_id):
    """Channels of a community with unread counts and live-party flags."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT ch.*,
               EXISTS(
                   SELECT 1 FROM thr_watch_parties wp
                   WHERE wp.channel_id = ch.id
                     AND datetime(wp.scheduled_time) <= datetime('now')
               ) AS has_live_party
        FROM thr_channels ch
        WHERE ch.community_id = ?
        ORDER BY ch.is_default DESC, ch.id ASC
        """,
        (cid,),
    )
    out = []
    for row in cur.fetchall():
        ch = dict(row)
        ch["unread"] = _channel_unread(cur, ch, user_id)
        ch["has_live_party"] = bool(ch["has_live_party"])
        out.append(ch)
    conn.close()
    return out


def get_user_communities(user_id):
    """Rail list: communities the user belongs to, each with its channels,
    total unread and per-community mute flag."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.*, m.role, m.muted,
               (SELECT COUNT(*) FROM thr_community_members
                WHERE community_id = c.id) AS member_count
        FROM thr_communities c
        JOIN thr_community_members m ON m.community_id = c.id AND m.user_id = ?
        ORDER BY c.id ASC
        """,
        (user_id,),
    )
    out = []
    for row in cur.fetchall():
        c = dict(row)
        channels = get_community_channels(c["id"], user_id)
        c["channels"] = channels
        c["unread"] = sum(ch["unread"] for ch in channels)
        c["member_count"] = c.get("member_count") or 0
        c["muted"] = bool(c["muted"])
        c["role"] = c.get("role") or "member"
        out.append(c)
    conn.close()
    return out


def get_community_detail(cid, user_id):
    """Everything the Communities tab needs for one community: info, my role,
    members with roles, channels, open reports (mods only)."""
    community = get_community(cid)
    if not community:
        return None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    me = cur.fetchone()
    if not me:
        conn.close()
        return None
    cur.execute(
        """
        SELECT u.id, u.username, u.avatar_color, m.role, m.muted, m.joined_at
        FROM thr_community_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.community_id = ?
        ORDER BY m.joined_at ASC
        """,
        (cid,),
    )
    members = [dict(r) for r in cur.fetchall()]
    cur.execute(
        "SELECT u.id, u.username FROM thr_community_bans b JOIN users u ON u.id = b.user_id WHERE b.community_id = ?",
        (cid,),
    )
    banned = [dict(r) for r in cur.fetchall()]
    reports = []
    my_role = me["role"]
    if my_role in ("owner", "moderator"):
        cur.execute(
            """
            SELECT r.*, m.content, u.username AS reporter
            FROM thr_reports r
            JOIN thr_messages m ON m.id = r.message_id
            JOIN users u ON u.id = r.reporter_id
            WHERE r.status = 'open'
              AND m.context_type = 'channel'
              AND m.context_id IN (SELECT id FROM thr_channels WHERE community_id = ?)
            ORDER BY r.id DESC LIMIT 50
            """,
            (cid,),
        )
        reports = [dict(r) for r in cur.fetchall()]
    conn.close()
    community = dict(community)
    community["member_count"] = community_member_count(cid)
    return {
        "community": community,
        "my_role": my_role,
        "members": members,
        "banned": banned,
        "reports": reports,
    }


def discover_communities(user_id, genre=None, q=None):
    """Public communities the user hasn't joined, with member counts."""
    conn = get_connection()
    cur = conn.cursor()
    sql = """
        SELECT c.*,
               (SELECT COUNT(*) FROM thr_community_members
                WHERE community_id = c.id) AS member_count
        FROM thr_communities c
        WHERE c.is_public = 1
          AND NOT EXISTS (SELECT 1 FROM thr_community_members m
                          WHERE m.community_id = c.id AND m.user_id = ?)
    """
    args = [user_id]
    if genre:
        sql += " AND c.genre = ?"
        args.append(genre)
    if q:
        sql += " AND (c.name LIKE ? OR c.description LIKE ? OR c.genre LIKE ?)"
        like = f"%{q.strip()}%"
        args += [like, like, like]
    sql += " ORDER BY member_count DESC, c.id ASC LIMIT 50"
    cur.execute(sql, tuple(args))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def is_community_member(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def get_member_role(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return row["role"] if row else None


def is_banned(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM thr_community_bans WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def is_community_moderator(cid, user_id):
    role = get_member_role(cid, user_id)
    return role in ("owner", "moderator")


def join_community(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO thr_community_members (community_id, user_id, role) VALUES (?, ?, 'member')",
        (cid, user_id),
    )
    conn.commit()
    conn.close()


def leave_community(cid, user_id):
    """Leave a community. The owner transfers ownership to the earliest
    moderator, else the earliest member. An empty community is deleted."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    if not row:
        conn.close()
        return False
    role = row["role"]
    cur.execute(
        "DELETE FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    cur.execute("DELETE FROM thr_channel_reads WHERE user_id = ? AND channel_id IN (SELECT id FROM thr_channels WHERE community_id = ?)", (user_id, cid))
    if role == "owner":
        cur.execute(
            "SELECT user_id FROM thr_community_members WHERE community_id = ? ORDER BY joined_at ASC LIMIT 1",
            (cid,),
        )
        nxt = cur.fetchone()
        if nxt:
            cur.execute(
                "UPDATE thr_community_members SET role = 'owner' WHERE community_id = ? AND user_id = ?",
                (cid, nxt["user_id"]),
            )
        else:
            cur.execute("DELETE FROM thr_communities WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return True


def remove_member(cid, user_id):
    """Kick a member out (owner/moderator action). The owner cannot be kicked."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    if not row or row["role"] == "owner":
        conn.close()
        return False
    cur.execute(
        "DELETE FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    cur.execute(
        "DELETE FROM thr_channel_reads WHERE user_id = ? AND channel_id IN (SELECT id FROM thr_channels WHERE community_id = ?)",
        (user_id, cid),
    )
    conn.commit()
    conn.close()
    return True


def set_member_role(cid, user_id, role):
    if role not in COMMUNITY_ROLES:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    if not row or row["role"] == "owner":
        conn.close()
        return False  # owner can't be demoted; unknown user
    cur.execute(
        "UPDATE thr_community_members SET role = ? WHERE community_id = ? AND user_id = ?",
        (role, cid, user_id),
    )
    conn.commit()
    conn.close()
    return True


def ban_member(cid, user_id, banned_by, reason=None):
    """Ban a user: add to bans and drop their membership."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT role FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    if row and row["role"] == "owner":
        conn.close()
        return False
    cur.execute(
        "INSERT OR REPLACE INTO thr_community_bans (community_id, user_id, banned_by, reason) VALUES (?, ?, ?, ?)",
        (cid, user_id, banned_by, reason),
    )
    cur.execute(
        "DELETE FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    cur.execute(
        "DELETE FROM thr_channel_reads WHERE user_id = ? AND channel_id IN (SELECT id FROM thr_channels WHERE community_id = ?)",
        (user_id, cid),
    )
    conn.commit()
    conn.close()
    return True


def unban_member(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM thr_community_bans WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    conn.commit()
    conn.close()


def set_community_muted(cid, user_id, muted):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_community_members SET muted = ? WHERE community_id = ? AND user_id = ?",
        (1 if muted else 0, cid, user_id),
    )
    conn.commit()
    conn.close()


def set_member_muted(cid, user_id, muted):
    """Mod action: mute a member so they can't post."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_community_members SET muted = ? WHERE community_id = ? AND user_id = ?",
        (1 if muted else 0, cid, user_id),
    )
    conn.commit()
    conn.close()


def get_member_muted(cid, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT muted FROM thr_community_members WHERE community_id = ? AND user_id = ?",
        (cid, user_id),
    )
    row = cur.fetchone()
    conn.close()
    return bool(row and row["muted"])


def update_community(cid, name=None, description=None, genre=None, icon_color=None, rules=None):
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name.strip())
    if description is not None:
        sets.append("description = ?")
        args.append((description or "").strip())
    if genre is not None:
        sets.append("genre = ?")
        args.append((genre or "").strip())
    if icon_color is not None:
        sets.append("icon_color = ?")
        args.append(icon_color)
    if rules is not None:
        sets.append("rules = ?")
        args.append((rules or "").strip())
    if not sets:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE thr_communities SET {', '.join(sets)} WHERE id = ?", tuple(args) + (cid,))
    conn.commit()
    conn.close()
    return True


def create_channel(cid, name, topic=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO thr_channels (community_id, name, topic) VALUES (?, ?, ?)",
        (cid, name.strip(), (topic or "").strip()),
    )
    chid = cur.lastrowid
    conn.commit()
    conn.close()
    return chid


def rename_channel(chid, name=None, topic=None):
    sets, args = [], []
    if name is not None:
        sets.append("name = ?")
        args.append(name.strip())
    if topic is not None:
        sets.append("topic = ?")
        args.append((topic or "").strip())
    if not sets:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(f"UPDATE thr_channels SET {', '.join(sets)} WHERE id = ?", tuple(args) + (chid,))
    conn.commit()
    conn.close()
    return True


def delete_channel(chid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM thr_channels WHERE id = ?", (chid,))
    conn.commit()
    conn.close()


def mark_channel_read(chid, user_id, message_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_channel_reads (channel_id, user_id, last_read_message_id)
        VALUES (?, ?, ?)
        ON CONFLICT(channel_id, user_id)
        DO UPDATE SET last_read_message_id = excluded.last_read_message_id
        """,
        (chid, user_id, message_id),
    )
    conn.commit()
    conn.close()


def log_mod_action(cid, actor_id, action, target_user_id=None, target_message_id=None, reason=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_mod_log
        (community_id, actor_id, action, target_user_id, target_message_id, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (cid, actor_id, action, target_user_id, target_message_id, reason),
    )
    conn.commit()
    conn.close()


def get_mod_log(cid, limit=50):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT l.*, a.username AS actor, t.username AS target
        FROM thr_mod_log l
        LEFT JOIN users a ON a.id = l.actor_id
        LEFT JOIN users t ON t.id = l.target_user_id
        WHERE l.community_id = ?
        ORDER BY l.id DESC LIMIT ?
        """,
        (cid, limit),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def report_message(message_id, reporter_id, reason=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO thr_reports (message_id, reporter_id, reason) VALUES (?, ?, ?)",
        (message_id, reporter_id, (reason or "").strip()[:500]),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def resolve_report(report_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_reports SET status = 'resolved' WHERE id = ? AND status = 'open'",
        (report_id,),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def block_user(user_id, blocked_id):
    if user_id == blocked_id:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO thr_user_blocks (user_id, blocked_id) VALUES (?, ?)",
        (user_id, blocked_id),
    )
    conn.commit()
    conn.close()
    return True


def unblock_user(user_id, blocked_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM thr_user_blocks WHERE user_id = ? AND blocked_id = ?",
        (user_id, blocked_id),
    )
    conn.commit()
    conn.close()


def get_blocked_ids(user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT blocked_id FROM thr_user_blocks WHERE user_id = ?", (user_id,))
    ids = [r["blocked_id"] for r in cur.fetchall()]
    conn.close()
    return ids


def get_community_members_public(cid):
    """Member rows with roles — used to power channel chat (mention list)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.id, u.username, u.avatar_color, m.role, m.muted, m.joined_at
        FROM thr_community_members m
        JOIN users u ON u.id = m.user_id
        WHERE m.community_id = ?
        ORDER BY m.joined_at ASC
        """,
        (cid,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def soft_delete_any(msg_id):
    """Moderator delete — soft-deletes regardless of sender."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE thr_messages SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
        (_utcnow(), msg_id),
    )
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok
# ---------------------------------------------------------------------------
# Polls
# ---------------------------------------------------------------------------

def create_poll(channel_id, author_id, question, options):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO thr_polls (channel_id, question, author_id) VALUES (?, ?, ?)",
        (channel_id, (question or "").strip(), author_id),
    )
    pid = cur.lastrowid
    for opt in options[:8]:
        opt = (opt or "").strip()
        if opt:
            cur.execute(
                "INSERT INTO thr_poll_options (poll_id, text) VALUES (?, ?)", (pid, opt)
            )
    conn.commit()
    conn.close()
    return pid


def get_channel_polls(channel_id, viewer_id):
    """All polls in a channel with option counts and the viewer's vote."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT p.*, u.username AS author, u.avatar_color AS author_color,
               (SELECT COUNT(*) FROM thr_poll_votes v WHERE v.poll_id = p.id) AS total_votes
        FROM thr_polls p
        JOIN users u ON u.id = p.author_id
        WHERE p.channel_id = ?
        ORDER BY p.id ASC
        """,
        (channel_id,),
    )
    out = []
    for row in cur.fetchall():
        poll = dict(row)
        poll["author_color"] = poll.get("author_color") or "#8b5cf6"
        cur.execute(
            "SELECT id, text FROM thr_poll_options WHERE poll_id = ? ORDER BY id ASC",
            (poll["id"],),
        )
        options = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT option_id, COUNT(*) AS n FROM thr_poll_votes WHERE poll_id = ? GROUP BY option_id",
            (poll["id"],),
        )
        counts = {r["option_id"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT option_id FROM thr_poll_votes WHERE poll_id = ? AND user_id = ?",
            (poll["id"], viewer_id),
        )
        my_vote = cur.fetchone()
        for opt in options:
            opt["votes"] = counts.get(opt["id"], 0)
        poll["options"] = options
        poll["my_option_id"] = my_vote["option_id"] if my_vote else None
        out.append(poll)
    conn.close()
    return out


def vote_poll(poll_id, option_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id FROM thr_poll_options WHERE id = ? AND poll_id = ?", (option_id, poll_id))
    if cur.fetchone() is None:
        conn.close()
        return False
    cur.execute(
        "INSERT OR REPLACE INTO thr_poll_votes (poll_id, option_id, user_id) VALUES (?, ?, ?)",
        (poll_id, option_id, user_id),
    )
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Watch parties
# ---------------------------------------------------------------------------

def create_watch_party(channel_id, anime_id, host_user_id, title, scheduled_time):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO thr_watch_parties
        (channel_id, anime_id, host_user_id, title, scheduled_time)
        VALUES (?, ?, ?, ?, ?)
        """,
        (channel_id, (anime_id or "").strip(), host_user_id, (title or "").strip()[:120], scheduled_time),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def get_channel_parties(channel_id, viewer_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT wp.*, u.username AS host, u.avatar_color AS host_color,
               CASE WHEN datetime(wp.scheduled_time) <= datetime('now') THEN 1 ELSE 0 END AS is_live,
               (SELECT COUNT(*) FROM thr_watch_party_rsvps r WHERE r.party_id = wp.id) AS rsvp_count,
               EXISTS(SELECT 1 FROM thr_watch_party_rsvps r WHERE r.party_id = wp.id AND r.user_id = ?) AS is_rsvped
        FROM thr_watch_parties wp
        JOIN users u ON u.id = wp.host_user_id
        WHERE wp.channel_id = ?
        ORDER BY wp.scheduled_time ASC
        """,
        (viewer_id, channel_id),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        row["is_live"] = bool(row["is_live"])
        row["is_rsvped"] = bool(row["is_rsvped"])
    conn.close()
    return rows


def get_community_parties(cid, viewer_id):
    """All watch parties across a community (for the channel panel)."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT wp.*, ch.name AS channel_name, u.username AS host, u.avatar_color AS host_color,
               CASE WHEN datetime(wp.scheduled_time) <= datetime('now') THEN 1 ELSE 0 END AS is_live,
               (SELECT COUNT(*) FROM thr_watch_party_rsvps r WHERE r.party_id = wp.id) AS rsvp_count,
               EXISTS(SELECT 1 FROM thr_watch_party_rsvps r WHERE r.party_id = wp.id AND r.user_id = ?) AS is_rsvped
        FROM thr_watch_parties wp
        JOIN thr_channels ch ON ch.id = wp.channel_id
        JOIN users u ON u.id = wp.host_user_id
        WHERE ch.community_id = ?
        ORDER BY datetime(wp.scheduled_time) ASC
        """,
        (viewer_id, cid),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for row in rows:
        row["is_live"] = bool(row["is_live"])
        row["is_rsvped"] = bool(row["is_rsvped"])
    conn.close()
    return rows


def get_party(party_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM thr_watch_parties WHERE id = ?", (party_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def rsvp_party(party_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO thr_watch_party_rsvps (party_id, user_id) VALUES (?, ?)",
        (party_id, user_id),
    )
    conn.commit()
    conn.close()


def unrsvp_party(party_id, user_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM thr_watch_party_rsvps WHERE party_id = ? AND user_id = ?",
        (party_id, user_id),
    )
    conn.commit()
    conn.close()


def delete_party(party_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM thr_watch_parties WHERE id = ?", (party_id,))
    conn.commit()
    conn.close()