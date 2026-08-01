from flask import Blueprint, request, jsonify, g

import database
from services import giphy

chat_bp = Blueprint("chat_bp", __name__)

MAX_MESSAGE_LENGTH = 800


@chat_bp.route("/community/<anime_slug>/messages", methods=["GET"])
def get_messages(anime_slug):
    after_id = request.args.get("after_id", 0, type=int)
    messages = database.get_chat_messages(anime_slug, after_id=after_id)

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

    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "text")
    content = (data.get("content") or "").strip()

    if kind not in ("text", "gif"):
        return jsonify({"success": False, "error": "Invalid message type."}), 400

    if not content:
        return jsonify({"success": False, "error": "Message can't be empty."}), 400

    if kind == "text" and len(content) > MAX_MESSAGE_LENGTH:
        content = content[:MAX_MESSAGE_LENGTH]

    if kind == "gif" and not content.startswith(("http://", "https://")):
        return jsonify({"success": False, "error": "Invalid gif."}), 400

    message = database.add_chat_message(
        anime_slug,
        g.user["id"],
        g.user["username"],
        g.user["avatar_color"],
        kind,
        content,
    )

    database.touch_presence(anime_slug, g.user["id"], g.user["username"], g.user["avatar_color"])

    return jsonify({"success": True, "message": message})


@chat_bp.route("/community/<anime_slug>/presence", methods=["GET"])
def presence(anime_slug):
    if g.get("user"):
        database.touch_presence(anime_slug, g.user["id"], g.user["username"], g.user["avatar_color"])

    online = database.get_online_users(anime_slug)
    return jsonify({"success": True, "count": len(online), "members": online})


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
