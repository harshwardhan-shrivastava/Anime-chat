"""Standalone War Zone routes (Free / Friendly wars) - Phase 1.

A war is its own battlefield (not anchored to a review's Reply War). The
creator posts a declaration, any C+ user enters one battler, and the crowd
votes by like-ratio. Guild / GvG duels (declaration -> claim -> owner accept
-> VS board + guild XP) layer on top of this engine later.

Kept as a Flask blueprint so app.py can register it from a reachable spot.
"""
import time

from flask import Blueprint, g, request, jsonify, abort, render_template

from anime_data import anime_database
from database import (
    create_warzone,
    get_warzones,
    get_warzone,
    add_warzone_entry,
    get_review_likes,
    get_user_rank,
)

wz_bp = Blueprint("wz", __name__)

_rate = {}


def _hit(key, limit=40, window=300):
    now = int(time.time())
    arr = [t for t in _rate.get(key, []) if now - t < window]
    if len(arr) >= limit:
        return True
    arr.append(now)
    _rate[key] = arr
    return False


def _require_rank(user):
    rank = get_user_rank(user["id"])
    if rank not in ("C", "B", "A", "S", "S+"):
        return None
    return rank


@wz_bp.route("/api/warzone/create", methods=["POST"])
def warzone_create():
    """Create a standalone war. C rank and above only - D accounts can't
    drag the site into battlefields they don't belong in."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to create a war."}), 401
    if _require_rank(user) is None:
        return jsonify({
            "success": False,
            "error": "Creating a war requires C rank (500 XP) - keep getting likes to unlock it.",
        }), 403
    if _hit("u:" + str(user["id"])) or _hit("ip:" + (request.remote_addr or "?"), 120, 300):
        return jsonify({"success": False, "error": "You're posting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    try:
        hours = int(data.get("hours") or 24)
    except (TypeError, ValueError):
        hours = 24
    topic_type = data.get("topic_type") or "blank"
    if topic_type not in ("blank", "anime", "episode", "gif"):
        topic_type = "blank"
    ok, err, wid = create_warzone(
        user["id"],
        data.get("title"),
        data.get("declaration"),
        hours=hours,
        is_private=bool(data.get("is_private")),
        topic_type=topic_type,
        anime_slug=(data.get("anime_slug") or "").strip()[:120] or None,
        episode_ref=(data.get("episode_ref") or "").strip()[:120] or None,
        gif_url=(data.get("gif_url") or "").strip()[:500] or None,
    )
    if not ok:
        return jsonify({"success": False, "error": err or "Could not create the war."}), 400
    return jsonify({"success": True, "war_id": wid, "url": "/war/zone/{}".format(wid)})


@wz_bp.route("/war/zone/<int:wid>")
def warzone_detail(wid):
    """A standalone war's duel board: the declaration on top, every battler
    below, the live leader (or the crowned winner once the timer ends)."""
    user = g.get("user")
    war = get_warzone(wid, user["id"] if user else None)
    if not war:
        abort(404)
    my_entry = None
    if user:
        my_entry = next((e for e in war["entries"] if e["user_id"] == user["id"]), None)
    user_rank = get_user_rank(user["id"]) if user else None
    entry = anime_database.get(war["anime_slug"]) if war.get("anime_slug") else None
    topic = {
        "title": (entry.get("title") if entry else None) or war.get("anime_slug"),
        "image": (entry.get("image") if entry else None) or (war.get("gif_url") if war.get("gif_url") else None),
    }
    return render_template(
        "war_zone.html",
        war=war,
        topic=topic,
        my_entry=my_entry,
        user_rank=user_rank,
        current_user=user,
    )


@wz_bp.route("/api/warzone/<int:wid>/enter", methods=["POST"])
def warzone_enter(wid):
    """Enter one battler into a standalone war - C rank and above, one per
    user per war."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to enter the war."}), 401
    if _require_rank(user) is None:
        return jsonify({
            "success": False,
            "error": "War entries require C rank (500 XP) - keep getting likes to unlock it.",
        }), 403
    if _hit("u:" + str(user["id"])) or _hit("ip:" + (request.remote_addr or "?"), 120, 300):
        return jsonify({"success": False, "error": "You're posting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    ok, err, entry = add_warzone_entry(user["id"], wid, data.get("content"))
    if not ok:
        return jsonify({"success": False, "error": err or "Could not enter the war."}), 400
    return jsonify({"success": True, "entry": entry})


@wz_bp.route("/api/warzone/<int:eid>/vote", methods=["POST"])
def warzone_vote(eid):
    """Vote on a war battler - the whole crowd decides. Dislikes stay C+.

    We route through app's gated/commit-first toggle (lazy import to avoid
    a circular import) so the C+ dislike rule and the SQLite deadlock fix
    both apply to war votes exactly like review votes."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to vote."}), 401
    if _hit("u:" + str(user["id"])) or _hit("ip:" + (request.remote_addr or "?"), 120, 300):
        return jsonify({"success": False, "error": "You're voting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    is_like = data.get("is_like")
    if is_like is None:
        return jsonify({"success": False, "error": "Missing vote type."}), 400
    try:
        from app import _gated_toggle_review_like as toggle
        new_is_like, removed = toggle(user["id"], "warzone", eid, bool(is_like))
    except PermissionError as exc:
        return jsonify({"success": False, "error": str(exc)}), 403
    except Exception:
        return jsonify({"success": False, "error": "Entry not found."}), 404
    counts = get_review_likes("warzone", eid)
    return jsonify({
        "success": True,
        "likes": counts["likes"],
        "dislikes": counts["dislikes"],
        "user_vote": None if removed else (1 if new_is_like else 0),
    })