import re

from flask import Blueprint, request, jsonify, g

import database
from services import giphy

chat_bp = Blueprint("chat_bp", __name__)

MAX_MESSAGE_LENGTH = 800
MAX_GIF_URL_LENGTH = 500
# A gif URL is rendered straight into an <img src>, so keep it to a plain
# https URL: no quotes, angle brackets, whitespace or javascript:/data: URIs.
GIF_URL_RE = re.compile(r"^https://[^\s\"'<>\\]+$")


@chat_bp.route("/community/<anime_slug>/messages", methods=["GET"])
def get_messages(anime_slug):
    after_id = request.args.get("after_id", 0, type=int)
    messages = database.get_chat_messages(
        anime_slug,
        after_id=after_id,
        user_id=g.user["id"] if g.get("user") else None,
    )

    # Anyone polling for messages while logged in counts as "present" here --
    # this is what powers the real online-count/member list.
    if g.get("user"):
        database.touch_presence(anime_slug, g.user["id"], g.user["username"], g.user["avatar_color"])

    return jsonify({
        "success": True,
        "messages": messages,
        "you": g.user if g.get("user") else None,
    })


@chat_bp.route("/community/<anime_slug>/messages", methods=["POST"])
def post_message(anime_slug):
    if not g.get("user"):
        return jsonify({"success": False, "error": "You need to log in to chat."}), 401

    if not g.user["is_verified"]:
        return jsonify({"success": False, "error": "Verify your email before chatting."}), 403

    if not database.is_community_member(anime_slug, g.user["id"]):
        return jsonify({"success": False, "error": "Join this community first to chat."}), 403

    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "text")
    content = (data.get("content") or "").strip()
    try:
        reply_to = int(data.get("reply_to")) if data.get("reply_to") not in (None, "") else None
    except (TypeError, ValueError):
        reply_to = None
    if reply_to is not None and reply_to <= 0:
        reply_to = None

    if kind not in ("text", "gif", "anime"):
        return jsonify({"success": False, "error": "Invalid message type."}), 400

    if not content:
        return jsonify({"success": False, "error": "Message can't be empty."}), 400

    if kind == "text" and len(content) > MAX_MESSAGE_LENGTH:
        content = content[:MAX_MESSAGE_LENGTH]

    if kind == "gif" and (len(content) > MAX_GIF_URL_LENGTH or not GIF_URL_RE.match(content)):
        return jsonify({"success": False, "error": "Invalid gif."}), 400

    if kind == "anime" and not content:
        return jsonify({"success": False, "error": "Anime slug required."}), 400

    # Single DB connection for message + presence (saves ~800 ms).
    message = database.add_chat_message_with_presence(
        anime_slug,
        g.user["id"],
        g.user["username"],
        g.user["avatar_color"],
        kind,
        content,
        reply_to=reply_to,
    )

    return jsonify({"success": True, "message": message})


@chat_bp.route("/community/<anime_slug>/presence", methods=["GET"])
def presence(anime_slug):
    if g.get("user"):
        database.touch_presence(anime_slug, g.user["id"], g.user["username"], g.user["avatar_color"])

    online = database.get_online_users(anime_slug)
    return jsonify({"success": True, "count": len(online), "members": online})


@chat_bp.route("/community/<anime_slug>/messages/<int:message_id>/react", methods=["POST"])
def react_message(anime_slug, message_id):
    if not g.get("user"):
        return jsonify({"success": False, "error": "You need to log in to react."}), 401

    data = request.get_json(silent=True) or {}
    emoji = (data.get("emoji") or "").strip()
    if not emoji or len(emoji) > 8:
        return jsonify({"success": False, "error": "Invalid emoji."}), 400

    message = database.get_chat_message(message_id)
    if message is None or message["anime_slug"] != anime_slug:
        return jsonify({"success": False, "error": "Message not found."}), 404

    result = database.toggle_reaction(message_id, g.user["id"], emoji)
    reactions, my_reactions = database.get_message_reactions(message_id, g.user["id"])

    return jsonify({
        "success": True,
        "added": result["added"],
        "reaction_id": result["reaction_id"],
        "reactions": reactions,
        "my_reactions": my_reactions,
    })


@chat_bp.route("/community/<anime_slug>/reactions", methods=["GET"])
def reaction_updates(anime_slug):
    after_id = request.args.get("after_id", 0, type=int)
    data = database.get_reactions_since(
        after_id,
        user_id=g.user["id"] if g.get("user") else None,
    )
    return jsonify({"success": True, **data})


@chat_bp.route("/community/<anime_slug>/gifs", methods=["GET"])
def chat_gifs(anime_slug):
    gifs = database.get_chat_gifs(anime_slug)
    return jsonify({"success": True, "gifs": gifs})


@chat_bp.route("/api/gif-search", methods=["GET"])
def gif_search():
    query = (request.args.get("q") or "").strip()

    try:
        offset = int(request.args.get("offset", 0))
    except ValueError:
        offset = 0

    if not giphy.is_configured():
        return jsonify({
            "success": False,
            "error": "GIPHY_API_KEY is not configured.",
        }), 503

    try:
        if query:
            raw = giphy.search(query, offset=offset)
        else:
            raw = giphy.trending(offset=offset)
    except Exception as exc:
        return jsonify({
            "success": False,
            "error": f"GIPHY request failed: {exc}"
        }), 502

    if "error" in raw:
        return jsonify({
            "success": False,
            "error": raw["error"]
        }), 502

    return jsonify({
        "success": True,
        **giphy.simplify(raw)
    })


# ------------------------------------------------------------------
#  Community membership
# ------------------------------------------------------------------

@chat_bp.route("/community/<anime_slug>/join", methods=["POST"])
def join_community_route(anime_slug):
    if not g.get("user"):
        return jsonify({"success": False, "error": "Login required."}), 401
    if not g.user["is_verified"]:
        return jsonify({"success": False, "error": "Verify your email first."}), 403
    database.join_community(anime_slug, g.user["id"])
    count = database.get_community_member_count(anime_slug)
    database.insert_system_message(anime_slug, g.user["username"] + " joined the community", user_id=g.user["id"])
    return jsonify({"success": True, "member_count": count, "joined": True})


@chat_bp.route("/community/<anime_slug>/leave", methods=["POST"])
def leave_community_route(anime_slug):
    if not g.get("user"):
        return jsonify({"success": False, "error": "Login required."}), 401
    database.leave_community(anime_slug, g.user["id"])
    count = database.get_community_member_count(anime_slug)
    return jsonify({"success": True, "member_count": count, "joined": False})


@chat_bp.route("/community/<anime_slug>/members", methods=["GET"])
def get_members_route(anime_slug):
    members = database.get_community_members(anime_slug)
    count = len(members)
    is_member = False
    if g.get("user"):
        is_member = database.is_community_member(anime_slug, g.user["id"])
    return jsonify({
        "success": True,
        "members": members,
        "count": count,
        "is_member": is_member,
    })
