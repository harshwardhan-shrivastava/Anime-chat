import calendar
import functools
import json
import os
import random
import re
import threading
import time

import requests

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, g, url_for, flash, redirect

from anime_data import anime_database
from characters_data import characters_index, search_characters, reload_characters
from database import (
    create_tables,
    get_connection,
    get_anime_stats,
    get_all_anime_stats,
    add_review,
    add_episode_review,
    get_episode_stats,
    get_user_episode_review,
    get_all_episode_stats,
    save_quiz_result,
    get_latest_quiz_result,
)
from auth import auth, load_logged_in_user
from chat import chat_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

app.register_blueprint(auth)
app.register_blueprint(chat_bp)


@app.before_request
def _attach_user():
    load_logged_in_user()


@app.context_processor
def _inject_user():
    return {"current_user": g.get("user")}


def _hd_anilist_url(image):
    """Upgrade AniList cover URLs from the light medium (~230px) flavor to
    the large (~460px) HD flavor. Pure URL rewrite — the image file is the
    same, only the flavor folder differs. TVmaze/Kitsu URLs pass through
    untouched (they have their own flavors, handled by the data layer)."""
    return image.replace("/cover/medium/", "/cover/large/")


@app.template_filter("anime_img")
def anime_img(image):
    """Templates call {{ image | anime_img }}. Local filenames resolve to
    /static/images/anime/<name>; full URLs pass through, with AniList cover
    URLs upgraded from medium to large so every poster renders in HD — even
    when the catalog data holds the lighter medium flavor."""
    if image.startswith(("http://", "https://")):
        return _hd_anilist_url(image)
    return url_for("static", filename="images/anime/" + image)


@app.template_filter("anime_img_large")
def anime_img_large(image):
    """Like anime_img — AniList cover URLs are served in the large (HD)
    flavor. Kept as a separate filter so the detail-page hero stays explicit
    about serving HD where it matters most."""
    return anime_img(image)


# ---------------------------------------------------------------------------
# Streaming provider branding + helpers
# ---------------------------------------------------------------------------

_PROVIDER_BRANDS = [
    ("crunchyroll", "crunch", "Cr", "#f47521"),
    ("netflix", "netflix", "N", "#e50914"),
    ("prime video", "prime", "P", "#00a8e1"),
    ("disney", "disney", "D+", "#113ccf"),
    ("u-next", "unext", "U", "#6a3ab2"),
    ("danime", "danime", "dA", "#ff4d9d"),
    ("anime times", "animetimes", "AT", "#9b4dca"),
    ("toei", "toei", "T", "#e8562a"),
    ("hidive", "hidive", "HD", "#9b4dca"),
    ("fod", "fod", "FOD", "#1d6fd6"),
    ("hulu", "hulu", "H", "#1ce783"),
    ("tubi", "tubi", "T", "#ff6a00"),
    ("pluto", "pluto", "P", "#d81f26"),
    ("youtube", "youtube", "YT", "#ff0000"),
    ("hbo", "hbo", "M", "#7a4ff0"),
    ("peacock", "peacock", "P", "#fdb913"),
    ("paramount", "paramount", "P+", "#0064ff"),
    ("viki", "viki", "V", "#e50914"),
    ("retrocrush", "retrocrush", "RC", "#ff7a00"),
    ("philo", "philo", "PH", "#e21b1b"),
    ("fubo", "fubo", "f", "#c8102e"),
    ("spectrum", "spectrum", "S", "#ff0000"),
    ("kanopy", "kanopy", "K", "#1a73e8"),
    ("adult swim", "adultswim", "AS", "#ffd100"),
    ("the cw", "cw", "CW", "#00a0d8"),
    ("amc", "amc", "AMC", "#e50914"),
    ("cineverse", "cineverse", "C", "#d62828"),
    ("midnight pulp", "midnightpulp", "MP", "#2b2b2b"),
    ("amasian", "amasian", "A", "#e50914"),
    ("fawesome", "fawesome", "F", "#ff8c00"),
    ("apple", "apple", "TV", "#0a0a0a"),
    ("amazon", "amazon", "a", "#ff9900"),
]


