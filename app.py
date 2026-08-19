import calendar
import functools
import json
import os
import random
import re
import threading
import time
import traceback
from datetime import timedelta

import requests

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, g, url_for, flash, redirect

from anime_data import anime_database
from characters_data import search_characters, index_stats, reload_characters
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
    create_user_list,
    get_user_lists,
    get_user_list,
    rename_user_list,
    delete_user_list,
    add_to_user_list,
    remove_from_user_list,
    record_view,
    get_view_history,
    MAX_USER_LISTS,
)
from auth import auth, load_logged_in_user
from chat import chat_bp
from profile_routes import bp as profile_bp

from threads import init_threads


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

# Sessions last 10 years, so users stay logged in across devices/visits.
app.permanent_session_lifetime = timedelta(days=3650)

app.register_blueprint(auth)
app.register_blueprint(chat_bp)
app.register_blueprint(profile_bp)


init_threads(app)


# Make sure the profile/history/list tables exist even if the app is
# imported (not only when run as __main__). Idempotent.
create_tables()


@app.before_request
def _attach_user():
    load_logged_in_user()


@app.after_request
def _no_store_html(response):
    """Never let browsers cache HTML pages.

    Without this, browsers (especially Brave) serve stale copies of pages
    like /signup for hours, which caused confusing "old version" errors
    (outdated username messages, missing fixes). Static assets (css/js)
    are unaffected - only text/html is forced fresh.
    """
    if response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


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
    when the catalog data holds the lighter medium flavor. (The templates add
    an onerror fallback that swaps back to medium if the large flavor 404s.)"""
    if not isinstance(image, str) or not image:
        return ""
    if image.startswith(("http://", "https://")):
        return _hd_anilist_url(image)
    return url_for("static", filename="images/anime/" + image)


@app.template_filter("anime_img_large")
def anime_img_large(image):
    """Like anime_img — AniList cover URLs are served in the large (HD)
    flavor. Kept as a separate filter so the detail-page hero stays explicit
    about serving HD where it matters most."""
    return anime_img(image)


def _parse_db_time(ts):
    from datetime import datetime, timezone
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


@app.template_filter("time_ago")
def time_ago(ts):
    """'just now' / '3h ago' / '12d ago' / 'Jul 3, 2026'."""
    from datetime import datetime, timezone
    dt = _parse_db_time(ts)
    if dt is None:
        return ""
    delta = datetime.now(timezone.utc) - dt
    secs = max(int(delta.total_seconds()), 0)
    if secs < 60:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs}h ago"
    days = hrs // 24
    if days < 7:
        return f"{days}d ago"
    return dt.strftime("%b %d, %Y")


@app.template_filter("nice_date")
def nice_date(ts):
    """'Jul 3, 2026' — used for list 'Updated on' lines and member since."""
    dt = _parse_db_time(ts)
    if dt is None:
        return ts or ""
    return dt.strftime("%b %d, %Y")


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

_SCHEDULE_TTL = 600  # 10 minutes: keep live airing data fresh on the deployed site
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

class _LazyByAid(dict):
    """anime_database keyed by anilist_id, built lazily so importing the
    app never loads the catalog into RAM on low-memory hosts."""

    _built = False

    def _ensure(self):
        if not self._built:
            self.update({
                e["anilist_id"]: (slug, e)
                for slug, e in anime_database.items()
                if e.get("anilist_id")
            })
            self._built = True

    def get(self, key, default=None):
        self._ensure()
        return dict.get(self, key, default)

    def __getitem__(self, key):
        self._ensure()
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._ensure()
        return dict.__contains__(self, key)


_BY_AID = _LazyByAid()


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


_episode_state_import_warned = False


def _apply_episode_state(entry, st, nxt):
    """Mirror apply_airing's released/TBC logic on the in-memory catalog.

    Runs off the fresh AniList schedule data so the deployed site keeps
    every episode's released state and next-episode countdown accurate
    without loading a second copy of the catalog (safe on Render's 512MB
    free tier, where the heavy disk-based enrichment is disabled).
    """
    global _episode_state_import_warned
    try:
        from scripts.enrich_airing import _global_number, _is_placeholder
    except Exception as exc:
        if not _episode_state_import_warned:
            _episode_state_import_warned = True
            print(f"[schedule] episode-state helpers unavailable, "
                  f"skipping released/TBC updates: {exc}", flush=True)
        return

    if st == "Ongoing":
        aired = (nxt or {}).get("episode")
        if not aired:
            return
        aired -= 1
    elif st in ("Completed", "Cancelled"):
        aired = entry.get("total_episodes") or 0
        if not aired:
            return
    else:
        return

    seasons = entry.get("seasons") or []
    for si, s in enumerate(seasons):
        for ep in s.get("episodes") or []:
            gnum = _global_number(seasons, si, ep.get("number") or 0)
            if st == "Ongoing":
                if gnum > aired:
                    if ep.get("released") is not False:
                        ep["released"] = False
                    if not ep.get("title") or _is_placeholder(ep.get("title")):
                        ep["title"] = "TBC"
                else:
                    ep.pop("released", None)
                    if _is_placeholder(ep.get("title")):
                        ep.pop("title", None)
            elif gnum <= aired:
                ep.pop("released", None)
                if _is_placeholder(ep.get("title")):
                    ep.pop("title", None)


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

                _apply_episode_state(entry, st, nxt)
        except Exception as exc:
            print(f"[schedule] airing refresh failed for batch at offset {i}: {exc}",
                  flush=True)
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
    # Runs everywhere, including Render: refreshes airing status, next
    # episode countdowns and released/TBC flags purely in memory, so it is
    # safe on Render's 512MB free tier (no second catalog copy, no reload).
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

        try:
            from scripts.upgrade_posters_kitsu import main as kitsu_posters
            kitsu_posters(["--budget", "150"])
            kitsu_posters(["--apply"])
        except Exception as exc:
            print(f"[auto-enrich] kitsu poster slice failed (continuing): {exc}", flush=True)

        reload_database()
        reload_characters()

        _BY_AID.clear()
        _BY_AID.update({
            e["anilist_id"]: (slug, e)
            for slug, e in anime_database.items()
            if e.get("anilist_id")
        })
        _BY_AID._built = True
        print("[auto-enrich] Full enrichment completed successfully", flush=True)
    except Exception as exc:
        print(f"[auto-enrich] Error during enrichment: {exc}", flush=True)
        traceback.print_exc()
    finally:
        with _enrich_lock:
            _enrich_state["last"] = time.time()
            _enrich_state["running"] = False


def _full_enrich_loop():
    # The heavy enrichment loads a second copy of the ~60MB catalog into
    # RAM while serving requests, which would OOM-kill Render's 512MB free
    # instances (and its disk writes are ephemeral anyway). Live airing
    # updates on Render are handled by _schedule_loop above instead.
    if os.environ.get("RENDER"):
        return
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
    user = g.get("user")
    if user is not None:
        record_view(user["id"], anime_slug)
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


@app.route("/healthz")
def healthz():
    """Lightweight health check that does not load the anime catalog."""
    return "ok"


@app.route("/characters")
def characters():
    initial = search_characters("", 0, 60)
    total, covered, with_va = index_stats()

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
        "total": index_stats()[0],
        "results": results,
    })


# ---------------------------------------------------------------------------
# "New to Anime" -- Beginner Quiz (branching modal flow)
# ---------------------------------------------------------------------------
#
# The quiz is a six-step branching flow:
#   1. taste   — broad, Hollywood-friendly pick (single) — no anime jargon
#   2. mood    — how the user feels right now (single)
#   3. branch Q1 — adapts to the taste pick
#   4. mood Q1   — adapts to the mood pick
#   5. branch Q2 — second question for that taste
#   6. mood Q2   — second question for that mood
#
# Steps 3-6 are multi-select (max 3) and each option can declare `conflicts`
# so directly opposite choices can never be selected together.

_TASTE_Q = {
    "question": "What kinda shows do you usually binge?",
    "hint": "No anime knowledge needed — just what you already watch. Pick one.",
    "multi": False,
    "max": 1,
    "options": [
        {"value": "action", "emoji": "🎬", "label": "Action & blockbusters", "weights": {"Action": 3, "Adventure": 2}},
        {"value": "superhero", "emoji": "🦸", "label": "Superheroes & superpowers", "weights": {"Action": 2, "Supernatural": 3}},
        {"value": "comedy", "emoji": "😂", "label": "Comedy & sitcoms", "weights": {"Comedy": 3, "Slice of Life": 1}},
        {"value": "drama", "emoji": "🎭", "label": "Drama & romance", "weights": {"Drama": 3, "Romance": 2}},
        {"value": "scifi", "emoji": "🚀", "label": "Sci-fi & fantasy", "weights": {"Sci-Fi": 3, "Fantasy": 2}},
        {"value": "horror", "emoji": "👻", "label": "Horror & thrillers", "weights": {"Horror": 3, "Psychological": 2, "Thriller": 1}},
        {"value": "sports", "emoji": "🏀", "label": "Sports & competition", "weights": {"Sports": 3, "Action": 1}},
        {"value": "animated", "emoji": "🎨", "label": "Cartoons & animation", "weights": {"Adventure": 2, "Fantasy": 2, "Comedy": 1}},
        {"value": "everything", "emoji": "🎲", "label": "A bit of everything", "weights": {}},
    ],
}

_MOOD_Q = {
    "question": "What's your mood right now?",
    "hint": "We'll match the show to how you're feeling. Pick one.",
    "multi": False,
    "max": 1,
    "options": [
        {"value": "laugh", "emoji": "😂", "label": "Make me laugh", "weights": {"Comedy": 3, "Slice of Life": 1}},
        {"value": "pumped", "emoji": "⚡", "label": "Pump me up", "weights": {"Action": 3, "Sports": 1}},
        {"value": "feel", "emoji": "💔", "label": "Let me feel things", "weights": {"Drama": 3, "Romance": 1}},
        {"value": "chill", "emoji": "🧘", "label": "Keep it chill", "weights": {"Slice of Life": 3}},
        {"value": "mind", "emoji": "🧠", "label": "Blow my mind", "weights": {"Psychological": 3, "Thriller": 2, "Mystery": 1}},
        {"value": "spook", "emoji": "👻", "label": "Spook me", "weights": {"Horror": 3, "Supernatural": 1}},
    ],
}

_BRANCH_QUESTIONS = {
    "action": {
        "q1": {
            "question": "What kind of action gets your blood pumping?",
            "hint": "Pick up to 3 — the ones that make you lean forward.",
            "multi": True, "max": 3,
            "options": [
                {"value": "act_battles", "emoji": "⚔️", "label": "Epic battles & showdowns", "weights": {"Action": 3}, "conflicts": ["act_slow"]},
                {"value": "act_chase", "emoji": "🏎️", "label": "Car chases & high-speed hunts", "weights": {"Action": 2, "Thriller": 1}, "conflicts": ["act_slow"]},
                {"value": "act_martial", "emoji": "🥋", "label": "Martial arts & fist fights", "weights": {"Action": 2, "Sports": 1}, "conflicts": ["act_slow"]},
                {"value": "act_war", "emoji": "🪖", "label": "Wars & armies", "weights": {"Action": 2, "Drama": 1}},
                {"value": "act_heist", "emoji": "💼", "label": "Heists & secret missions", "weights": {"Action": 2, "Thriller": 1}},
                {"value": "act_slow", "emoji": "🧊", "label": "Slow-burn tension", "weights": {"Psychological": 2, "Thriller": 2}, "conflicts": ["act_battles", "act_chase", "act_martial"]},
            ],
        },
        "q2": {
            "question": "Where should the adventure happen?",
            "hint": "Pick up to 3 worlds you'd happily escape into.",
            "multi": True, "max": 3,
            "options": [
                {"value": "set_city", "emoji": "🌆", "label": "Modern city streets", "weights": {"Action": 1, "Slice of Life": 1}},
                {"value": "set_fantasy", "emoji": "🏰", "label": "Fantasy worlds & kingdoms", "weights": {"Fantasy": 2, "Adventure": 1}},
                {"value": "set_space", "emoji": "🚀", "label": "Space & other planets", "weights": {"Sci-Fi": 2, "Adventure": 1}},
                {"value": "set_school", "emoji": "🏫", "label": "School & campus", "weights": {"Slice of Life": 1, "Romance": 1}},
                {"value": "set_hist", "emoji": "⏳", "label": "Historical eras", "weights": {"Drama": 2}, "conflicts": ["set_future"]},
                {"value": "set_future", "emoji": "🤖", "label": "Dystopian futures", "weights": {"Sci-Fi": 2, "Psychological": 1}, "conflicts": ["set_hist"]},
            ],
        },
    },
    "superhero": {
        "q1": {
            "question": "What kind of hero do you love watching?",
            "hint": "Pick up to 3 — the hero archetypes you can't get enough of.",
            "multi": True, "max": 3,
            "options": [
                {"value": "hero_lone", "emoji": "🕶️", "label": "Lone vigilantes", "weights": {"Action": 2, "Psychological": 1}, "conflicts": ["hero_team"]},
                {"value": "hero_team", "emoji": "🦸‍♀️", "label": "Super teams", "weights": {"Action": 2, "Adventure": 1}, "conflicts": ["hero_lone"]},
                {"value": "hero_anti", "emoji": "😈", "label": "Anti-heroes & morally grey", "weights": {"Psychological": 2, "Drama": 1}},
                {"value": "hero_power", "emoji": "⚡", "label": "Overpowered power fantasy", "weights": {"Action": 2, "Supernatural": 1}},
                {"value": "hero_origin", "emoji": "📖", "label": "Origin stories & growth", "weights": {"Drama": 2, "Supernatural": 1}},
            ],
        },
        "q2": {
            "question": "What powers excite you most?",
            "hint": "Pick up to 3 superpowers you'd kill to have.",
            "multi": True, "max": 3,
            "options": [
                {"value": "pow_fight", "emoji": "🥊", "label": "Hand-to-hand combat", "weights": {"Action": 2, "Sports": 1}, "conflicts": ["pow_energy"]},
                {"value": "pow_energy", "emoji": "⚡", "label": "Energy blasts & powers", "weights": {"Supernatural": 3}, "conflicts": ["pow_fight"]},
                {"value": "pow_speed", "emoji": "💨", "label": "Speed & agility", "weights": {"Action": 2}},
                {"value": "pow_mind", "emoji": "🧠", "label": "Mind powers & telepathy", "weights": {"Psychological": 2, "Supernatural": 1}},
                {"value": "pow_transform", "emoji": "🦖", "label": "Shapeshifting & transformations", "weights": {"Supernatural": 2, "Action": 1}},
            ],
        },
    },
    "comedy": {
        "q1": {
            "question": "What style of comedy is your thing?",
            "hint": "Pick up to 3 flavors that make you wheeze.",
            "multi": True, "max": 3,
            "options": [
                {"value": "com_banter", "emoji": "🗣️", "label": "Witty banter & roasting", "weights": {"Comedy": 3}, "conflicts": ["com_absurd"]},
                {"value": "com_awkward", "emoji": "😅", "label": "Awkward situations", "weights": {"Comedy": 2, "Slice of Life": 1}},
                {"value": "com_absurd", "emoji": "🤪", "label": "Absurd & random humor", "weights": {"Comedy": 2, "Fantasy": 1}, "conflicts": ["com_banter"]},
                {"value": "com_parody", "emoji": "🎭", "label": "Parody & satire", "weights": {"Comedy": 2, "Psychological": 1}},
                {"value": "com_slapstick", "emoji": "🤸", "label": "Slapstick & physical comedy", "weights": {"Comedy": 2}},
            ],
        },
        "q2": {
            "question": "Who's the funniest type of character?",
            "hint": "Pick up to 3 comedic archetypes you love.",
            "multi": True, "max": 3,
            "options": [
                {"value": "who_deadpan", "emoji": "😐", "label": "Deadpan straight-man", "weights": {"Comedy": 3}, "conflicts": ["who_over"]},
                {"value": "who_over", "emoji": "🎢", "label": "Loud over-the-top goofball", "weights": {"Comedy": 2, "Slice of Life": 1}, "conflicts": ["who_deadpan"]},
                {"value": "who_group", "emoji": "👥", "label": "A chaotic friend group", "weights": {"Comedy": 2, "Slice of Life": 1}},
                {"value": "who_genius", "emoji": "🧠", "label": "Genius who's terrible at life", "weights": {"Comedy": 2, "Psychological": 1}},
            ],
        },
    },
    "drama": {
        "q1": {
            "question": "What kind of story pulls at your heart?",
            "hint": "Pick up to 3 storylines that hook you.",
            "multi": True, "max": 3,
            "options": [
                {"value": "drm_romance", "emoji": "💞", "label": "Slow-burn romance", "weights": {"Romance": 3}, "conflicts": ["drm_tragedy"]},
                {"value": "drm_tragedy", "emoji": "💔", "label": "Tragic & bittersweet", "weights": {"Drama": 3}, "conflicts": ["drm_romance"]},
                {"value": "drm_growth", "emoji": "🌱", "label": "Coming-of-age growth", "weights": {"Slice of Life": 2, "Drama": 1}},
                {"value": "drm_family", "emoji": "🏡", "label": "Family & friendship bonds", "weights": {"Drama": 2, "Slice of Life": 1}},
                {"value": "drm_twist", "emoji": "🔄", "label": "Plot twists & secrets", "weights": {"Mystery": 2, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "How do you like the emotional payoff?",
            "hint": "Pick up to 3 — how the story should land.",
            "multi": True, "max": 3,
            "options": [
                {"value": "pay_cry", "emoji": "😭", "label": "Make me cry", "weights": {"Drama": 3}, "conflicts": ["pay_happy"]},
                {"value": "pay_happy", "emoji": "😊", "label": "Warm happy endings", "weights": {"Romance": 2, "Slice of Life": 1}, "conflicts": ["pay_cry"]},
                {"value": "pay_hope", "emoji": "🌅", "label": "Hope through hardship", "weights": {"Drama": 2}},
                {"value": "pay_bittersweet", "emoji": "🌗", "label": "Bittersweet & realistic", "weights": {"Drama": 2, "Psychological": 1}},
            ],
        },
    },
    "scifi": {
        "q1": {
            "question": "What kind of sci-fi / fantasy world?",
            "hint": "Pick up to 3 universes you'd move into.",
            "multi": True, "max": 3,
            "options": [
                {"value": "sf_space", "emoji": "🚀", "label": "Space exploration", "weights": {"Sci-Fi": 3}, "conflicts": ["sf_magic"]},
                {"value": "sf_magic", "emoji": "🧙", "label": "Magic & monsters", "weights": {"Fantasy": 3}, "conflicts": ["sf_space"]},
                {"value": "sf_cyber", "emoji": "🤖", "label": "Cyberpunk & high tech", "weights": {"Sci-Fi": 2, "Psychological": 1}},
                {"value": "sf_dystopia", "emoji": "🌆", "label": "Dystopian societies", "weights": {"Psychological": 2, "Sci-Fi": 1}},
                {"value": "sf_portal", "emoji": "🌀", "label": "Portal to another world", "weights": {"Fantasy": 2, "Adventure": 1}},
            ],
        },
        "q2": {
            "question": "What's the hook for you?",
            "hint": "Pick up to 3 things that keep you glued.",
            "multi": True, "max": 3,
            "options": [
                {"value": "hook_mystery", "emoji": "🕵️", "label": "Mysteries of the universe", "weights": {"Mystery": 2, "Sci-Fi": 1}},
                {"value": "hook_action", "emoji": "💥", "label": "Epic battles", "weights": {"Action": 2, "Fantasy": 1}},
                {"value": "hook_world", "emoji": "🌍", "label": "World-building & lore", "weights": {"Fantasy": 2, "Adventure": 1}},
                {"value": "hook_philo", "emoji": "🧠", "label": "Philosophical questions", "weights": {"Psychological": 3}},
                {"value": "hook_survival", "emoji": "🏕️", "label": "Survival & resourcefulness", "weights": {"Adventure": 2, "Drama": 1}},
            ],
        },
    },
    "horror": {
        "q1": {
            "question": "What scares you in the best way?",
            "hint": "Pick up to 3 kinds of creepy you enjoy.",
            "multi": True, "max": 3,
            "options": [
                {"value": "hrr_jump", "emoji": "😱", "label": "Jump scares & monsters", "weights": {"Horror": 3}, "conflicts": ["hrr_psych", "hrr_mild"]},
                {"value": "hrr_psych", "emoji": "🌀", "label": "Psychological dread", "weights": {"Psychological": 3, "Horror": 1}, "conflicts": ["hrr_jump"]},
                {"value": "hrr_gore", "emoji": "🩸", "label": "Gore & dark fantasy", "weights": {"Horror": 2, "Action": 1}, "conflicts": ["hrr_mild"]},
                {"value": "hrr_super", "emoji": "🧟", "label": "Supernatural & ghosts", "weights": {"Supernatural": 2, "Horror": 1}},
                {"value": "hrr_mild", "emoji": "🌙", "label": "Spooky but not traumatizing", "weights": {"Supernatural": 1, "Mystery": 1}, "conflicts": ["hrr_jump", "hrr_gore"]},
            ],
        },
        "q2": {
            "question": "How dark can it go?",
            "hint": "Pick up to 3 — set the darkness dial.",
            "multi": True, "max": 3,
            "options": [
                {"value": "dk_full", "emoji": "🌑", "label": "Full dark, no light", "weights": {"Horror": 2, "Psychological": 2}, "conflicts": ["dk_light", "dk_hopeful"]},
                {"value": "dk_light", "emoji": "🌅", "label": "Light at the end", "weights": {"Drama": 1, "Mystery": 1}, "conflicts": ["dk_full"]},
                {"value": "dk_hopeful", "emoji": "🌈", "label": "Hopeful endings", "weights": {"Drama": 1, "Supernatural": 1}, "conflicts": ["dk_full"]},
                {"value": "dk_thrill", "emoji": "🎢", "label": "Thrills without gore", "weights": {"Thriller": 2, "Mystery": 1}},
            ],
        },
    },
    "sports": {
        "q1": {
            "question": "What kind of competition?",
            "hint": "Pick up to 3 sports you love to watch.",
            "multi": True, "max": 3,
            "options": [
                {"value": "sp_team", "emoji": "⚽", "label": "Team sports", "weights": {"Sports": 3}, "conflicts": ["sp_individual"]},
                {"value": "sp_individual", "emoji": "🥊", "label": "Individual duels", "weights": {"Sports": 2, "Action": 1}, "conflicts": ["sp_team"]},
                {"value": "sp_racing", "emoji": "🏎️", "label": "Racing & speed", "weights": {"Sports": 2, "Action": 1}},
                {"value": "sp_game", "emoji": "♟️", "label": "Mind games & strategy", "weights": {"Sports": 1, "Psychological": 2}},
                {"value": "sp_rival", "emoji": "🤝", "label": "Rivalries & underdogs", "weights": {"Sports": 2, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "What gets you invested?",
            "hint": "Pick up to 3 reasons you stay for the season.",
            "multi": True, "max": 3,
            "options": [
                {"value": "sp_win", "emoji": "🏆", "label": "The road to the top", "weights": {"Sports": 3}},
                {"value": "sp_friendship", "emoji": "👯", "label": "Team bonds & friendships", "weights": {"Sports": 2, "Slice of Life": 1}},
                {"value": "sp_underdog", "emoji": "🐣", "label": "Underdogs beating giants", "weights": {"Sports": 2, "Drama": 1}},
                {"value": "sp_flow", "emoji": "🎯", "label": "Peak performance moments", "weights": {"Sports": 2, "Action": 1}},
            ],
        },
    },
    "animated": {
        "q1": {
            "question": "What kind of animated story?",
            "hint": "Pick up to 3 — cartoons come in every flavor.",
            "multi": True, "max": 3,
            "options": [
                {"value": "an_kids", "emoji": "🧸", "label": "Family-friendly adventures", "weights": {"Adventure": 2, "Fantasy": 1}},
                {"value": "an_epic", "emoji": "⚔️", "label": "Epic fantasy adventures", "weights": {"Adventure": 2, "Fantasy": 2}},
                {"value": "an_funny", "emoji": "🤡", "label": "Silly & goofy", "weights": {"Comedy": 2}},
                {"value": "an_movie", "emoji": "🎬", "label": "Movie-like polish & drama", "weights": {"Drama": 2, "Adventure": 1}},
                {"value": "an_art", "emoji": "🎨", "label": "Beautiful art & atmosphere", "weights": {"Slice of Life": 1, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "What matters most in a cartoon?",
            "hint": "Pick up to 3 — the most important part for you.",
            "multi": True, "max": 3,
            "options": [
                {"value": "imp_story", "emoji": "📖", "label": "A gripping story", "weights": {"Drama": 2, "Mystery": 1}},
                {"value": "imp_char", "emoji": "😊", "label": "Characters I love", "weights": {"Slice of Life": 2, "Comedy": 1}},
                {"value": "imp_world", "emoji": "🌍", "label": "Amazing worlds", "weights": {"Fantasy": 2, "Adventure": 1}},
                {"value": "imp_fun", "emoji": "🎉", "label": "Pure fun & laughs", "weights": {"Comedy": 2, "Slice of Life": 1}},
            ],
        },
    },
    "everything": {
        "q1": {
            "question": "What pulls you into a show first?",
            "hint": "Pick up to 3 — your show-starter instincts.",
            "multi": True, "max": 3,
            "options": [
                {"value": "ev_story", "emoji": "📖", "label": "A killer story", "weights": {"Drama": 1, "Mystery": 1}},
                {"value": "ev_char", "emoji": "💬", "label": "Characters I love", "weights": {"Slice of Life": 1, "Comedy": 1}},
                {"value": "ev_world", "emoji": "🌍", "label": "Amazing worlds", "weights": {"Fantasy": 1, "Adventure": 1}},
                {"value": "ev_action", "emoji": "💥", "label": "Action & spectacle", "weights": {"Action": 1}},
                {"value": "ev_vibes", "emoji": "🌙", "label": "Mood & atmosphere", "weights": {"Psychological": 1, "Supernatural": 1}},
            ],
        },
        "q2": {
            "question": "Pick any three you'd want in one show:",
            "hint": "Mix and match up to 3.",
            "multi": True, "max": 3,
            "options": [
                {"value": "mix_laugh", "emoji": "😂", "label": "Laughs", "weights": {"Comedy": 1}},
                {"value": "mix_feels", "emoji": "💔", "label": "Feels", "weights": {"Drama": 1, "Romance": 1}},
                {"value": "mix_action", "emoji": "⚡", "label": "Action", "weights": {"Action": 1}},
                {"value": "mix_mind", "emoji": "🧠", "label": "Mind-benders", "weights": {"Psychological": 1}},
                {"value": "mix_chill", "emoji": "🧘", "label": "Cozy comfort", "weights": {"Slice of Life": 1}},
            ],
        },
    },
}

_MOOD_QUESTIONS = {
    "laugh": {
        "q1": {
            "question": "What gets you giggling?",
            "hint": "Pick up to 3 — your comedy triggers.",
            "multi": True, "max": 3,
            "options": [
                {"value": "lg_roast", "emoji": "🗣️", "label": "Roasting & banter", "weights": {"Comedy": 3}},
                {"value": "lg_awkward", "emoji": "😅", "label": "Cringe & awkward", "weights": {"Comedy": 2, "Slice of Life": 1}},
                {"value": "lg_random", "emoji": "🤪", "label": "Random nonsense", "weights": {"Comedy": 2}, "conflicts": ["lg_dry"]},
                {"value": "lg_satire", "emoji": "🎭", "label": "Satire & parody", "weights": {"Comedy": 2, "Psychological": 1}},
                {"value": "lg_dry", "emoji": "😑", "label": "Dry & deadpan", "weights": {"Comedy": 2}, "conflicts": ["lg_random"]},
            ],
        },
        "q2": {
            "question": "How do you like your comedy served?",
            "hint": "Pick up to 3 — the seasoning matters.",
            "multi": True, "max": 3,
            "options": [
                {"value": "ls_light", "emoji": "🍃", "label": "Light & wholesome", "weights": {"Slice of Life": 2, "Comedy": 1}, "conflicts": ["ls_dark"]},
                {"value": "ls_dark", "emoji": "🌑", "label": "Dark humor", "weights": {"Comedy": 2, "Psychological": 1}, "conflicts": ["ls_light"]},
                {"value": "ls_chaos", "emoji": "🔥", "label": "Chaos & energy", "weights": {"Comedy": 2}},
                {"value": "ls_heart", "emoji": "💗", "label": "Comedy with heart", "weights": {"Comedy": 1, "Drama": 1, "Slice of Life": 1}},
            ],
        },
    },
    "pumped": {
        "q1": {
            "question": "What hypes you up most?",
            "hint": "Pick up to 3 — the adrenaline triggers.",
            "multi": True, "max": 3,
            "options": [
                {"value": "hp_battle", "emoji": "⚔️", "label": "Big fights", "weights": {"Action": 3}},
                {"value": "hp_epic", "emoji": "🎵", "label": "Epic music & moments", "weights": {"Action": 2, "Drama": 1}},
                {"value": "hp_underdog", "emoji": "🐺", "label": "Underdog comebacks", "weights": {"Sports": 2, "Drama": 1}},
                {"value": "hp_training", "emoji": "🏋️", "label": "Training & getting stronger", "weights": {"Sports": 2, "Action": 1}},
                {"value": "hp_team", "emoji": "🤝", "label": "Team rallies", "weights": {"Sports": 2, "Slice of Life": 1}},
            ],
        },
        "q2": {
            "question": "What keeps the hype going?",
            "hint": "Pick up to 3 — don't let the hype die.",
            "multi": True, "max": 3,
            "options": [
                {"value": "hk_stakes", "emoji": "🎯", "label": "High stakes", "weights": {"Thriller": 2, "Action": 1}},
                {"value": "hk_pacing", "emoji": "⏩", "label": "Non-stop pacing", "weights": {"Action": 2}},
                {"value": "hk_rival", "emoji": "👊", "label": "Rivalries", "weights": {"Action": 1, "Sports": 1, "Drama": 1}},
                {"value": "hk_payoff", "emoji": "💥", "label": "Huge payoffs", "weights": {"Action": 1, "Drama": 1}},
            ],
        },
    },
    "feel": {
        "q1": {
            "question": "What kind of feels are you after?",
            "hint": "Pick up to 3 — the feelings you're chasing.",
            "multi": True, "max": 3,
            "options": [
                {"value": "fl_romance", "emoji": "💞", "label": "Romance", "weights": {"Romance": 3}},
                {"value": "fl_sad", "emoji": "💧", "label": "Sad & heavy", "weights": {"Drama": 3}},
                {"value": "fl_warm", "emoji": "☀️", "label": "Warm & wholesome", "weights": {"Slice of Life": 2}},
                {"value": "fl_growth", "emoji": "🌱", "label": "Growth & healing", "weights": {"Drama": 2, "Slice of Life": 1}},
                {"value": "fl_nostalgia", "emoji": "📼", "label": "Nostalgia", "weights": {"Slice of Life": 1, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "How intense can the feels get?",
            "hint": "Pick up to 3 — set the feels dial.",
            "multi": True, "max": 3,
            "options": [
                {"value": "fi_gut", "emoji": "💔", "label": "Gut-punching", "weights": {"Drama": 3}, "conflicts": ["fi_gentle"]},
                {"value": "fi_gentle", "emoji": "🕊️", "label": "Gentle & soft", "weights": {"Slice of Life": 2}, "conflicts": ["fi_gut"]},
                {"value": "fi_hopeful", "emoji": "🌅", "label": "Hopeful", "weights": {"Drama": 1, "Slice of Life": 1}},
                {"value": "fi_epic", "emoji": "🌊", "label": "Epic emotional arcs", "weights": {"Drama": 2}},
            ],
        },
    },
    "chill": {
        "q1": {
            "question": "What does 'chill' mean to you?",
            "hint": "Pick up to 3 — your cozy buttons.",
            "multi": True, "max": 3,
            "options": [
                {"value": "ch_nature", "emoji": "🌄", "label": "Nature & scenery", "weights": {"Slice of Life": 2}},
                {"value": "ch_food", "emoji": "🍜", "label": "Food & cooking", "weights": {"Slice of Life": 2}},
                {"value": "ch_slice", "emoji": "🏠", "label": "Everyday life", "weights": {"Slice of Life": 3}},
                {"value": "ch_cute", "emoji": "🐱", "label": "Cute & comfy", "weights": {"Slice of Life": 2, "Comedy": 1}},
                {"value": "ch_music", "emoji": "🎶", "label": "Music & vibes", "weights": {"Slice of Life": 1, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "What's the perfect chill episode?",
            "hint": "Pick up to 3 — your ideal wind-down.",
            "multi": True, "max": 3,
            "options": [
                {"value": "cj_noise", "emoji": "🤫", "label": "Quiet & low-stakes", "weights": {"Slice of Life": 2}, "conflicts": ["cj_drama"]},
                {"value": "cj_drama", "emoji": "🍿", "label": "Light drama", "weights": {"Drama": 1, "Slice of Life": 1}, "conflicts": ["cj_noise"]},
                {"value": "cj_friends", "emoji": "👫", "label": "Hanging with friends", "weights": {"Slice of Life": 2, "Comedy": 1}},
                {"value": "cj_adventure", "emoji": "🚶", "label": "Gentle adventures", "weights": {"Adventure": 1, "Slice of Life": 1}},
            ],
        },
    },
    "mind": {
        "q1": {
            "question": "What kind of mind-bender?",
            "hint": "Pick up to 3 — the twists that break you.",
            "multi": True, "max": 3,
            "options": [
                {"value": "mb_twist", "emoji": "🔀", "label": "Plot twists", "weights": {"Mystery": 2, "Thriller": 1}},
                {"value": "mb_philo", "emoji": "🧠", "label": "Philosophy & ideas", "weights": {"Psychological": 3}},
                {"value": "mb_mystery", "emoji": "🕵️", "label": "Unsolved mysteries", "weights": {"Mystery": 3}},
                {"value": "mb_games", "emoji": "♟️", "label": "Games & puzzles", "weights": {"Psychological": 2, "Mystery": 1}},
                {"value": "mb_psych", "emoji": "🌀", "label": "Character psychology", "weights": {"Psychological": 2, "Drama": 1}},
            ],
        },
        "q2": {
            "question": "How much should it hurt?",
            "hint": "Pick up to 3 — the pain tolerance check.",
            "multi": True, "max": 3,
            "options": [
                {"value": "mh_full", "emoji": "🔥", "label": "Full mind-fry", "weights": {"Psychological": 3}, "conflicts": ["mh_gentle"]},
                {"value": "mh_gentle", "emoji": "🌤️", "label": "Gentle introspective", "weights": {"Slice of Life": 1, "Drama": 1}, "conflicts": ["mh_full"]},
                {"value": "mh_thrill", "emoji": "🎢", "label": "Thrilling ride", "weights": {"Thriller": 2}},
                {"value": "mh_reward", "emoji": "💡", "label": "Aha! moments", "weights": {"Mystery": 2}},
            ],
        },
    },
    "spook": {
        "q1": {
            "question": "What kind of spooky?",
            "hint": "Pick up to 3 — your fear flavor.",
            "multi": True, "max": 3,
            "options": [
                {"value": "sp_ghosts", "emoji": "👻", "label": "Ghosts & spirits", "weights": {"Supernatural": 3}},
                {"value": "sp_psych", "emoji": "🌀", "label": "Psychological horror", "weights": {"Psychological": 2, "Horror": 1}},
                {"value": "sp_monsters", "emoji": "👹", "label": "Monsters & creatures", "weights": {"Horror": 2, "Supernatural": 1}},
                {"value": "sp_mystery", "emoji": "🔮", "label": "Creepy mysteries", "weights": {"Mystery": 2, "Supernatural": 1}},
                {"value": "sp_gore", "emoji": "🩸", "label": "Gore & body horror", "weights": {"Horror": 2}, "conflicts": ["sp_light"]},
                {"value": "sp_light", "emoji": "🌙", "label": "Spooky but light", "weights": {"Supernatural": 1, "Comedy": 1}, "conflicts": ["sp_gore"]},
            ],
        },
        "q2": {
            "question": "How do you want to feel after?",
            "hint": "Pick up to 3 — the aftermath you're after.",
            "multi": True, "max": 3,
            "options": [
                {"value": "sa_shook", "emoji": "😨", "label": "Shook", "weights": {"Horror": 2, "Psychological": 2}},
                {"value": "sa_relief", "emoji": "😮‍💨", "label": "Relieved it ended", "weights": {"Thriller": 1, "Mystery": 1}},
                {"value": "sa_curious", "emoji": "🤔", "label": "Wanting answers", "weights": {"Mystery": 2}},
                {"value": "sa_thrill", "emoji": "🎢", "label": "Thrilled, not traumatized", "weights": {"Thriller": 2, "Supernatural": 1}},
            ],
        },
    },
}


_OPTION_WEIGHTS = {}
for _q in [_TASTE_Q, _MOOD_Q] + [
    q for b in _BRANCH_QUESTIONS.values() for q in (b["q1"], b["q2"])
] + [q for m in _MOOD_QUESTIONS.values() for q in (m["q1"], m["q2"])]:
    for _o in _q["options"]:
        _OPTION_WEIGHTS[_o["value"]] = _o.get("weights", {})


_FRANCHISE_RE = re.compile(
    r"-(?:2nd|3rd|4th|5th|s\d+|season|part|ova|movie|film|special|tv|remake|rebirth|the-movie|the-movie-.*|\d+).*$"
)


def _pub_question(q):
    """Strip scoring weights so the client only gets display data."""
    return {
        "question": q["question"],
        "hint": q.get("hint", ""),
        "multi": q.get("multi", False),
        "max": q.get("max", 1),
        "options": [
            {"value": o["value"], "emoji": o.get("emoji", ""),
             "label": o["label"], "conflicts": o.get("conflicts", [])}
            for o in q["options"]
        ],
    }


def _quiz_flow_json():
    return {
        "taste": _pub_question(_TASTE_Q),
        "mood": _pub_question(_MOOD_Q),
        "branches": {
            k: {"q1": _pub_question(v["q1"]), "q2": _pub_question(v["q2"])}
            for k, v in _BRANCH_QUESTIONS.items()
        },
        "moods": {
            k: {"q1": _pub_question(v["q1"]), "q2": _pub_question(v["q2"])}
            for k, v in _MOOD_QUESTIONS.items()
        },
    }


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


def _diverse_top(pool, n):
    """Top-n picks with light franchise dedupe so a single show's seasons
    don't hog every recommendation slot."""
    picked = []
    seen = set()
    for score, slug in pool:
        if len(picked) >= n:
            break
        base = _FRANCHISE_RE.sub("", slug)
        if base in seen:
            continue
        seen.add(base)
        picked.append(slug)
    return picked


