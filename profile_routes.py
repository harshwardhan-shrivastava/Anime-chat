import re

from flask import Blueprint, render_template, request, jsonify, g, url_for, flash, redirect, session, abort

from anime_data import anime_database
from database import (
    get_anime_stats,
    create_user_list,
    get_user_lists,
    ensure_default_lists,
    ensure_and_get_lists,
    get_user_list,
    rename_user_list,
    delete_user_list,
    add_to_user_list,
    remove_from_user_list,
    get_view_history,
    get_history_count,
    update_user_profile,
    get_user_xp,
    get_user_rank,
    MAX_USER_LISTS,
)

bp = Blueprint("profile", __name__)


def _pick_card(slug, skip_stats=False):
    """Build a pick dict shaped for the homepage _anime_card.html partial."""
    entry = anime_database.get(slug)
    if entry is None:
        return None
    if skip_stats:
        live_rating = entry.get("rating", "N/A")
    else:
        stats = get_anime_stats(slug)
        live_rating = stats["average"] if stats["votes"] > 0 else entry.get("rating", "N/A")
    return {
        "slug": slug,
        "title": entry.get("title") or slug,
        "image": entry.get("image") or "",
        "rating": entry.get("rating") or "N/A",
        "year": entry.get("release") or "",
        "genre": entry.get("genre") or "",
        "total_episodes": entry.get("total_episodes", 0) or 0,
        "member_count": entry.get("member_count", 0) or 0,
        "has_sub": bool(entry.get("subtitles")),
        "has_dub": any(
            str(d).strip().lower() == "english"
            for d in (entry.get("dub") or [])
        ),
        "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
        "live_rating": live_rating,
        "badge_label": "In List",
    }


def _require_user_json():
    """Return (user, error_response) — 401 JSON if logged out."""
    user = g.get("user")
    if user is None:
        return None, (jsonify({"success": False, "error": "login"}), 401)
    return user, None


def _user_lists(user_id):
    """Seed the default List 1-10 on first access, then return the lists.

    Uses a single DB connection instead of two separate ones (ensure →
    close → get → close), saving ~800 ms of Turso round-trip latency.
    """
    return ensure_and_get_lists(user_id)


def _list_pub(lst):
    """Public shape of a list for JSON/templates."""
    return {
        "id": lst["id"],
        "name": lst["name"],
        "created_at": lst.get("created_at", ""),
        "updated_at": lst.get("updated_at", ""),
        "item_count": len(lst.get("slugs") or []),
    }


@bp.route("/profile", methods=["GET", "POST"])
def profile():
    user = g.get("user")
    if user is None:
        flash("Log in to see your profile.", "error")
        return redirect(url_for("auth.login", next=request.path))

    # Settings tab: update username + avatar image (Tohoku-style).
    if request.method == "POST":
        from werkzeug.security import check_password_hash
        import database as db

        username = (request.form.get("username") or "").strip()
        # Sanitize hidden control characters and trim to 100 - any name works.
        username = re.sub(r"[\x00-\x1f\x7f\u200b-\u200d\ufeff]", "", username).strip()[:100]
        avatar = (request.form.get("avatar") or "profile1.png").strip()
        password = request.form.get("password") or ""

        full = db.get_user_by_id(user["id"])
        if not full or not check_password_hash(full["password_hash"], password):
            flash("Enter your current password to save changes.", "error")
            return redirect(url_for("profile.profile", tab="settings"))

        if not username:
            flash("Type a username first.", "error")
            return redirect(url_for("profile.profile", tab="settings"))

        other = db.get_user_by_username(username)
        if other and other["id"] != user["id"]:
            flash("That username is already taken.", "error")
            return redirect(url_for("profile.profile", tab="settings"))

        update_user_profile(user["id"], username, avatar)
        session["user_id"] = user["id"]
        flash("Profile updated!", "success")
        return redirect(url_for("profile.profile", tab="settings"))

    tab = request.args.get("tab", "history")
    if tab not in ("history", "lists", "settings", "reviews"):
        tab = "history"

    history = []
    history_count = 0
    if tab == "history":
        try:
            for row in get_view_history(user["id"], 60):
                pick = _pick_card(row["anime_slug"], skip_stats=True)
                if pick is None:
                    continue
                pick["badge_label"] = "Visited"
                pick["visited_at"] = row["viewed_at"]
                history.append(pick)
            history_count = get_history_count(user["id"])
        except Exception:
            history = []
            history_count = 0

    reviews = []
    if tab == "reviews":
        try:
            from database import get_user_review_history, get_bulk_review_likes
            reviews = get_user_review_history(user["id"], 50)
            for r in reviews:
                likes_data = get_bulk_review_likes(r["type"], [r["id"]])
                r["likes"] = likes_data.get(r["id"], {}).get("likes", 0)
                r["dislikes"] = likes_data.get(r["id"], {}).get("dislikes", 0)
        except Exception:
            reviews = []

    try:
        user_xp = get_user_xp(user["id"])
    except Exception:
        user_xp = 0
    try:
        user_rank = get_user_rank(user["id"])
    except Exception:
        user_rank = "D"
    try:
        lists = [_list_pub(lst) for lst in _user_lists(user["id"])]
    except Exception:
        lists = []
    if tab != "history":
        try:
            history_count = get_history_count(user["id"])
        except Exception:
            history_count = 0

    return render_template(
        "profile.html",
        user=user,
        tab=tab,
        history=history,
        history_count=history_count,
        reviews=reviews,
        user_xp=user_xp,
        user_rank=user_rank,
        lists=lists,
        max_lists=MAX_USER_LISTS,
        genres=_genre_list(),
    )


