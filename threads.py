"""
Threads — the new chat system for Otakul (Phase 1: Messages tab).

A single blueprint that owns:
  • GET /threads            — the full-screen Threads app (Discord-style shell)
  • /threads/api/*          — JSON endpoints for conversations, the unified
                              message engine, typing, presence, GIF search,
                              uploads, settings and notifications.

Everything reads/writes the thr_* tables in threads_db.py. The legacy
chat.py / chat_messages / chat_presence system is never touched.

To enable, add TWO lines to app.py (after the profile blueprint):

    from threads import init_threads
    init_threads(app)
"""

import os
import secrets

from flask import (
    Blueprint,
    render_template,
    request,
    jsonify,
    g,
    url_for,
    redirect,
    flash,
)

import database as site_db
import threads_db

from services import giphy

bp = Blueprint("threads", __name__)

UPLOAD_DIR = os.path.join("static", "uploads")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB
ALLOWED_EXT = {
    "png": "image", "jpg": "image", "jpeg": "image",
    "gif": "image", "webp": "image",
    "mp4": "video", "webm": "video", "mov": "video",
}


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _user():
    return g.get("user")


def _json_user():
    """(user, error_response) — 401 JSON when logged out."""
    user = _user()
    if user is None:
        return None, (jsonify({"success": False, "error": "login"}), 401)
    return user, None


def _ctx(raw):
    """Parse a 'dm:12' / 'group:4' / 'channel:7' context string."""
    if not raw or ":" not in raw:
        return None
    ctype, _, cid = raw.partition(":")
    if ctype not in threads_db.CONTEXT_TYPES or not cid.isdigit():
        return None
    return ctype, int(cid)


def _enrich_messages(rows, member_ids=None):
    """Attach sender info + reply snippet to raw message rows."""
    out = []
    users = {}
    if member_ids:
        for uid in member_ids:
            u = site_db.get_user_by_id(uid)
            if u:
                users[uid] = {
                    "id": u["id"],
                    "username": u["username"],
                    "avatar_color": u["avatar_color"],
                    "avatar": u["avatar"],
                }
    for m in rows:
        item = dict(m)
        sender = users.get(m["sender_id"])
        if sender is None:
            u = site_db.get_user_by_id(m["sender_id"])
            sender = u and {
                "id": u["id"],
                "username": u["username"],
                "avatar_color": u["avatar_color"],
                "avatar": u["avatar"],
            }
            if sender:
                users[m["sender_id"]] = sender
        item["sender"] = sender
        item["parent"] = None
        if m.get("parent_message_id"):
            parent = threads_db.get_message(m["parent_message_id"])
            if parent:
                pu = site_db.get_user_by_id(parent["sender_id"])
                item["parent"] = {
                    "sender_username": pu["username"] if pu else "someone",
                    "content": parent.get("content") or "",
                    "id": parent["id"],
                }
        out.append(item)
    return out


def _can_act_in_context(ctype, cid):
    """Membership guard for every message route."""
    user, err = _json_user()
    if err:
        return None, err
    if not threads_db.can_access_context(ctype, cid, user["id"]):
        return None, (jsonify({"success": False, "error": "not_member"}), 403)
    return user, None


def _enrich_parties(parties):
    """Attach anime title/image to watch-party rows for display."""
    try:
        from anime_data import anime_database
    except Exception:
        anime_database = {}
    out = []
    for p in parties:
        item = dict(p)
        entry = anime_database.get(p.get("anime_id") or "")
        item["anime_title"] = (entry or {}).get("title") or p.get("anime_id") or ""
        item["anime_image"] = (entry or {}).get("image") or ""
        out.append(item)
    return out


def _community_guard(cid):
    """(user, community, error) — requires the community to exist; membership
    is checked per-action."""
    user, err = _json_user()
    if err:
        return None, None, err
    community = threads_db.get_community(cid)
    if community is None:
        return None, None, (jsonify({"success": False, "error": "not_found"}), 404)
    return user, community, None


def _mod_guard(community, user):
    """None if allowed, else an error tuple."""
    if not threads_db.is_community_moderator(community["id"], user["id"]):
        return (jsonify({"success": False, "error": "forbidden"}), 403)
    return None