def _run_quiz(answers):
    weights = {}
    for key, values in answers.items():
        if isinstance(values, str):
            values = [values]
        for value in values or []:
            for genre, w in (_OPTION_WEIGHTS.get(value) or {}).items():
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

    top_genres = [g for g, w in sorted(weights.items(), key=lambda kv: -kv[1]) if w > 0][:5]
    return top_genres, _diverse_top(pool, 4)


def _pick_card(slug):
    """Build a pick dict shaped for the homepage _anime_card.html partial."""
    entry = anime_database.get(slug)
    if entry is None:
        return None
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
        "badge_label": "Your Match",
    }


@app.route("/quiz", methods=["GET", "POST"])
def quiz():
    user = g.get("user")
    if user is None:
        flash("Log in to find anime made for you.", "error")
        return redirect(url_for("auth.login", next=request.path))

    picks = []
    top_genres = []
    show_results = False

    if request.method == "POST":
        steps = ["q1", "q2", "q3", "q4", "q5", "q6"]
        answers = {}
        for step in steps:
            # Support both repeated fields and comma-joined values from the client
            values = []
            for v in request.form.getlist(step):
                values.extend(x.strip() for x in v.split(",") if x.strip())
            answers[step] = values[0] if len(values) == 1 else values
        if all(answers.get(s) for s in steps):
            top_genres, slugs = _run_quiz(answers)
            save_quiz_result(user["id"], answers, top_genres, slugs)
            picks = [p for slug in slugs if (p := _pick_card(slug)) is not None]
            show_results = True
        else:
            flash("Please answer every question before getting your picks.", "error")

    return render_template(
        "quiz.html",
        quiz_json=_quiz_flow_json(),
        picks=picks,
        top_genres=top_genres,
        show_results=show_results,
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