@bp.route("/profile/lists/<int:list_id>")
def profile_list(list_id):
    user = g.get("user")
    if user is None:
        flash("Log in to see your lists.", "error")
        return redirect(url_for("auth.login", next=request.path))

    lst = get_user_list(list_id, user["id"])
    if lst is None:
        flash("That list doesn't exist.", "error")
        return redirect(url_for("profile.profile", tab="lists"))

    items = []
    for slug in lst["slugs"]:
        pick = _pick_card(slug)
        if pick is not None:
            items.append(pick)

    return render_template(
        "profile_list.html",
        user=user,
        list_data=_list_pub(lst),
        items=items,
        genres=_genre_list(),
    )


@bp.route("/user/<username>")
def public_profile(username):
    """Public profile view for any user."""
    import database as db
    target = db.get_user_by_username(username)
    if target is None:
        flash("User not found.", "error")
        return redirect(url_for("home"))

    # Reviews are public feed content (they're on /reviews), so the profile
    # page for them is always visible — no is_public gate. Browsing history
    # stays private on the owner's own /profile?tab=history page.
    reviews = db.get_user_review_history(target["id"], 30)
    for r in reviews:
        likes_data = db.get_bulk_review_likes(r["type"], [r["id"]])
        r["likes"] = likes_data.get(r["id"], {}).get("likes", 0)
        r["dislikes"] = likes_data.get(r["id"], {}).get("dislikes", 0)

    user_xp = db.get_user_xp(target["id"])
    user_rank = db.get_user_rank(target["id"])
    history_count = db.get_history_count(target["id"])

    return render_template(
        "public_profile.html",
        target=target,
        reviews=reviews,
        user_xp=user_xp,
        user_rank=user_rank,
        history_count=history_count,
        genres=_genre_list(),
    )

@bp.route("/user/<username>/history")
def user_review_history(username):
    """Full review history for a user — anime + episode reviews, with sort
    (highest / newest / oldest) and a search-by-anime filter bar."""
    import database as db
    target = db.get_user_by_username(username)
    if target is None:
        return render_template(
            "user_not_found.html", username=username, requested_path="history"
        ), 404
    # No is_public gate here either: this page shows the same reviews the
    # public /reviews feed already shows. The "private" flag only ever
    # applied to browsing history, which never leaves /profile.
    sort = request.args.get("sort", "newest")
    if sort not in ("highest", "newest", "oldest"):
        sort = "newest"
    q = (request.args.get("q") or "").strip()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = 25

    reviews = db.get_user_review_history(target["id"], 400)
    if q:
        ql = q.casefold()
        reviews = [r for r in reviews if ql in (r.get("anime_title") or "").casefold()]
    if sort == "highest":
        reviews.sort(key=lambda r: r["rating"], reverse=True)
    elif sort == "oldest":
        reviews.sort(key=lambda r: (r.get("created_at") or ""))
    # "newest" keeps the DB's id-DESC order.

    total = len(reviews)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    start = (page - 1) * per_page
    page_reviews = reviews[start:start + per_page]
    anime_count = sum(1 for r in reviews if r["type"] == "anime")
    ep_count = sum(1 for r in reviews if r["type"] == "episode")

    for r in page_reviews:
        likes = db.get_bulk_review_likes(r["type"], [r["id"]])
        r["likes"] = likes.get(r["id"], {}).get("likes", 0)
        r["dislikes"] = likes.get(r["id"], {}).get("dislikes", 0)
        r["date_fmt"] = (r.get("created_at") or "")[:10]
        if r["type"] == "episode":
            r["season_idx"] = _resolve_season_idx(r["anime_slug"], r.get("season_name"))

    user_xp = db.get_user_xp(target["id"])
    user_rank = db.get_user_rank(target["id"])
    history_count = db.get_history_count(target["id"])
    return render_template(
        "user_history.html",
        target=target,
        page_reviews=page_reviews,
        anime_count=anime_count,
        ep_count=ep_count,
        total=total,
        sort=sort,
        q=q,
        page=page,
        pages=pages,
        user_xp=user_xp,
        user_rank=user_rank,
        history_count=history_count,
    )