def _member_guard(community, user):
    """None if the user is a (non-banned) member, else an error tuple."""
    if not threads_db.is_community_member(community["id"], user["id"]):
        return (jsonify({"success": False, "error": "not_member"}), 403)
    if threads_db.is_banned(community["id"], user["id"]):
        return (jsonify({"success": False, "error": "banned"}), 403)
    return None


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

@bp.route("/threads")
def index():
    user = _user()
    if user is None:
        flash("Log in to open Threads.", "error")
        return redirect(url_for("auth.login", next="/threads"))

    conversations = threads_db.get_user_conversations(user["id"])
    return render_template(
        "threads.html",
        conversations=conversations,
        unread_notifications=threads_db.unread_notification_count(user["id"]),
    )


# ---------------------------------------------------------------------------
# User search (DM / group creation)
# ---------------------------------------------------------------------------

@bp.route("/threads/api/users/search")
def search_users():
    user, err = _json_user()
    if err:
        return err
    q = (request.args.get("q") or "").strip()
    if len(q) < 1:
        return jsonify({"success": True, "users": []})
    results = threads_db.search_users(q, exclude_id=user["id"], limit=10)
    return jsonify({"success": True, "users": results})


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

@bp.route("/threads/api/conversations")
def conversations_list():
    user, err = _json_user()
    if err:
        return err
    return jsonify({
        "success": True,
        "conversations": threads_db.get_user_conversations(user["id"]),
    })


@bp.route("/threads/api/conversations/dm", methods=["POST"])
def open_dm():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        other_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "missing_user"}), 400
    if not other_id:
        return jsonify({"success": False, "error": "missing_user"}), 400
    other = site_db.get_user_by_id(other_id)
    if not other:
        return jsonify({"success": False, "error": "no_such_user"}), 404
    if other_id == user["id"]:
        return jsonify({"success": False, "error": "self_dm"}), 400
    conv_id = threads_db.get_or_create_dm(user["id"], other_id)
    convs = threads_db.get_user_conversations(user["id"])
    conv = next((c for c in convs if c["id"] == conv_id), None)
    return jsonify({"success": True, "conversation": conv})


@bp.route("/threads/api/conversations/group", methods=["POST"])
def create_group():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    member_ids = [int(i) for i in (data.get("member_ids") or []) if str(i).isdigit()]
    if not name or len(name) > 40:
        return jsonify({"success": False, "error": "bad_name"}), 400
    if len(member_ids) > 20:
        return jsonify({"success": False, "error": "too_many"}), 400
    # Only existing users may be added.
    valid = []
    for uid in member_ids:
        if site_db.get_user_by_id(uid):
            valid.append(uid)
    conv_id = threads_db.create_group(
        name, user["id"], valid, avatar_color=data.get("avatar_color")
    )
    convs = threads_db.get_user_conversations(user["id"])
    conv = next((c for c in convs if c["id"] == conv_id), None)
    return jsonify({"success": True, "conversation": conv})


@bp.route("/threads/api/conversations/<int:cid>", methods=["PATCH"])
def rename_group(cid):
    user, err = _json_user()
    if err:
        return err
    conv = threads_db.get_conversation(cid)
    if not conv or conv["type"] != "group":
        return jsonify({"success": False, "error": "not_found"}), 404
    if not threads_db.is_conversation_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or len(name) > 40:
        return jsonify({"success": False, "error": "bad_name"}), 400
    threads_db.rename_conversation(cid, name)
    return jsonify({"success": True, "name": name})


