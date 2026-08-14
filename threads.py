"""
Threads — the new chat system for AnimeChat (Phase 1: Messages tab).

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

    if after:
        rows = threads_db.get_messages_after(ctype, cid, after, limit)
        typing = threads_db.get_typing_users(ctype, cid, user["id"])
        return jsonify({
            "success": True,
            "messages": _enrich_messages(rows),
            "typing": typing,
            "members": threads_db.get_conversation_members(cid)
                if ctype in ("dm", "group") else [],
        })

    rows = threads_db.get_messages(ctype, cid, before_id=before or None, limit=limit)
    members = []
    if ctype in ("dm", "group"):
        members = threads_db.get_conversation_members(cid)
    return jsonify({
        "success": True,
        "messages": _enrich_messages(rows),
        "typing": threads_db.get_typing_users(ctype, cid, user["id"]),
        "pins": _enrich_messages(threads_db.get_pinned_messages(ctype, cid)),
        "members": members,
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
# Registration hook
# ---------------------------------------------------------------------------

def init_threads(app):
    """Call from app.py (two lines). Creates the thr_* tables and registers
    the blueprint. Idempotent."""
    threads_db.create_tables()
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    app.register_blueprint(bp)