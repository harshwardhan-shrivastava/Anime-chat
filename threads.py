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
    """Attach sender info + reply snippet to raw message rows.

    Batched: every sender and reply-parent is fetched with at most two
    queries total (get_users_by_ids / get_messages_by_ids). The old version
    ran two queries PER message, which on the remote Turso DB meant ~120
    sequential round trips for a 60-message channel.
    """
    out = []
    users = {}
    if member_ids:
        users.update(site_db.get_users_by_ids(member_ids))

    # Collect every user id and parent message id we need first.
    sender_ids = {m["sender_id"] for m in rows if m.get("sender_id")}
    parent_ids = [m["parent_message_id"] for m in rows if m.get("parent_message_id")]
    parents = threads_db.get_messages_by_ids(parent_ids) if parent_ids else {}
    for p in parents.values():
        if p.get("sender_id"):
            sender_ids.add(p["sender_id"])

    for uid in sender_ids:
        if uid not in users:
            u = site_db.get_user_by_id(uid)  # served from the TTL cache when warm
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
        item["sender"] = sender
        item["parent"] = None
        parent = parents.get(m.get("parent_message_id"))
        if parent:
            pu = users.get(parent["sender_id"])
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
    """Attach anime title/image to watch-party rows for display.

    Skips the anime catalog entirely when there are no parties — importing
    anime_data parses a ~47MB JSON on first touch, which used to stall the
    whole threads page for seconds on cold starts."""
    if not parties:
        return []
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

    # Pre-load the first conversation's 30 messages so the page opens
    # instantly with messages visible (like community chat).
    preloaded = {}
    # Also pre-load the first community's default channel so switching to the
    # Guilds tab opens its chat instantly instead of a blank/spinner.
    preloadedGuild = {}
    user_communities = threads_db.get_user_communities(user["id"])
    if user_communities:
        first_comm = user_communities[0]
        channels = first_comm.get("channels") or []
        # Prefer #general (is_default) for the fastest, most expected open.
        first_ch = next((ch for ch in channels if ch.get("is_default")), channels[0] if channels else None)
        if first_ch:
            gchid = first_ch.get("id")
            if gchid and threads_db.can_access_context("channel", gchid, user["id"]):
                drows = threads_db.get_messages("channel", gchid, limit=30)
                denriched = _enrich_messages(drows)
                preloadedGuild = {
                    "ctx": "channel:" + str(gchid),
                    "community_id": first_comm.get("id"),
                    "channel_id": gchid,
                    "messages": denriched,
                    "afterId": denriched[-1]["id"] if denriched else 0,
                    "firstId": denriched[0]["id"] if denriched else 0,
                    "hasMore": len(denriched) >= 30,
                    "pins": _enrich_messages(threads_db.get_pinned_messages("channel", gchid)),
                    "members": threads_db.get_community_members_public(first_comm.get("id")),
                    "polls": threads_db.get_channel_polls(gchid, user["id"]),
                    "parties": _enrich_parties(threads_db.get_channel_parties(gchid, user["id"])),
                }
    if conversations:
        first = conversations[0]
        ctype = first.get("type", "dm")
        cid = first.get("id")
        if cid and threads_db.can_access_context(ctype, cid, user["id"]):
            rows = threads_db.get_messages(ctype, cid, limit=30)
            enriched = _enrich_messages(rows)
            preloaded = {
                "ctx": f"{ctype}:{cid}",
                "messages": enriched,
                "afterId": enriched[-1]["id"] if enriched else 0,
                "firstId": enriched[0]["id"] if enriched else 0,
                "hasMore": len(enriched) >= 30,
                "pins": _enrich_messages(threads_db.get_pinned_messages(ctype, cid)),
            }
            # Add members for the first conversation
            if ctype in ("dm", "group"):
                preloaded["members"] = threads_db.get_conversation_members(cid)
            elif ctype == "channel":
                ch = threads_db.get_channel(cid)
                if ch:
                    preloaded["members"] = threads_db.get_community_members_public(ch["community_id"])
                    preloaded["polls"] = threads_db.get_channel_polls(cid, user["id"])
                    preloaded["parties"] = _enrich_parties(threads_db.get_channel_parties(cid, user["id"]))
            preloaded["settings"] = threads_db.get_settings(user["id"])

    user_xp = site_db.get_user_xp(user["id"])
    user_rank = site_db.get_user_rank(user["id"])
    try:
        _xp_rank, xp_pct = site_db.xp_progress(user_xp)
    except Exception:
        xp_pct = 0

    return render_template(
        "threads.html",
        conversations=conversations,
        unread_notifications=threads_db.unread_notification_count(user["id"]),
        preloaded=preloaded,
        preloaded_guild=preloadedGuild,
        user_xp=user_xp,
        user_rank=user_rank or "D",
        xp_pct=xp_pct,
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
    # Attach friendship status in ONE bulk query (was one query per user).
    statuses = threads_db.friendship_status_bulk(user["id"], [u["id"] for u in results])
    for u in results:
        fs = statuses.get(u["id"], {"status": "none", "req_id": None})
        u["friend_status"] = fs["status"]
        if fs["req_id"]:
            u["friend_req_id"] = fs["req_id"]
    return jsonify({"success": True, "users": results})


# ---------------------------------------------------------------------------
# Friend requests
# ---------------------------------------------------------------------------

@bp.route("/threads/api/friends/request", methods=["POST"])
def friend_request_send():
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    target = None
    if data.get("user_id"):
        try:
            target = site_db.get_user_by_id(int(data["user_id"]))
        except (TypeError, ValueError):
            target = None
    elif (data.get("username") or "").strip():
        target = site_db.get_user_by_username(data["username"].strip())
    if not target:
        return jsonify({"success": False, "error": "no_such_user"}), 404
    if target["id"] == user["id"]:
        return jsonify({"success": False, "error": "That's you!"}), 400
    ok, reason = threads_db.send_friend_request(user["id"], target["id"])
    if ok:
        return jsonify({"success": True, "status": reason,
                        "pending_count": threads_db.pending_friend_request_count(target["id"])})
    return jsonify({"success": False, "error": reason}), 409


@bp.route("/threads/api/friends")
def friends_list():
    """All accepted friends — used to send guild invites directly."""
    user, err = _json_user()
    if err:
        return err
    return jsonify({"success": True, "friends": threads_db.list_friends(user["id"])})


@bp.route("/threads/api/friends/requests")
def friend_requests_list():
    user, err = _json_user()
    if err:
        return err
    incoming, outgoing = threads_db.list_friend_requests(user["id"])
    return jsonify({
        "success": True,
        "incoming": incoming,
        "outgoing": outgoing,
        "count": len(incoming),
    })


@bp.route("/threads/api/friends/requests/<int:rid>/respond", methods=["POST"])
def friend_request_respond(rid):
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    accept = bool(data.get("accept"))
    ok, msg, other_id = threads_db.respond_friend_request(rid, user["id"], accept)
    if not ok:
        return jsonify({"success": False, "error": msg}), 404 if msg == "no_such_request" else 409
    resp = {"success": True, "action": msg}
    if accept and other_id:
        conv_id = threads_db.get_or_create_dm(user["id"], other_id)
        convs = threads_db.get_user_conversations(user["id"])
        resp["conversation"] = next((c for c in convs if c["id"] == conv_id), None)
    return jsonify(resp)


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
    # DMs require an accepted friend request (an existing DM keeps working).
    if not threads_db.are_friends(user["id"], other_id):
        has_dm = False
        convs = threads_db.get_user_conversations(user["id"])
        for c in convs:
            if (c["type"] == "dm" and c.get("other")
                    and c["other"]["id"] == other_id):
                has_dm = True
                break
        if not has_dm:
            return jsonify({"success": False, "error": "not_friends",
                            "hint": "Send a friend request first."}), 403
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
    # Dedupe: don't create a second guild with the exact same name that the
    # same user already owns (e.g. double-clicking "Create guild"). Returns
    # the existing guild so the UI still behaves correctly.
    existing = next(
        (c for c in threads_db.get_user_communities(user["id"])
         if c.get("owner_id") == user["id"]
         and (c.get("name") or "").strip().casefold() == name.casefold()),
        None,
    )
    if existing:
        return jsonify({"success": True, "community": existing, "already_exists": True})
    is_public = bool(data.get("is_public", True))
    cid = threads_db.create_community(
        name,
        (data.get("description") or "").strip()[:500],
        (data.get("genre") or "").strip()[:40],
        user["id"],
        data.get("icon_color"),
        (data.get("icon_url") or "").strip()[:500] or None,
        is_public=is_public,
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
    if "is_public" in data:
        is_public = bool(data.get("is_public"))
    else:
        is_public = None
    threads_db.update_community(
        cid,
        name=data.get("name"),
        description=data.get("description"),
        genre=data.get("genre"),
        icon_color=data.get("icon_color"),
        icon_url=(data.get("icon_url") or "").strip()[:500] or None,
        rules=data.get("rules"),
        is_public=is_public,
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


@bp.route("/threads/api/communities/<int:cid>/invite")
def community_invite(cid):
    """Get (or create) the guild's shareable invite link. Any member can
    share it — no mod powers needed."""
    user, community, err = _community_guard(cid)
    if err:
        return err
    mem_err = _member_guard(community, user)
    if mem_err:
        return mem_err
    code = threads_db.get_community_invite_code(cid)
    return jsonify({"success": True, "invite_code": code})


@bp.route("/threads/api/communities/<int:cid>/invite/send", methods=["POST"])
def community_invite_send(cid):
    """DM an accepted friend a guild invite link (no copying needed)."""
    user, community, err = _community_guard(cid)
    if err:
        return err
    mem_err = _member_guard(community, user)
    if mem_err:
        return mem_err
    data = request.get_json(silent=True) or {}
    try:
        other_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "missing_user"}), 400
    if not other_id:
        return jsonify({"success": False, "error": "missing_user"}), 400
    if not threads_db.are_friends(user["id"], other_id):
        return jsonify({"success": False, "error": "not_friends"}), 403
    code = threads_db.get_community_invite_code(cid)
    link = request.host_url.rstrip("/") + "/threads?invite=" + code
    conv_id = threads_db.get_or_create_dm(user["id"], other_id)
    row = threads_db.add_message(
        "dm", conv_id, user["id"], kind="text",
        content='Join my guild "' + community["name"] + '": ' + link,
    )
    return jsonify({"success": True, "message": _enrich_messages([row])[0]})


@bp.route("/threads/api/communities/join-invite", methods=["POST"])
def community_join_invite():
    """Join a guild through an invite link (/threads?invite=CODE)."""
    user, err = _json_user()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"success": False, "error": "bad_invite"}), 400
    cid, cerr = threads_db.join_community_by_invite(code, user["id"])
    if cerr:
        return jsonify({"success": False, "error": cerr}), 404 if cerr == "invalid_invite" else 403
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