@bp.route("/threads/api/conversations/<int:cid>/members", methods=["POST"])
def add_group_member(cid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.is_conversation_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    uid = int(data.get("user_id") or 0)
    if not site_db.get_user_by_id(uid):
        return jsonify({"success": False, "error": "no_such_user"}), 404
    threads_db.add_conversation_member(cid, uid)
    return jsonify({"success": True})


@bp.route("/threads/api/conversations/<int:cid>/members/<int:uid>", methods=["DELETE"])
def remove_group_member(cid, uid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.is_conversation_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    # You can remove yourself (leave) or, as owner/admin, remove others.
    if uid != user["id"]:
        conv = threads_db.get_conversation(cid)
        if not conv or conv["type"] != "group":
            return jsonify({"success": False, "error": "not_found"}), 404
        members = threads_db.get_conversation_members(cid)
        me = next((m for m in members if m["id"] == user["id"]), None)
        if not me or me["role"] not in ("owner", "admin"):
            return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.remove_conversation_member(cid, uid)
    return jsonify({"success": True, "conversation_gone": not threads_db.get_conversation(cid)})


@bp.route("/threads/api/conversations/<int:cid>/read", methods=["POST"])
def mark_read(cid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.is_conversation_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    threads_db.mark_conversation_read(cid, user["id"], int(data.get("message_id") or 0))
    return jsonify({"success": True})


@bp.route("/threads/api/conversations/<int:cid>/mute", methods=["POST"])
def toggle_mute(cid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.is_conversation_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    threads_db.set_conversation_muted(cid, user["id"], bool(data.get("muted")))
    return jsonify({"success": True, "muted": bool(data.get("muted"))})


# ---------------------------------------------------------------------------
# Messages — the unified engine
# ---------------------------------------------------------------------------

@bp.route("/threads/api/messages", methods=["GET"])
def messages_get():
    """?ctx=dm:1&after=0  -> poll (incremental)
       ?ctx=dm:1&before=500 -> older history
    """
    user, err = _json_user()
    if err:
        return err
    parsed = _ctx(request.args.get("ctx"))
    if not parsed:
        return jsonify({"success": False, "error": "bad_ctx"}), 400
    ctype, cid = parsed
    if not threads_db.can_access_context(ctype, cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403

    after = int(request.args.get("after") or 0)
    before = int(request.args.get("before") or 0)
    limit = min(int(request.args.get("limit") or 60), 200)
    blocked = threads_db.get_blocked_ids(user["id"])

    members = []
    polls = []
    parties = []
    if ctype in ("dm", "group"):
        members = threads_db.get_conversation_members(cid)
    elif ctype == "channel":
        ch = threads_db.get_channel(cid)
        if ch:
            members = threads_db.get_community_members_public(ch["community_id"])
            polls = threads_db.get_channel_polls(cid, user["id"])
            parties = _enrich_parties(threads_db.get_channel_parties(cid, user["id"]))

    if after:
        rows = threads_db.get_messages_after(ctype, cid, after, limit, exclude_user_ids=blocked)
        typing = threads_db.get_typing_users(ctype, cid, user["id"])
        return jsonify({
            "success": True,
            "messages": _enrich_messages(rows),
            "typing": typing,
            "members": members,
            "polls": polls,
            "parties": parties,
        })

    rows = threads_db.get_messages(
        ctype, cid, before_id=before or None, limit=limit, exclude_user_ids=blocked
    )
    return jsonify({
        "success": True,
        "messages": _enrich_messages(rows),
        "typing": threads_db.get_typing_users(ctype, cid, user["id"]),
        "pins": _enrich_messages(threads_db.get_pinned_messages(ctype, cid)),
        "members": members,
        "polls": polls,
        "parties": parties,
        "settings": threads_db.get_settings(user["id"]),
    })


@bp.route("/threads/api/messages", methods=["POST"])
def messages_send():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    parsed = _ctx(data.get("ctx"))
    if not parsed:
        return jsonify({"success": False, "error": "bad_ctx"}), 400
    ctype, cid = parsed
    if not threads_db.can_access_context(ctype, cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403

    kind = data.get("kind") or "text"
    if kind not in threads_db.MESSAGE_KINDS:
        kind = "text"
    content = (data.get("content") or "").strip()
    if kind == "text" and not content:
        return jsonify({"success": False, "error": "empty"}), 400
    if len(content) > 4000:
        return jsonify({"success": False, "error": "too_long"}), 400

    # Community rules: muted members can't post.
    if ctype == "channel":
        ch = threads_db.get_channel(cid)
        if not ch:
            return jsonify({"success": False, "error": "not_found"}), 404
        if threads_db.get_member_muted(ch["community_id"], user["id"]):
            return jsonify({"success": False, "error": "muted"}), 403
    # DMs: you can't message someone who blocked you.
    if ctype == "dm":
        for m in threads_db.get_conversation_members(cid):
            if m["id"] != user["id"] and user["id"] in threads_db.get_blocked_ids(m["id"]):
                return jsonify({"success": False, "error": "blocked"}), 403

    parent_id = data.get("parent_message_id")
    parent_id = int(parent_id) if str(parent_id or "").isdigit() else None
    if parent_id:
        parent = threads_db.get_message(parent_id)
        if not parent or parent["context_type"] != ctype or parent["context_id"] != cid:
            parent_id = None

    row = threads_db.add_message(
        ctype, cid, user["id"], kind=kind, content=content,
        attachment_url=data.get("attachment_url"),
        attachment_preview=data.get("attachment_preview"),
        parent_message_id=parent_id,
    )
    return jsonify({
        "success": True,
        "message": _enrich_messages([row])[0],
    })


@bp.route("/threads/api/messages/<int:mid>", methods=["PATCH"])
def messages_edit(mid):
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"success": False, "error": "empty"}), 400
    if len(content) > 4000:
        return jsonify({"success": False, "error": "too_long"}), 400
    if not threads_db.edit_message(mid, user["id"], content):
        return jsonify({"success": False, "error": "forbidden"}), 403
    return jsonify({"success": True, "message": _enrich_messages([threads_db.get_message(mid)])[0]})


@bp.route("/threads/api/messages/<int:mid>", methods=["DELETE"])
def messages_delete(mid):
    user, err = _json_user()
    if err:
        return err
    msg = threads_db.get_message(mid)
    if not msg or msg.get("deleted_at"):
        return jsonify({"success": False, "error": "not_found"}), 404
    if msg["sender_id"] != user["id"]:
        # Channel moderators may delete any message inside their community.
        if msg["context_type"] == "channel":
            ch = threads_db.get_channel(msg["context_id"])
            if ch and threads_db.is_community_moderator(ch["community_id"], user["id"]):
                if not threads_db.soft_delete_any(mid):
                    return jsonify({"success": False, "error": "forbidden"}), 403
                threads_db.log_mod_action(
                    ch["community_id"], user["id"], "delete_message",
                    target_user_id=msg["sender_id"], target_message_id=mid,
                )
                return jsonify({"success": True, "id": mid, "deleted_at": True})
        return jsonify({"success": False, "error": "forbidden"}), 403
    if not threads_db.soft_delete_message(mid, user["id"]):
        return jsonify({"success": False, "error": "forbidden"}), 403
    return jsonify({"success": True, "id": mid, "deleted_at": True})


@bp.route("/threads/api/messages/<int:mid>/pin", methods=["POST"])
def messages_pin(mid):
    user, err = _json_user()
    if err:
        return err
    msg = threads_db.get_message(mid)
    if not msg or msg.get("deleted_at"):
        return jsonify({"success": False, "error": "not_found"}), 404
    if not threads_db.can_access_context(msg["context_type"], msg["context_id"], user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    threads_db.toggle_pin(mid)
    msg = threads_db.get_message(mid)
    return jsonify({
        "success": True,
        "id": mid,
        "is_pinned": bool(msg["is_pinned"]),
    })


# ---------------------------------------------------------------------------
# Typing + presence
# ---------------------------------------------------------------------------

@bp.route("/threads/api/typing", methods=["POST"])
def typing():
    user, err = _json_user()
    if err:
        return err
    parsed = _ctx((request.get_json(silent=True) or {}).get("ctx"))
    if not parsed:
        return jsonify({"success": False, "error": "bad_ctx"}), 400
    ctype, cid = parsed
    if not threads_db.can_access_context(ctype, cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    threads_db.set_typing(ctype, cid, user["id"])
    return jsonify({"success": True})


@bp.route("/threads/api/presence")
def presence():
    user, err = _json_user()
    if err:
        return err
    # Heartbeat — this request doubles as the "I'm here" ping.
    status = "away" if request.args.get("away") == "1" else "online"
    threads_db.touch_presence(user["id"], status)
    ids = request.args.get("ids")
    user_ids = [int(i) for i in ids.split(",") if str(i).isdigit()] if ids else []
    return jsonify({"success": True, "presence": threads_db.get_presence(user_ids)})


# ---------------------------------------------------------------------------
# GIF search (reuses services/giphy.py — set GIPHY_API_KEY)
# ---------------------------------------------------------------------------

@bp.route("/threads/api/gifs")
def gifs():
    user, err = _json_user()
    if err:
        return err
    q = (request.args.get("q") or "").strip()
    try:
        if q:
            raw = giphy.search(q, limit=24)
        else:
            raw = giphy.trending(limit=24)
    except Exception as exc:  # network / API hiccup — never crash the chat
        return jsonify({"success": False, "error": str(exc)}), 502
    if isinstance(raw, dict) and raw.get("error"):
        return jsonify({
            "success": False,
            "error": raw["error"],
            "hint": "Add your GIPHY_API_KEY to a .env file, then restart.",
        })
    return jsonify({"success": True, "results": giphy.simplify(raw).get("results", [])})


# ---------------------------------------------------------------------------
# Uploads (images / videos) — stored in static/uploads
# ---------------------------------------------------------------------------

@bp.route("/threads/api/upload", methods=["POST"])
def upload():
    user, err = _json_user()
    if err:
        return err
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"success": False, "error": "no_file"}), 400
    ext = (file.filename.rsplit(".", 1)[-1] or "").lower()
    if ext not in ALLOWED_EXT:
        return jsonify({"success": False, "error": "bad_type"}), 400
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_BYTES:
        return jsonify({"success": False, "error": "too_large"}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fname = f"{secrets.token_hex(10)}.{ext}"
    path = os.path.join(UPLOAD_DIR, fname)
    file.save(path)
    url = url_for("static", filename=f"uploads/{fname}")
    return jsonify({
        "success": True,
        "kind": ALLOWED_EXT[ext],
        "url": url,
        "name": file.filename,
    })


# ---------------------------------------------------------------------------
# Settings + notifications
# ---------------------------------------------------------------------------

@bp.route("/threads/api/settings", methods=["GET"])
def settings_get():
    user, err = _json_user()
    if err:
        return err
    return jsonify({"success": True, "settings": threads_db.get_settings(user["id"])})


@bp.route("/threads/api/settings", methods=["POST"])
def settings_save():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    threads_db.save_settings(
        user["id"],
        read_receipts=data.get("read_receipts"),
        typing_indicators=data.get("typing_indicators"),
    )
    return jsonify({"success": True, "settings": threads_db.get_settings(user["id"])})


@bp.route("/threads/api/notifications")
def notifications():
    user, err = _json_user()
    if err:
        return err
    return jsonify({
        "success": True,
        "notifications": threads_db.get_notifications(user["id"]),
        "unread": threads_db.unread_notification_count(user["id"]),
    })


@bp.route("/threads/api/notifications/read", methods=["POST"])
def notifications_read():
    user, err = _json_user()
    if err:
        return err
    threads_db.mark_notifications_read(user["id"])
    return jsonify({"success": True})


# ---------------------------------------------------------------------------
# Communities (Phase 2)
# ---------------------------------------------------------------------------

@bp.route("/threads/api/communities")
def communities_list():
    user, err = _json_user()
    if err:
        return err
    communities = threads_db.get_user_communities(user["id"])
    for c in communities:
        c["parties"] = _enrich_parties(
            threads_db.get_community_parties(c["id"], user["id"])
        )
    return jsonify({"success": True, "communities": communities})


@bp.route("/threads/api/communities", methods=["POST"])
def communities_create():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name or len(name) > 60:
        return jsonify({"success": False, "error": "bad_name"}), 400
    cid = threads_db.create_community(
        name,
        (data.get("description") or "").strip()[:500],
        (data.get("genre") or "").strip()[:40],
        user["id"],
        data.get("icon_color"),
    )
    communities = threads_db.get_user_communities(user["id"])
    community = next((c for c in communities if c["id"] == cid), None)
    return jsonify({"success": True, "community": community})


@bp.route("/threads/api/communities/discover")
def communities_discover():
    user, err = _json_user()
    if err:
        return err
    genre = (request.args.get("genre") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    return jsonify({
        "success": True,
        "communities": threads_db.discover_communities(user["id"], genre=genre, q=q),
    })


@bp.route("/threads/api/communities/<int:cid>")
def community_detail(cid):
    user, err = _json_user()
    if err:
        return err
    community = threads_db.get_community(cid)
    if community is None:
        return jsonify({"success": False, "error": "not_found"}), 404
    detail = threads_db.get_community_detail(cid, user["id"])
    if detail is None:
        return jsonify({"success": False, "error": "not_member"}), 403
    return jsonify({"success": True, **detail})


@bp.route("/threads/api/communities/<int:cid>", methods=["PATCH"])
def community_update(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    data = request.get_json(silent=True) or {}
    threads_db.update_community(
        cid,
        name=data.get("name"),
        description=data.get("description"),
        genre=data.get("genre"),
        icon_color=data.get("icon_color"),
        rules=data.get("rules"),
    )
    threads_db.log_mod_action(cid, user["id"], "update_community")
    return jsonify({"success": True, "community": threads_db.get_community(cid)})


@bp.route("/threads/api/communities/<int:cid>/join", methods=["POST"])
def community_join(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    if not community["is_public"]:
        return jsonify({"success": False, "error": "private"}), 403
    if threads_db.is_banned(cid, user["id"]):
        return jsonify({"success": False, "error": "banned"}), 403
    if threads_db.is_community_member(cid, user["id"]):
        return jsonify({"success": True, "already": True})
    threads_db.join_community(cid, user["id"])
    communities = threads_db.get_user_communities(user["id"])
    joined = next((c for c in communities if c["id"] == cid), None)
    return jsonify({"success": True, "community": joined})


@bp.route("/threads/api/communities/<int:cid>/leave", methods=["POST"])
def community_leave(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    if not threads_db.leave_community(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    return jsonify({"success": True, "community_gone": not threads_db.get_community(cid)})


@bp.route("/threads/api/communities/<int:cid>/mute", methods=["POST"])
def community_mute(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mem_err = _member_guard(community, user)
    if mem_err:
        return mem_err
    data = request.get_json(silent=True) or {}
    threads_db.set_community_muted(cid, user["id"], bool(data.get("muted")))
    return jsonify({"success": True, "muted": bool(data.get("muted"))})


@bp.route("/threads/api/communities/<int:cid>/channels", methods=["POST"])
def channel_create(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().lower().replace(" ", "-")
    if not name or len(name) > 30:
        return jsonify({"success": False, "error": "bad_name"}), 400
    chid = threads_db.create_channel(cid, name, data.get("topic"))
    threads_db.log_mod_action(cid, user["id"], "create_channel", reason=name)
    return jsonify({"success": True, "channel": threads_db.get_channel(chid)})


@bp.route("/threads/api/channels/<int:chid>", methods=["PATCH"])
def channel_update(chid):
    user, err = _json_user()
    if err:
        return err
    ch = threads_db.get_channel(chid)
    if not ch:
        return jsonify({"success": False, "error": "not_found"}), 404
    community = threads_db.get_community(ch["community_id"])
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip().lower().replace(" ", "-") if data.get("name") else None
    if name is not None and (not name or len(name) > 30):
        return jsonify({"success": False, "error": "bad_name"}), 400
    threads_db.rename_channel(chid, name=name, topic=data.get("topic"))
    threads_db.log_mod_action(community["id"], user["id"], "update_channel", reason=name or "topic")
    return jsonify({"success": True, "channel": threads_db.get_channel(chid)})


@bp.route("/threads/api/channels/<int:chid>", methods=["DELETE"])
def channel_delete(chid):
    user, err = _json_user()
    if err:
        return err
    ch = threads_db.get_channel(chid)
    if not ch:
        return jsonify({"success": False, "error": "not_found"}), 404
    community = threads_db.get_community(ch["community_id"])
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    threads_db.delete_channel(chid)
    threads_db.log_mod_action(community["id"], user["id"], "delete_channel", reason=ch["name"])
    return jsonify({"success": True})


@bp.route("/threads/api/channels/<int:chid>/read", methods=["POST"])
def channel_read(chid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.can_access_context("channel", chid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    threads_db.mark_channel_read(chid, user["id"], int(data.get("message_id") or 0))
    return jsonify({"success": True})


# ---- Polls ----

@bp.route("/threads/api/channels/<int:chid>/polls", methods=["POST"])
def poll_create(chid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.can_access_context("channel", chid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    options = [str(o).strip() for o in (data.get("options") or [])]
    options = [o for o in options if o]
    if not question or len(question) > 300:
        return jsonify({"success": False, "error": "bad_question"}), 400
    if len(options) < 2 or len(options) > 8:
        return jsonify({"success": False, "error": "bad_options"}), 400
    pid = threads_db.create_poll(chid, user["id"], question, options)
    return jsonify({"success": True, "polls": threads_db.get_channel_polls(chid, user["id"])})


@bp.route("/threads/api/polls/<int:pid>/vote", methods=["POST"])
def poll_vote(pid):
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        option_id = int(data.get("option_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "bad_option"}), 400
    conn = threads_db.get_connection()
    row = conn.execute(
        "SELECT channel_id FROM thr_polls WHERE id = ?", (pid,)
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    if not threads_db.can_access_context("channel", row["channel_id"], user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    if not threads_db.vote_poll(pid, option_id, user["id"]):
        return jsonify({"success": False, "error": "bad_option"}), 400
    return jsonify({
        "success": True,
        "polls": threads_db.get_channel_polls(row["channel_id"], user["id"]),
    })


# ---- Watch parties ----

@bp.route("/threads/api/channels/<int:chid>/parties", methods=["POST"])
def party_create(chid):
    user, err = _json_user()
    if err:
        return err
    if not threads_db.can_access_context("channel", chid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    anime_id = (data.get("anime_id") or "").strip()
    scheduled = (data.get("scheduled_time") or "").strip()
    if not title:
        return jsonify({"success": False, "error": "bad_title"}), 400
    if not scheduled:
        return jsonify({"success": False, "error": "bad_time"}), 400
    pid = threads_db.create_watch_party(chid, anime_id, user["id"], title, scheduled)
    return jsonify({
        "success": True,
        "parties": _enrich_parties(threads_db.get_channel_parties(chid, user["id"])),
    })


@bp.route("/threads/api/parties/<int:pid>/rsvp", methods=["POST"])
def party_rsvp(pid):
    user, err = _json_user()
    if err:
        return err
    party = threads_db.get_party(pid)
    if not party:
        return jsonify({"success": False, "error": "not_found"}), 404
    if not threads_db.can_access_context("channel", party["channel_id"], user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    threads_db.rsvp_party(pid, user["id"])
    return jsonify({
        "success": True,
        "parties": _enrich_parties(threads_db.get_channel_parties(party["channel_id"], user["id"])),
    })


@bp.route("/threads/api/parties/<int:pid>/rsvp", methods=["DELETE"])
def party_unrsvp(pid):
    user, err = _json_user()
    if err:
        return err
    party = threads_db.get_party(pid)
    if not party:
        return jsonify({"success": False, "error": "not_found"}), 404
    threads_db.unrsvp_party(pid, user["id"])
    return jsonify({
        "success": True,
        "parties": _enrich_parties(threads_db.get_channel_parties(party["channel_id"], user["id"])),
    })


@bp.route("/threads/api/parties/<int:pid>", methods=["DELETE"])
def party_delete(pid):
    user, err = _json_user()
    if err:
        return err
    party = threads_db.get_party(pid)
    if not party:
        return jsonify({"success": False, "error": "not_found"}), 404
    ch = threads_db.get_channel(party["channel_id"])
    is_mod = ch and threads_db.is_community_moderator(ch["community_id"], user["id"])
    if party["host_user_id"] != user["id"] and not is_mod:
        return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.delete_party(pid)
    return jsonify({"success": True})


# ---- Roles + moderation ----

@bp.route("/threads/api/communities/<int:cid>/members/<int:uid>/role", methods=["POST"])
def member_set_role(cid, uid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    if not threads_db.is_community_member(cid, user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    # Only the owner changes roles (moderator <-> member).
    if threads_db.get_member_role(cid, user["id"]) != "owner":
        return jsonify({"success": False, "error": "forbidden"}), 403
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in ("moderator", "member"):
        return jsonify({"success": False, "error": "bad_role"}), 400
    if not threads_db.set_member_role(cid, uid, role):
        return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.log_mod_action(cid, user["id"], "set_role", target_user_id=uid, reason=role)
    return jsonify({"success": True, "role": role})


@bp.route("/threads/api/communities/<int:cid>/members/<int:uid>/kick", methods=["POST"])
def member_kick(cid, uid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    if uid == user["id"]:
        return jsonify({"success": False, "error": "self"}), 400
    if not threads_db.remove_member(cid, uid):
        return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.log_mod_action(cid, user["id"], "kick", target_user_id=uid)
    return jsonify({"success": True})


@bp.route("/threads/api/communities/<int:cid>/members/<int:uid>/mute", methods=["POST"])
def member_mute(cid, uid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    data = request.get_json(silent=True) or {}
    muted = bool(data.get("muted"))
    if uid == user["id"] or not threads_db.is_community_member(cid, uid):
        return jsonify({"success": False, "error": "bad_user"}), 400
    threads_db.set_member_muted(cid, uid, muted)
    threads_db.log_mod_action(cid, user["id"], "mute" if muted else "unmute", target_user_id=uid)
    return jsonify({"success": True, "muted": muted})


@bp.route("/threads/api/communities/<int:cid>/members/<int:uid>/ban", methods=["POST"])
def member_ban(cid, uid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    data = request.get_json(silent=True) or {}
    if uid == user["id"]:
        return jsonify({"success": False, "error": "self"}), 400
    if not threads_db.ban_member(cid, uid, user["id"], (data.get("reason") or "").strip()[:300]):
        return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.log_mod_action(cid, user["id"], "ban", target_user_id=uid, reason=(data.get("reason") or "").strip()[:300])
    return jsonify({"success": True})


@bp.route("/threads/api/communities/<int:cid>/members/<int:uid>/unban", methods=["POST"])
def member_unban(cid, uid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    threads_db.unban_member(cid, uid)
    threads_db.log_mod_action(cid, user["id"], "unban", target_user_id=uid)
    return jsonify({"success": True})


@bp.route("/threads/api/communities/<int:cid>/modlog")
def community_modlog(cid):
    user, community, err = _community_guard(cid)
    if err:
        return err
    mod_err = _mod_guard(community, user)
    if mod_err:
        return mod_err
    return jsonify({"success": True, "log": threads_db.get_mod_log(cid)})


@bp.route("/threads/api/messages/<int:mid>/report", methods=["POST"])
def message_report(mid):
    user, err = _json_user()
    if err:
        return err
    msg = threads_db.get_message(mid)
    if not msg or msg.get("deleted_at"):
        return jsonify({"success": False, "error": "not_found"}), 404
    if msg["sender_id"] == user["id"]:
        return jsonify({"success": False, "error": "self_report"}), 400
    if not threads_db.can_access_context(msg["context_type"], msg["context_id"], user["id"]):
        return jsonify({"success": False, "error": "not_member"}), 403
    data = request.get_json(silent=True) or {}
    threads_db.report_message(mid, user["id"], data.get("reason"))
    return jsonify({"success": True})


@bp.route("/threads/api/reports/<int:rid>/resolve", methods=["POST"])
def report_resolve(rid):
    user, err = _json_user()
    if err:
        return err
    conn = threads_db.get_connection()
    row = conn.execute(
        """
        SELECT r.id, ch.community_id FROM thr_reports r
        JOIN thr_messages m ON m.id = r.message_id
        JOIN thr_channels ch ON ch.id = m.context_id
        WHERE r.id = ? AND m.context_type = 'channel'
        """,
        (rid,),
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    if not threads_db.is_community_moderator(row["community_id"], user["id"]):
        return jsonify({"success": False, "error": "forbidden"}), 403
    threads_db.resolve_report(rid)
    return jsonify({"success": True})


# ---- User blocks ----

@bp.route("/threads/api/users/block", methods=["POST"])
def user_block():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        uid = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "bad_user"}), 400
    if uid == user["id"] or not site_db.get_user_by_id(uid):
        return jsonify({"success": False, "error": "bad_user"}), 400
    threads_db.block_user(user["id"], uid)
    return jsonify({"success": True})


@bp.route("/threads/api/users/block", methods=["DELETE"])
def user_unblock():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        uid = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "bad_user"}), 400
    threads_db.unblock_user(user["id"], uid)
    return jsonify({"success": True})


@bp.route("/threads/api/users/blocked")
def user_blocked():
    user, err = _json_user()
    if err:
        return err
    ids = threads_db.get_blocked_ids(user["id"])
    out = []
    for uid in ids:
        u = site_db.get_user_by_id(uid)
        if u:
            out.append({
                "id": uid,
                "username": u["username"],
                "avatar_color": u["avatar_color"],
                "avatar": u["avatar"],
            })
    return jsonify({"success": True, "blocked": out})


# ---------------------------------------------------------------------------
# Registration hook
# ---------------------------------------------------------------------------

def init_threads(app):
    """Call from app.py (two lines). Creates the thr_* tables and registers
    the blueprint. Idempotent."""
    threads_db.create_tables()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.register_blueprint(bp)