@app.template_filter("provider_brand")
def provider_brand(name):
    n = (name or "").lower()
    for key, cls, mark, color in _PROVIDER_BRANDS:
        if key in n:
            return {"cls": cls, "mark": mark, "color": color}
    return {"cls": "generic", "mark": (name or "TV")[:2].upper(), "color": "#5a6a7a"}


@app.template_filter("sort_streaming")
def sort_streaming(services):
    order = {"Streaming": 0, "Free": 1, "Free with Ads": 2, "Rent": 3, "Buy": 4}
    return sorted(
        services or [],
        key=lambda s: (order.get(s.get("monetization"), 5), (s.get("name") or "").lower()),
    )


@app.template_filter("real_dubs")
def real_dubs(dubs):
    return [d for d in (dubs or []) if str(d).strip().lower() not in ("japanese", "ja", "japanese (original)")]


# ---------------------------------------------------------------------------
# Live airing-schedule refresher
# ---------------------------------------------------------------------------

_SCHEDULE_TTL = 1800
_SCHEDULE_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      status
      startDate { year month day }
      nextAiringEpisode { episode airingAt timeUntilAiring }
    }
  }
}
"""
_SCHEDULE_STATUS_MAP = {
    "FINISHED": "Completed",
    "RELEASING": "Ongoing",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}

_schedule_state = {"last": 0.0, "running": False}
_schedule_lock = threading.Lock()

_BY_AID = {
    e["anilist_id"]: (slug, e)
    for slug, e in anime_database.items()
    if e.get("anilist_id")
}


def _save_fresh_airing_cache(fresh):
    from scripts.enrich_airing import load_json, save_json, _cache_files

    cache = {}
    for fname in _cache_files():
        cache.update(load_json(fname) or {})

    changed = False
    for aid_s, rec in fresh.items():
        old = cache.get(aid_s) or {}
        merged = dict(old)
        for key in ("status", "episodes", "startDate", "nextAiringEpisode"):
            if key in rec:
                merged[key] = rec[key]
        if merged != old:
            cache[aid_s] = merged
            changed = True

    if changed:
        try:
            save_json(
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "anime_airing_a0.json"),
                cache,
            )
        except Exception as exc:
            print(f"[schedule] failed to persist airing cache: {exc}", flush=True)


def _refresh_airing_schedule_worker():
    ids, seen = [], set()
    for entry in anime_database.values():
        if entry.get("status") in ("Ongoing", "Upcoming"):
            aid = entry.get("anilist_id")
            if aid and aid not in seen:
                seen.add(aid)
                ids.append(aid)

    fresh = {}
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            resp = requests.post(
                "https://graphql.anilist.co",
                json={"query": _SCHEDULE_QUERY, "variables": {"ids": batch}},
                timeout=20,
            )
            if resp.status_code != 200:
                continue
            for m in resp.json().get("data", {}).get("Page", {}).get("media", []):
                hit = _BY_AID.get(m.get("id"))
                if not hit:
                    continue
                _, entry = hit
                fresh[str(m.get("id"))] = {
                    "status": m.get("status"),
                    "startDate": m.get("startDate") or {},
                    "nextAiringEpisode": m.get("nextAiringEpisode") or {},
                }
                st = _SCHEDULE_STATUS_MAP.get(m.get("status"))
                if st:
                    entry["status"] = st
                nxt = m.get("nextAiringEpisode") or {}
                if nxt.get("airingAt"):
                    entry["next_episode_at"] = nxt["airingAt"]
                    entry["next_episode"] = nxt.get("episode") or entry.get("next_episode")
                else:
                    entry["next_episode_at"] = None
                sd = m.get("startDate") or {}
                if sd.get("year"):
                    entry["start_year"] = sd["year"]
                if sd.get("month"):
                    entry["start_month"] = sd["month"]
        except Exception:
            continue
        time.sleep(1.0)

    if fresh:
        _save_fresh_airing_cache(fresh)

    with _schedule_lock:
        _schedule_state["last"] = time.time()
        _schedule_state["running"] = False


def _ensure_airing_schedule():
    with _schedule_lock:
        stale = time.time() - _schedule_state["last"] > _SCHEDULE_TTL
        if stale and not _schedule_state["running"]:
            _schedule_state["running"] = True
            threading.Thread(
                target=_refresh_airing_schedule_worker, daemon=True
            ).start()


def _schedule_loop():
    while True:
        _ensure_airing_schedule()
        time.sleep(_SCHEDULE_TTL)


# ---------------------------------------------------------------------------
# Full auto-enrichment (airing + TVmaze + HD upgrade) every 10 minutes
# ---------------------------------------------------------------------------

_ENRICH_TTL = 600
_enrich_state = {"last": 0.0, "running": False}
_enrich_lock = threading.Lock()


def _full_enrich_worker():
    from anime_data import reload_database

    try:
        from scripts.enrich_airing import apply_airing, tvmaze_backfill

        try:
            apply_airing()
        except Exception as exc:
            print(f"[auto-enrich] apply_airing failed (continuing): {exc}", flush=True)

        try:
            reload_database()
        except Exception as exc:
            print(f"[auto-enrich] reload after apply failed: {exc}", flush=True)

        try:
            tvmaze_backfill(
                count=0,
                todo_path="anime_airing_todo.json",
                cross_path="anime_ep_thumbs_crosstodo.json",
            )
        except Exception as exc:
            print(f"[auto-enrich] tvmaze_backfill failed (continuing): {exc}", flush=True)

        try:
            from scripts.upgrade_thumbs_to_hd import main as hd_upgrade
            hd_upgrade()
        except Exception as exc:
            print(f"[auto-enrich] hd_upgrade failed (continuing): {exc}", flush=True)

        try:
            from scripts.fetch_characters import run_slice
            run_slice(budget_seconds=150)
        except Exception as exc:
            print(f"[auto-enrich] character slice failed (continuing): {exc}", flush=True)

        reload_database()
        reload_characters()

        _BY_AID.clear()
        _BY_AID.update({
            e["anilist_id"]: (slug, e)
            for slug, e in anime_database.items()
            if e.get("anilist_id")
        })
        print("[auto-enrich] Full enrichment completed successfully", flush=True)
    except Exception as exc:
        print(f"[auto-enrich] Error during enrichment: {exc}", flush=True)
    finally:
        with _enrich_lock:
            _enrich_state["last"] = time.time()
            _enrich_state["running"] = False


def _full_enrich_loop():
    time.sleep(120)
    while True:
        with _enrich_lock:
            if not _enrich_state["running"]:
                _enrich_state["running"] = True
                threading.Thread(
                    target=_full_enrich_worker, daemon=True
                ).start()
        time.sleep(_ENRICH_TTL)


SORT_TITLES = {
    "new": "Airing Now",
    "latest": "Latest Releases",
    "popular": "Most Popular",
    "trending": "Trending Now",
    "critics": "Critics' Picks",
    "underrated": "Hidden Gems (Underrated)",
    "upcoming": "Upcoming",
}


@functools.lru_cache(maxsize=1)
def _cached_genres():
    from collections import Counter

    counter = Counter()
    for entry in anime_database.values():
        for genre in entry.get("genre", "").split(" • "):
            genre = genre.strip()
            if genre and genre.lower() != "anime":
                counter[genre] += 1
    return [g for g, _ in counter.most_common(20)]


def _genre_list():
    return _cached_genres()


def _sort_value(entry, sort):
    rating = entry.get("rating")
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = 0.0
    popularity = entry.get("member_count", 0) or 0
    year = entry.get("release", "")
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = 0

    if sort == "popular":
        return (popularity, rating)
    if sort == "critics":
        return (rating, popularity)
    if sort == "underrated":
        return (rating, -popularity)
    if sort == "trending":
        return (year, popularity)
    return (year, popularity)


def _catalog_entries(sort="latest", genre=None, limit=None):
    all_stats = get_all_anime_stats()
    _ensure_airing_schedule()

    entries = []
    for slug, entry in anime_database.items():
        if genre:
            genres = entry.get("genre", "").lower()
            if genre.lower() not in genres:
                continue

        status = entry.get("status", "")
        if sort == "new" and status != "Ongoing":
            continue
        if sort == "upcoming" and status != "Upcoming":
            continue
        if sort == "latest" and status == "Upcoming":
            continue

        stats = all_stats.get(slug, {"votes": 0, "average": 0})
        live_rating = stats["average"] if stats["votes"] > 0 else entry.get("rating", "N/A")

        entries.append({
            "slug": slug,
            "title": entry.get("title", slug),
            "image": entry.get("image", ""),
            "live_rating": live_rating,
            "live_votes": stats["votes"],
            "member_count": entry.get("member_count", 0) or 0,
            "rating": entry.get("rating", "N/A"),
            "release": entry.get("release", ""),
            "genre": entry.get("genre", ""),
            "status": status,
            "next_episode": entry.get("next_episode"),
            "next_episode_at": entry.get("next_episode_at"),
            "start_year": entry.get("start_year"),
            "start_month": entry.get("start_month"),
            "total_episodes": entry.get("total_episodes", 0) or 0,
            "has_dub": any(
                str(d).strip().lower() == "english"
                for d in (entry.get("dub") or [])
            ),
            "has_sub": bool(entry.get("subtitles")),
            "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
        })

    if sort in ("new", "upcoming"):
        if sort == "new":
            entries.sort(key=lambda e: (0, e["next_episode_at"] or 0)
                         if e["next_episode_at"] else (1, 0))
        else:
            entries.sort(key=lambda e: (0, e["start_year"] or 0, e["start_month"] or 0)
                         if e["start_year"] else (1, 0, 0))
    else:
        entries.sort(key=lambda e: _sort_value(e, sort), reverse=True)

    if limit:
        entries = entries[:limit]
    return entries


def _episode_badge(entry):
    at = entry.get("next_episode_at")
    n = entry.get("next_episode")
    if not at or not n:
        return "AIRING NOW"
    delta = at - time.time()
    if delta <= 0:
        return f"EP {n} JUST AIRED"
    days = delta / 86400
    if days < 1:
        return f"EP {n} TODAY"
    if days < 2:
        return f"EP {n} TOMORROW"
    return f"EP {n} IN {int(days)}D"


def _start_badge(entry):
    y = entry.get("start_year")
    m = entry.get("start_month")
    if y and m and 1 <= m <= 12:
        return f"EXP {calendar.month_abbr[m].upper()} {y}"
    if y:
        return f"EXP {y}"
    return "UPCOMING"


def _decorate(entries, sort):
    for entry in entries:
        if sort == "new":
            entry["badge_label"] = _episode_badge(entry)
        elif sort == "upcoming":
            entry["badge_label"] = _start_badge(entry)
    return entries


@app.route("/")
def home():
    latest = _catalog_entries(sort="latest", limit=48)
    return render_template(
        "index.html",
        anime_list=latest,
        page_title="Latest Releases",
        genres=_genre_list(),
    )


@app.route("/browse")
def browse():
    sort = request.args.get("sort", "popular")
    if sort not in SORT_TITLES:
        sort = "popular"
    entries = _decorate(_catalog_entries(sort=sort), sort)
    return render_template(
        "browse.html",
        anime_list=entries,
        page_title=SORT_TITLES[sort],
        active_sort=sort,
        sort_titles=SORT_TITLES,
        genres=_genre_list(),
    )


@app.route("/category/<genre>")
def category(genre):
    entries = _decorate(_catalog_entries(sort="popular", genre=genre), "popular")
    return render_template(
        "browse.html",
        anime_list=entries,
        page_title=f"{genre} Anime",
        active_genre=genre,
        sort_titles=SORT_TITLES,
        genres=_genre_list(),
    )


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"success": True, "results": []})

    results = []
    _APOS = str.maketrans({"'": "", "\u2019": "", "\u2018": ""})
    _SEP_RE = re.compile(r"[\s\-\u2013\u2014:;,.!?/\\()\[\]\"]+")

    def _norm(s):
        return _SEP_RE.sub(" ", s.translate(_APOS).lower()).strip()

    qn = _norm(q)
    words = [w for w in qn.split() if w]

    def _matches(title):
        tn = _norm(title)
        if not qn:
            return False
        if qn in tn:
            return True
        return len(words) > 1 and all(w in tn for w in words)

    for slug, entry in anime_database.items():
        title = entry.get("title", "")
        if _matches(title):
            results.append({
                "slug": slug,
                "title": title,
                "image": entry.get("image", ""),
                "year": entry.get("release", ""),
                "rating": entry.get("rating", "N/A"),
            })
        if len(results) >= 12:
            break

    return jsonify({"success": True, "results": results})


@app.route("/anime/<anime_slug>")
def anime(anime_slug):
    anime = anime_database.get(anime_slug)
    if anime is None:
        return "Anime not found", 404
    return render_template(
        "anime.html",
        anime=anime,
        next_episode_label=_episode_badge(anime),
        episode_stats=get_all_episode_stats(anime_slug),
    )


def _find_episode(anime_slug, season_idx, episode_number):
    anime = anime_database.get(anime_slug)
    if anime is None:
        return None, None, None, None, None
    seasons = anime.get("seasons") or []
    if season_idx < 1 or season_idx > len(seasons):
        return anime, None, None, None, None
    season = seasons[season_idx - 1]
    episode = next(
        (e for e in (season.get("episodes") or []) if e.get("number") == episode_number),
        None,
    )
    if episode is None:
        return anime, season, None, None, None
    season_name = season.get("name", f"Season {season_idx}")
    episode_title = episode.get("title") or f"Episode {episode_number}"
    return anime, season, episode, season_name, episode_title


@app.route("/anime/<anime_slug>/episode/<int:season_idx>/<int:episode_number>", methods=["GET", "POST"])
def episode_rate(anime_slug, season_idx, episode_number):
    anime, season, episode, season_name, episode_title = _find_episode(
        anime_slug, season_idx, episode_number
    )
    if anime is None:
        return "Anime not found", 404
    if season is None:
        return "Season not found", 404
    if episode is None:
        return "Episode not found", 404

    if request.method == "POST":
        user = g.get("user")
        if user is None:
            flash("Log in to rate this episode.", "error")
            return redirect(url_for("auth.login", next=request.path))
        try:
            rating = int(request.form.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        if rating < 1 or rating > 10:
            flash("Please pick a star rating between 1 and 10.", "error")
            return redirect(url_for("episode_rate", anime_slug=anime_slug,
                                    season_idx=season_idx,
                                    episode_number=episode_number))
        comment = (request.form.get("comment") or "").strip()[:1000]
        add_episode_review(
            anime_slug, season_name, episode_number,
            user["id"], user["username"], user["avatar_color"],
            rating, comment,
        )
        flash(f"Thanks for rating {episode_title}!", "success")
        return redirect(url_for("episode_rate", anime_slug=anime_slug,
                                season_idx=season_idx,
                                episode_number=episode_number))

    stats = get_episode_stats(anime_slug, season_name, episode_number)
    user = g.get("user")
    my_review = get_user_episode_review(
        anime_slug, season_name, episode_number, user["id"] if user else None
    )
    return render_template(
        "episode_rate.html",
        anime=anime,
        season=season,
        season_idx=season_idx,
        season_name=season_name,
        episode=episode,
        episode_number=episode_number,
        episode_title=episode_title,
        stats=stats,
        my_review=my_review,
    )


@app.route("/community/<anime_slug>")
def community(anime_slug):
    entry = anime_database.get(anime_slug)
    if entry is None:
        return "Anime not found", 404
    return render_template(
        "community.html",
        anime_name=entry.get("title", anime_slug),
        anime_image=entry.get("image", ""),
        anime_slug=anime_slug
    )


@app.route("/anime-reviews/<anime_slug>", methods=["GET"])
def anime_reviews(anime_slug):
    if anime_slug not in anime_database:
        return jsonify({"success": False, "error": "Anime not found"}), 404
    stats = get_anime_stats(anime_slug)
    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
        "reviews": stats["reviews"],
    })


@app.route("/rate-anime", methods=["POST"])
def rate_anime():
    data = request.get_json(silent=True) or {}
    anime_slug = data.get("anime_slug")
    rating = data.get("rating")
    username = (data.get("username") or "Anonymous").strip()[:40] or "Anonymous"
    comment = (data.get("comment") or "").strip()[:1000]

    if not anime_slug or anime_slug not in anime_database:
        return jsonify({"success": False, "error": "Unknown anime"}), 404
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Rating must be a number"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"success": False, "error": "Rating must be between 1 and 5"}), 400

    add_review(anime_slug, username, rating, comment)
    stats = get_anime_stats(anime_slug)
    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
        "reviews": stats["reviews"],
    })


def _char_public(entries):
    return [
        {k: v for k, v in e.items() if not k.startswith("_")}
        for e in entries
    ]


@app.route("/characters")
def characters():
    initial = search_characters("", 0, 60)
    total = len(characters_index)
    covered = len({e["slug"] for e in characters_index})
    with_va = sum(1 for e in characters_index if e.get("jp") or e.get("en"))

    def _fmt(n):
        return f"{n:,}" if n >= 1000 else str(n)

    payload = {
        "initial": _char_public(initial),
        "total": total,
        "covered": covered,
        "with_va": with_va,
    }
    return render_template(
        "characters.html",
        initial=initial,
        total_fmt=_fmt(total),
        covered_fmt=_fmt(covered),
        with_va_fmt=_fmt(with_va),
        characters_data_json=json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"),
        genres=_genre_list(),
    )


@app.route("/api/characters/search")
def api_characters_search():
    q = (request.args.get("q") or "").strip()
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = min(120, max(1, int(request.args.get("limit", 60))))
    except (TypeError, ValueError):
        limit = 60

    results = search_characters(q, offset, limit)
    results = _char_public(results)
    return jsonify({
        "success": True,
        "q": q,
        "offset": offset,
        "limit": limit,
        "total": len(characters_index),
        "results": results,
    })


# ---------------------------------------------------------------------------
# "New to Anime" -- Beginner Quiz
# ---------------------------------------------------------------------------

def _quiz_questions():
    """Six beginner-friendly questions. Asked in plain English so someone who
    has never watched anime can answer — we translate their taste in
    Hollywood/superhero/cartoon shows into anime genres under the hood."""
    return [
        {
            "key": "taste",
            "question": "What kinda shows have you watched before?",
            "hint": "No anime knowledge needed — just what you already like.",
            "options": [
                {"value": "hollywood", "emoji": "🍿", "label": "Hollywood movies & series", "weights": {"Action": 2, "Drama": 2, "Sci-Fi": 1}},
                {"value": "superhero", "emoji": "🦸", "label": "Superhero stuff", "weights": {"Action": 3, "Supernatural": 2}},
                {"value": "cartoons", "emoji": "🎨", "label": "Cartoons & animation", "weights": {"Adventure": 2, "Fantasy": 2, "Comedy": 1}},
                {"value": "sitcom", "emoji": "😄", "label": "Sitcoms & comedy shows", "weights": {"Comedy": 3, "Slice of Life": 1}},
                {"value": "drama", "emoji": "🎭", "label": "Soap operas & dramas", "weights": {"Drama": 3, "Romance": 2}},
                {"value": "sports", "emoji": "🏀", "label": "Sports shows", "weights": {"Sports": 3, "Action": 1}},
                {"value": "scifi", "emoji": "🚀", "label": "Sci-fi & fantasy", "weights": {"Sci-Fi": 3, "Fantasy": 2}},
                {"value": "any", "emoji": "🎲", "label": "A bit of everything", "weights": {}},
            ],
        },
        {
            "key": "mood",
            "question": "What's your mood right now?",
            "hint": "We'll match the show to how you're feeling.",
            "options": [
                {"value": "laugh", "emoji": "😂", "label": "Make me laugh", "weights": {"Comedy": 3, "Slice of Life": 1}},
                {"value": "pumped", "emoji": "⚡", "label": "Pump me up", "weights": {"Action": 3, "Sports": 1}},
                {"value": "feel", "emoji": "💔", "label": "Let me feel things", "weights": {"Drama": 3, "Romance": 1}},
                {"value": "chill", "emoji": "🧘", "label": "Keep it chill", "weights": {"Slice of Life": 3}},
                {"value": "mind", "emoji": "🧠", "label": "Blow my mind", "weights": {"Psychological": 3, "Thriller": 2, "Mystery": 1}},
                {"value": "spook", "emoji": "👻", "label": "Spook me", "weights": {"Horror": 3, "Supernatural": 1}},
            ],
        },
        {
            "key": "world",
            "question": "Which world sounds more fun to escape into?",
            "hint": "Pick the setting that gives you wanderlust.",
            "options": [
                {"value": "real", "emoji": "🏙️", "label": "Everyday life, like ours", "weights": {"Slice of Life": 3, "Comedy": 1, "Drama": 1}},
                {"value": "magic", "emoji": "🧙", "label": "Magic & monsters", "weights": {"Fantasy": 3, "Adventure": 2, "Supernatural": 1}},
                {"value": "future", "emoji": "🤖", "label": "Futuristic / robots", "weights": {"Sci-Fi": 3, "Mecha": 2}},
                {"value": "fight", "emoji": "🥋", "label": "Battles & tournaments", "weights": {"Action": 3, "Sports": 1}},
                {"value": "highschool", "emoji": "🏫", "label": "High school life", "weights": {"Romance": 2, "Comedy": 2, "Slice of Life": 2}},
                {"value": "mystery", "emoji": "🕵️", "label": "Mysteries & crimes", "weights": {"Mystery": 3, "Psychological": 2, "Thriller": 2}},
            ],
        },
        {
            "key": "length",
            "question": "How much time are you willing to commit?",
            "hint": "We'll only suggest shows that fit your schedule.",
            "options": [
                {"value": "short", "emoji": "🍜", "label": "Short & sweet (≤ 12 eps)", "weights": {}},
                {"value": "cour", "emoji": "📺", "label": "One season (13–26 eps)", "weights": {}},
                {"value": "long", "emoji": "🐉", "label": "Long haul (27–100 eps)", "weights": {}},
                {"value": "marathon", "emoji": "♾️", "label": "Marathon (100+ eps)", "weights": {}},
                {"value": "any", "emoji": "🤷", "label": "No preference", "weights": {}},
            ],
        },
        {
            "key": "love",
            "question": "How do you feel about love stories?",
            "hint": "Romance is a big part of anime — let us know.",
            "options": [
                {"value": "love", "emoji": "💘", "label": "I'm a hopeless romantic", "weights": {"Romance": 4, "Drama": 2}},
                {"value": "sometimes", "emoji": "💞", "label": "Only if it's not the whole plot", "weights": {"Romance": 1, "Comedy": 1}},
                {"value": "nope", "emoji": "🙅", "label": "Skip the romance", "weights": {"Romance": -4}},
                {"value": "fine", "emoji": "😌", "label": "Don't mind either way", "weights": {}},
            ],
        },
        {
            "key": "avoid",
            "question": "Anything you'd rather not see?",
            "hint": "We'll keep those shows out of your picks.",
            "options": [
                {"value": "no_horror", "emoji": "😱", "label": "No scary / gory stuff", "weights": {"Horror": -4, "Psychological": -2}},
                {"value": "no_heavy", "emoji": "🌤️", "label": "Nothing too depressing", "weights": {"Horror": -2, "Psychological": -2, "Thriller": -1, "Drama": -1}},
                {"value": "no_fan_service", "emoji": "🙈", "label": "Nothing too awkward", "weights": {"Ecchi": -4}},
                {"value": "nothing", "emoji": "🚀", "label": "I'll watch anything", "weights": {}},
            ],
        },
    ]


def _quiz_score_entry(entry, weights):
    genres = {g.strip() for g in (entry.get("genre") or "").split(" • ") if g.strip()}
    positive = {g: w for g, w in weights.items() if w > 0}

    total = 0
    if positive:
        matched = sum(w for g, w in positive.items() if g in genres)
        if matched <= 0:
            return None
        total += matched * 100
    else:
        total += 50

    for g, w in weights.items():
        if w < 0 and g in genres:
            total += w * 60

    try:
        rating = float(entry.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0.0
    popularity = entry.get("member_count", 0) or 0
    total += rating * 3 + min(popularity / 100000.0, 10)
    return total


def _quiz_length_filter(value):
    def pred(entry):
        try:
            eps = int(entry.get("total_episodes") or 0)
        except (TypeError, ValueError):
            return True
        if value == "short":
            return eps <= 12
        if value == "cour":
            return 13 <= eps <= 26
        if value == "long":
            return 27 <= eps <= 100
        if value == "marathon":
            return eps > 100
        return True

    return pred


def _run_quiz(answers):
    weights = {}
    for q in _quiz_questions():
        value = answers.get(q["key"], "")
        if not value:
            continue
        for opt in q["options"]:
            if opt["value"] == value:
                for genre, w in opt["weights"].items():
                    weights[genre] = weights.get(genre, 0) + w

    pool = []
    for slug, entry in anime_database.items():
        if not entry.get("image"):
            continue
        scored = _quiz_score_entry(entry, weights)
        if scored is None:
            continue
        pool.append((scored, slug))
    pool.sort(key=lambda x: x[0], reverse=True)

    length = answers.get("length", "")
    if length and length != "any":
        pred = _quiz_length_filter(length)
        filtered = [x for x in pool if pred(anime_database[x[1]])]
        if len(filtered) >= 3:
            pool = filtered

    top = [slug for _, slug in pool[:15]]
    random.shuffle(top)

    top_genres = [g for g, w in sorted(weights.items(), key=lambda kv: -kv[1]) if w > 0][:5]
    return top_genres, top[:5]


@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    user = g.get("user")
    if user is None:
        flash("Log in to find anime made for you.", "error")
        return redirect(url_for("auth.login", next=request.path))

    if request.method == "POST":
        answers = {
            q["key"]: (request.form.get(q["key"]) or "").strip()
            for q in _quiz_questions()
        }
        top_genres, slugs = _run_quiz(answers)
        save_quiz_result(user["id"], answers, top_genres, slugs)
        flash("Quiz saved — your anime picks are ready!", "success")
        return redirect(url_for("for_you"))

    return render_template(
        "quiz.html",
        questions=_quiz_questions(),
        genres=_genre_list(),
    )


@app.route("/for-you")
def for_you():
    user = g.get("user")
    if user is None:
        flash("Log in to see your New to Anime picks.", "error")
        return redirect(url_for("auth.login", next=request.path))

    quiz_result = get_latest_quiz_result(user["id"])
    picks = []
    if quiz_result:
        for slug in quiz_result["result_slugs"]:
            entry = anime_database.get(slug)
            if entry is None:
                continue
            picks.append({
                "slug": slug,
                "title": entry.get("title") or slug,
                "image": entry.get("image") or "",
                "rating": entry.get("rating") or "N/A",
                "year": entry.get("release") or "",
                "genre": entry.get("genre") or "",
                "total_episodes": entry.get("total_episodes", 0) or 0,
            })

    return render_template(
        "for_you.html",
        picks=picks,
        quiz_result=quiz_result,
        top_genres=(quiz_result or {}).get("top_genres") or [],
        genres=_genre_list(),
    )


if __name__ == "__main__":
    create_tables()

    threading.Thread(target=_schedule_loop, daemon=True).start()

    threading.Thread(target=_full_enrich_loop, daemon=True).start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        use_reloader=False,
    )