@bp.route("/threads/api/communities/<int:cid>/delete", methods=["POST"])
def community_delete(cid):
    """Owner-only permanent delete of a guild + all its data."""
    user, community, err = _community_guard(cid)
    if err:
        return err
    if community.get("owner_id") != user["id"]:
        return jsonify({"success": False, "error": "You can only delete a guild you own."}), 403
    threads_db.delete_community(cid)
    return jsonify({"success": True, "community_gone": True})


@bp.route("/threads/api/users/<int:uid>/profile")
def user_public_profile(uid):
    """Public mini-profile for another user inside Threads: rank, XP, account
    join date, and the public guilds they're a member of (as tags)."""
    user, err = _json_user()
    if err:
        return err
    target = site_db.get_user_by_id(uid)
    if target is None:
        return jsonify({"success": False, "error": "no_such_user"}), 404
    xp = site_db.get_user_xp(uid)
    try:
        rank, xp_pct = site_db.xp_progress(xp)
    except Exception:
        rank, xp_pct = None, 0
    try:
        created_at = target.get("created_at")
    except Exception:
        created_at = None
    # Lightweight: ONE query for public guild tags. The old version called
    # get_user_communities(), which ran get_community_channels() per guild
    # (several sequential round trips over the remote Turso link) and made
    # the mini-profile hang on "Loading…".
    guilds = threads_db.get_user_public_guild_tags(uid)
    tags = [{
        "id": g["id"],
        "name": g["name"],
        "genre": g.get("genre"),
        "role": g.get("role") or "member",
    } for g in guilds]
    return jsonify({
        "success": True,
        "user": {
            "id": target["id"],
            "username": target["username"],
            "avatar": target.get("avatar"),
            "avatar_color": target.get("avatar_color") or "#8b5cf6",
        },
        "rank": rank,
        "xp": xp,
        "xp_pct": xp_pct,
        "joined_at": created_at,
        "guilds": tags,
    })


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