def _resolve_season_idx(anime_slug, season_name):
    """Map a stored season_name (display name or raw index) to a 1-based
    season index for building episode links."""
    entry = anime_database.get(anime_slug)
    seasons = (entry or {}).get("seasons") or []
    try:
        idx = int(season_name)
        if 1 <= idx <= len(seasons):
            return idx
    except (TypeError, ValueError):
        pass
    for i, s in enumerate(seasons, 1):
        if s.get("name") == season_name:
            return i
    return 1


@bp.route("/api/lists", methods=["GET", "POST"])
def api_lists():
    user, err = _require_user_json()
    if err:
        return err

    if request.method == "GET":
        slug = (request.args.get("slug") or "").strip()
        lists = []
        for lst in _user_lists(user["id"]):
            pub = _list_pub(lst)
            if slug:
                pub["contains"] = slug in lst["slugs"]
            lists.append(pub)
        return jsonify({
            "success": True,
            "lists": lists,
            "count": len(lists),
            "max": MAX_USER_LISTS,
        })

    # POST — create a list
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name"}), 400
    if len(name) > 50:
        return jsonify({"success": False, "error": "too_long"}), 400

    existing = _user_lists(user["id"])
    if len(existing) >= MAX_USER_LISTS:
        return jsonify({"success": False, "error": "limit"}), 400

    created = create_user_list(user["id"], name)
    if created is None:
        return jsonify({"success": False, "error": "limit"}), 400
    return jsonify({"success": True, "list": _list_pub(created), "count": len(existing) + 1})


@bp.route("/api/lists/<int:list_id>", methods=["POST"])
def api_list_action(list_id):
    user, err = _require_user_json()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    if action == "rename":
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"success": False, "error": "name"}), 400
        if len(name) > 50:
            return jsonify({"success": False, "error": "too_long"}), 400
        if not rename_user_list(list_id, user["id"], name):
            return jsonify({"success": False, "error": "not_found"}), 404
        return jsonify({"success": True, "name": name})
    if action == "delete":
        if not delete_user_list(list_id, user["id"]):
            return jsonify({"success": False, "error": "not_found"}), 404
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "action"}), 400


@bp.route("/api/lists/<int:list_id>/items", methods=["POST"])
def api_list_add_item(list_id):
    user, err = _require_user_json()
    if err:
        return err

    slug = (request.form.get("slug") or "").strip()
    if not slug or anime_database.get(slug) is None:
        return jsonify({"success": False, "error": "slug"}), 400

    added = add_to_user_list(list_id, user["id"], slug)
    if added is None:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "added": bool(added), "contains": True})


@bp.route("/api/lists/<int:list_id>/items/<anime_slug>", methods=["DELETE"])
def api_list_remove_item(list_id, anime_slug):
    user, err = _require_user_json()
    if err:
        return err

    removed = remove_from_user_list(list_id, user["id"], anime_slug)
    if removed is False:
        return jsonify({"success": False, "error": "not_found"}), 404
    return jsonify({"success": True, "contains": False})


def _genre_list():
    """Top genres for the navbar category dropdown (matches app.py)."""
    from collections import Counter

    counter = Counter()
    for entry in anime_database.values():
        for genre in entry.get("genre", "").split(" • "):
            genre = genre.strip()
            if genre and genre.lower() != "anime":
                counter[genre] += 1
    return [g for g, _ in counter.most_common(20)]
