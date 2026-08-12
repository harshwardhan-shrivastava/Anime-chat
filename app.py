import calendar
import functools
import os
import re
import threading
import time

import requests

from dotenv import load_dotenv
load_dotenv()

from flask import Flask, render_template, request, jsonify, g, url_for, flash, redirect
from anime_data import anime_database
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
)

from auth import auth, load_logged_in_user
from chat import chat_bp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

app.register_blueprint(auth)
app.register_blueprint(chat_bp)


# ---------------------------------------------------------------------------
#  FIND YOUR MOOD — live AniList edition (new page: /find-mood)
#  Queries the AniList GraphQL API at request time for real, non-hardcoded
#  recommendations. Falls back to scoring your own catalog if AniList is
#  unreachable. Local catalog images are resolved through _resolve_image().
# ---------------------------------------------------------------------------

import json as _json
import re as _re
from datetime import date as _date
from urllib.error import HTTPError as _HTTPError
from urllib.error import URLError as _URLError
from urllib.request import Request as _UrlRequest
from urllib.request import urlopen as _urlopen

ANILIST_API_URL = "https://graphql.anilist.co"
ANILIST_TIMEOUT = 9  # seconds

# Mood -> AniList genre(s) to query live. Each genre gets its own query
# (AniList genre_in is AND semantics) and results are merged + deduped.
MOOD_GENRES = {
    "happy": ["Comedy", "Music"],
    "sad": ["Drama"],
    "action": ["Action"],
    "romance": ["Romance"],
    "horror": ["Horror"],
    "fantasy": ["Fantasy"],
    "chill": ["Slice of Life"],
    "mystery": ["Mystery"],
    "comedy": ["Comedy"],
    "scifi": ["Sci-Fi"],
    "sports": ["Sports"],
    "mind": ["Psychological", "Thriller"],
}

# Mood -> keywords used to relevance-score results (also used for the
# offline fallback that scores your own catalog).
MOOD_KEYWORDS = {
    "happy": ["Comedy", "Slice of Life", "Music"],
    "sad": ["Drama"],
    "action": ["Action", "Martial Arts"],
    "romance": ["Romance"],
    "horror": ["Horror", "Psychological", "Thriller"],
    "fantasy": ["Fantasy", "Mahou Shoujo", "Adventure"],
    "chill": ["Slice of Life", "Music"],
    "mystery": ["Mystery", "Thriller", "Psychological"],
    "comedy": ["Comedy"],
    "scifi": ["Sci-Fi", "Mecha"],
    "sports": ["Sports", "Racing"],
    "mind": ["Psychological", "Thriller", "Supernatural"],
}

ANILIST_MOOD_QUERY = """query ($genre: [String], $minScore: Int, $sort: [MediaSort], $perPage: Int, $page: Int) {
  Page(page: $page, perPage: $perPage) {
    media(
      type: ANIME
      format_in: [TV, MOVIE, OVA, ONA, SPECIAL]
      genre_in: $genre
      averageScore_greater: $minScore
      sort: $sort
      isAdult: false
    ) {
      id
      title { romaji english }
      coverImage { large }
      averageScore
      genres
      episodes
      seasonYear
      description(asHtml: false)
    }
  }
}"""

ANILIST_TRENDING_QUERY = """query ($sort: [MediaSort], $perPage: Int, $page: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, format_in: [TV, MOVIE], sort: $sort, isAdult: false) {
      id
      title { romaji english }
      coverImage { large }
      averageScore
      genres
      episodes
      seasonYear
      description(asHtml: false)
    }
  }
}"""

_CATALOG_INDEX = None


def _anilist_post(query, variables):
    """POST a GraphQL query to AniList. Returns parsed JSON or None."""
    payload = _json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = _UrlRequest(
        ANILIST_API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "AnimeChat-MoodFinder/1.0",
        },
    )
    try:
        with _urlopen(req, timeout=ANILIST_TIMEOUT) as resp:
            return _json.loads(resp.read().decode("utf-8"))
    except (_URLError, _HTTPError, OSError, ValueError):
        return None


def _norm_key(text):
    """'My Hero Academia!' -> 'myheroacademia' (for catalog title matching)."""
    return _re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _safe_float(value, default=0.0):
    """float() that never throws — catalog ratings can be "N/A"."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _get_catalog_index():
    """slug -> normalized-title map of your live catalog, built once."""
    global _CATALOG_INDEX
    if _CATALOG_INDEX is None:
        _CATALOG_INDEX = {
            _norm_key(entry.get("title", "")): slug
            for slug, entry in anime_database.items()
        }
    return _CATALOG_INDEX


def _synopsis(media):
    text = _re.sub(r"<[^>]+>", " ", media.get("description") or "")
    text = _re.sub(r"\s+", " ", text).strip()
    return text[:220].rstrip() + ("…" if len(text) > 220 else "")


def _pick_from_anilist(media, catalog_index):
    """Convert an AniList media object into a pick dict the page can render.

    If the title also exists in your own catalog, `src` becomes "catalog"
    (so the button links to YOUR /anime/<slug> page). Otherwise `src` is
    "anilist" and the button links to the AniList page.
    """
    title = (media.get("title") or {}).get("english") or (media.get("title") or {}).get("romaji") or ""
    if not title:
        return None
    genres = media.get("genres") or []
    score = (media.get("averageScore") or 0) / 20.0
    slug = _re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "anime"
    local_slug = catalog_index.get(_norm_key(title))
    return {
        "t": title,
        "s": local_slug or (slug + "-" + str(media.get("id"))),
        "g": " • ".join(genres),
        "r": f"{score:.1f}",
        "e": media.get("episodes") or 0,
        "y": str(media.get("seasonYear") or ""),
        "i": (media.get("coverImage") or {}).get("large") or "",
        "d": _synopsis(media),
        "id": media.get("id"),
        "src": "catalog" if local_slug else "anilist",
    }


def _mood_relevance(pick, keywords):
    g = (pick.get("g") or "").lower()
    score = 0
    for kw in keywords:
        if kw.lower() in g:
            score += 2
    rating = _safe_float(pick.get("r"))
    if rating >= 4.4:
        score += 1
    elif rating >= 4.0:
        score += 0.5
    return score


def _live_picks(moods, catalog_index):
    """Fetch REAL anime live from AniList for the selected moods.

    Returns a list of picks, or None if AniList is unreachable
    (caller then falls back to scoring your own catalog).
    """
    seen = {}  # anilist id -> {"pick": ..., "score": ...}
    for mood in moods:
        keywords = MOOD_KEYWORDS.get(mood, [])
        for genre in MOOD_GENRES.get(mood, []):
            data = _anilist_post(
                ANILIST_MOOD_QUERY,
                {
                    "genre": [genre],
                    "minScore": 65,
                    "sort": ["SCORE_DESC"],
                    "perPage": 25,
                    "page": 1,
                },
            )
            if not data:
                continue
            for media in ((data.get("data") or {}).get("Page") or {}).get("media") or []:
                pick = _pick_from_anilist(media, catalog_index)
                if not pick:
                    continue
                score = _mood_relevance(pick, keywords)
                cur = seen.get(pick["id"])
                if cur is None or score > cur["score"]:
                    seen[pick["id"]] = {"pick": pick, "score": score}
    if not seen:
        return None
    ranked = sorted(
        seen.values(),
        key=lambda x: (x["score"], _safe_float(x["pick"].get("r"))),
        reverse=True,
    )
    return [item["pick"] for item in ranked][:24]


def _catalog_picks(moods):
    """Offline fallback: score YOUR live catalog (anime_database) per mood.

    Used only when AniList is unreachable — still computed at request
    time, never a hardcoded list.
    """
    by_slug = {}
    for mood in moods:
        keywords = MOOD_KEYWORDS.get(mood, [])
        scored = []
        for slug, entry in anime_database.items():
            pick = {
                "t": entry.get("title", ""),
                "s": slug,
                "g": entry.get("genre", ""),
                "r": str(entry.get("rating", "")),
                "e": entry.get("total_episodes", 0),
                "y": str(entry.get("release", "")),
                "i": _resolve_image(entry.get("image", "")),
                "d": (entry.get("synopsis") or "")[:220],
                "id": None,
                "src": "catalog",
            }
            score = _mood_relevance(pick, keywords)
            if score > 0:
                scored.append((score, pick))
        scored.sort(key=lambda x: (-x[0], -_safe_float(x[1]["r"])))
        for score, pick in scored[:40]:
            cur = by_slug.get(pick["s"])
            if cur is None or score > cur[0]:
                by_slug[pick["s"]] = (score, pick)
    ranked = sorted(by_slug.values(), key=lambda x: (-x[0], -_safe_float(x[1]["r"])))
    return [pick for _, pick in ranked][:8]


def _live_hero(catalog_index):
    """Today's pick: the top trending show on AniList right now."""
    data = _anilist_post(
        ANILIST_TRENDING_QUERY,
        {"sort": ["TRENDING_DESC", "SCORE_DESC"], "perPage": 1, "page": 1},
    )
    if not data:
        return None
    media = ((data.get("data") or {}).get("Page") or {}).get("media") or []
    return _pick_from_anilist(media[0], catalog_index) if media else None


def _daily_catalog_pick():
    """Fallback hero when AniList is down: top-rated catalog show, rotating daily."""
    rated = sorted(
        anime_database.items(),
        key=lambda kv: _safe_float(kv[1].get("rating")),
        reverse=True,
    )[:40]
    if not rated:
        return None
    slug, entry = rated[_date.today().toordinal() % len(rated)]
    return {
        "t": entry.get("title", ""),
        "s": slug,
        "g": entry.get("genre", ""),
        "r": str(entry.get("rating", "")),
        "e": entry.get("total_episodes", 0),
        "y": str(entry.get("release", "")),
        "i": _resolve_image(entry.get("image", "")),
        "d": (entry.get("synopsis") or "")[:220],
        "id": None,
        "src": "catalog",
    }


@app.route("/api/mood-picks")
def api_mood_picks():
    """JSON endpoint the page calls whenever the user changes moods."""
    moods = [
        m.strip()
        for m in request.args.get("moods", "").split(",")
        if m.strip() in MOOD_GENRES
    ]
    moods = list(dict.fromkeys(moods))  # dedupe, keep order

    catalog_index = _get_catalog_index()
    picks, source, note = [], "idle", ""
    if moods:
        picks = _live_picks(moods, catalog_index)
        if picks is None:
            picks = _catalog_picks(moods)
            source = "catalog"
            note = "Offline picks from your catalog (AniList unreachable)"
        else:
            source = "live"
            note = "Real picks fetched live from AniList"
    try:
        hero = _live_hero(catalog_index) or _daily_catalog_pick()
    except Exception as exc:
        print(f"[find-mood] hero fallback failed: {exc}", flush=True)
        hero = None
    return jsonify({"picks": picks, "hero": hero, "source": source, "note": note})


@app.route("/find-mood")
def find_mood():
    return render_template("find_mood.html")


@app.before_request
def _attach_user():
    load_logged_in_user()


@app.context_processor
def _inject_user():
    return {"current_user": g.get("user")}


@app.template_filter("anime_img")
def anime_img(image):
    """Templates call {{ image | anime_img }}. Local filenames resolve to
    /static/images/anime/<name>; full URLs (AniList CDN) pass through."""
    if image.startswith(("http://", "https://")):
        return image
    return url_for("static", filename="images/anime/" + image)


# ---------------------------------------------------------------------------
# Streaming provider branding + helpers
# ---------------------------------------------------------------------------
# provider_brand maps a service name (e.g. "Crunchyroll Amazon Channel") to a
# monogram chip: css class, short mark and brand color used by the
# Where to Watch list on anime pages.

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
    """Map a provider name to a {cls, mark, color} logo-chip descriptor."""
    n = (name or "").lower()
    for key, cls, mark, color in _PROVIDER_BRANDS:
        if key in n:
            return {"cls": cls, "mark": mark, "color": color}
    return {"cls": "generic", "mark": (name or "TV")[:2].upper(), "color": "#5a6a7a"}


@app.template_filter("sort_streaming")
def sort_streaming(services):
    """Order streaming services: Streaming first, then Free, then the rest,
    alphabetical by name within each tier."""
    order = {"Streaming": 0, "Free": 1, "Free with Ads": 2, "Rent": 3, "Buy": 4}
    return sorted(
        services or [],
        key=lambda s: (order.get(s.get("monetization"), 5), (s.get("name") or "").lower()),
    )


@app.template_filter("real_dubs")
def real_dubs(dubs):
    """A show's original language (Japanese) is subtitled content, not a dub.
    Strip it so 'Dub Available' only lists actual alternate-language dubs."""
    return [d for d in (dubs or []) if str(d).strip().lower() not in ("japanese", "ja", "japanese (original)")]


# ---------------------------------------------------------------------------
# Live airing-schedule refresher
# ---------------------------------------------------------------------------
# The catalog's next-episode timestamps are baked in at build time. To keep
# the Airing Now / Upcoming views honest (tomorrow's episode shows up on its
# own), a background thread re-fetches nextAiringEpisode from AniList every
# SCHEDULE_TTL seconds and patches the in-memory entries. Page loads never
# block: they kick off a refresh only if one isn't already running.

_SCHEDULE_TTL = 1800  # refresh at most every 30 minutes
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

# anilist_id -> (slug, entry) index used to patch entries after a refresh.
_BY_AID = {
    e["anilist_id"]: (slug, e)
    for slug, e in anime_database.items()
    if e.get("anilist_id")
}


def _save_fresh_airing_cache(fresh):
    """Persist freshly-fetched AniList airing info into the on-disk airing
    cache (anime_airing_a*.json) so the next apply_airing() run sees fresh
    status / next-episode data and flips newly-aired episodes to released
    without anyone re-running the fetch step by hand."""
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
    """Fetch fresh airing info for every Ongoing/Upcoming title and patch the
    in-memory catalog, so the site always points at the real next episode."""
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
                    # No next episode scheduled (finished airing): drop the
                    # stale timestamp so it leaves the "airing soon" group.
                    entry["next_episode_at"] = None
                sd = m.get("startDate") or {}
                if sd.get("year"):
                    entry["start_year"] = sd["year"]
                if sd.get("month"):
                    entry["start_month"] = sd["month"]
        except Exception:
            continue
        time.sleep(1.0)  # stay well under AniList's 90 req/min limit

    if fresh:
        _save_fresh_airing_cache(fresh)

    with _schedule_lock:
        _schedule_state["last"] = time.time()
        _schedule_state["running"] = False


def _ensure_airing_schedule():
    """Non-blocking: start a background refresh if the cached schedule is
    stale and no refresh is already in flight."""
    with _schedule_lock:
        stale = time.time() - _schedule_state["last"] > _SCHEDULE_TTL
        if stale and not _schedule_state["running"]:
            _schedule_state["running"] = True
            threading.Thread(
                target=_refresh_airing_schedule_worker, daemon=True
            ).start()


def _schedule_loop():
    """Background loop that keeps the schedule fresh for the app's lifetime."""
    while True:
        _ensure_airing_schedule()
        time.sleep(_SCHEDULE_TTL)


# ---------------------------------------------------------------------------
# Full auto-enrichment (airing + TVmaze + HD upgrade) every 10 minutes
# ---------------------------------------------------------------------------
# The live schedule refresh above only updates the in-memory next_episode_at
# fields. This thread runs the full enrichment pipeline that actually writes
# TBC markers, new episode titles, and HD thumbnails to anime_data.json and
# reloads it so the app sees the changes.

_ENRICH_TTL = 600  # 10 minutes

_enrich_state = {"last": 0.0, "running": False}
_enrich_lock = threading.Lock()


def _full_enrich_worker():
    """Run the full enrichment pipeline: airing apply, TVmaze backfill, HD
    upgrade, then reload the database in memory. Each stage is isolated so a
    slow/failed stage (e.g. TVmaze being unreachable) never blocks the
    database reload -- the site always serves the freshest data on disk."""
    from anime_data import reload_database

    try:
        # Step 1: Airing apply (AniList data -> TBC markers, statuses)
        from scripts.enrich_airing import apply_airing, tvmaze_backfill
        try:
            apply_airing()
        except Exception as exc:
            print(f"[auto-enrich] apply_airing failed (continuing): {exc}", flush=True)

        # Step 2: Reload so released/TBC changes show immediately, even if
        # the TVmaze backfill below hangs on the network.
        try:
            reload_database()
        except Exception as exc:
            print(f"[auto-enrich] reload after apply failed: {exc}", flush=True)

        # Step 3: TVmaze backfill for newest aired episodes (network-bound;
        # failures here are non-fatal).
        try:
            tvmaze_backfill(
                count=0,
                todo_path="anime_airing_todo.json",
                cross_path="anime_ep_thumbs_crosstodo.json",
            )
        except Exception as exc:
            print(f"[auto-enrich] tvmaze_backfill failed (continuing): {exc}", flush=True)

        # Step 4: HD thumbnail upgrade
        try:
            from scripts.upgrade_thumbs_to_hd import main as hd_upgrade
            hd_upgrade()
        except Exception as exc:
            print(f"[auto-enrich] hd_upgrade failed (continuing): {exc}", flush=True)

        # Step 5: Reload the database so the running app sees the changes
        reload_database()

        # Rebuild the anilist_id index used by the live schedule refresher
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
    """Background loop that runs the full enrichment pipeline on a timer.

    Runs once at startup (after a short delay) and then every _ENRICH_TTL
    seconds (10 minutes) for the life of the app.
    """
    time.sleep(120)  # wait 2 minutes for the app to finish starting
    while True:
        with _enrich_lock:
            if not _enrich_state["running"]:
                _enrich_state["running"] = True
                threading.Thread(
                    target=_full_enrich_worker, daemon=True
                ).start()
        time.sleep(_ENRICH_TTL)


# The full catalog lives in anime_data.py (auto-generated by
# scripts/fetch_anime_catalog.py). Helpers below build sorted/filtered views.

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
    """Derive the genre list from the catalog, most common first."""
    from collections import Counter

    counter = Counter()
    for entry in anime_database.values():
        for genre in entry.get("genre", "").split(" \u2022 "):
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
        # High rating but low popularity = hidden gems.
        return (rating, -popularity)
    if sort == "trending":
        return (year, popularity)
    return (year, popularity)  # latest


def _catalog_entries(sort="latest", genre=None, limit=None):
    """Return catalog entries enriched with live rating/votes, sorted and
    optionally filtered by genre. Used by home + browse + category pages.

    "new" shows anime AIRING NOW (next episode countdown, soonest first),
    "upcoming" shows titles not yet released (expected start date, soonest
    first), and "latest" excludes upcoming titles so the homepage never
    mixes them with actual releases.
    """
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
            # A dub only counts when an English dub is available; Japanese
            # audio is the original (sub) track, not a dub.
            "has_dub": any(
                str(d).strip().lower() == "english"
                for d in (entry.get("dub") or [])
            ),
            "has_sub": bool(entry.get("subtitles")),
            "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
        })

    if sort in ("new", "upcoming"):
        # Airing now: soonest next episode first. Upcoming: soonest start.
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
    """Humanized 'next episode' label for the Airing Now view."""
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
    """Humanized expected-start label for the Upcoming view."""
    y = entry.get("start_year")
    m = entry.get("start_month")
    if y and m and 1 <= m <= 12:
        return f"EXP {calendar.month_abbr[m].upper()} {y}"
    if y:
        return f"EXP {y}"
    return "UPCOMING"


def _decorate(entries, sort):
    """Attach per-card badges for the Airing Now / Upcoming views."""
    for entry in entries:
        if sort == "new":
            entry["badge_label"] = _episode_badge(entry)
        elif sort == "upcoming":
            entry["badge_label"] = _start_badge(entry)
    return entries


@app.route("/")
def home():
    """Homepage shows the LATEST releases (most recent first) -- the full
    catalog lives behind the Browse links in the navbar."""
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
    """Search the FULL catalog (not just the home page grid). Returns JSON so
    the navbar search can open an anime page directly."""
    q = (request.args.get("q") or "").strip().lower()
    if not q:
        return jsonify({"success": True, "results": []})

    results = []

    # Normalize a title/query so punctuation and spacing don't block a
    # match: lowercase, drop apostrophes, and collapse separators (spaces,
    # hyphens, dashes, colons, dots, slashes, parens) to single spaces.
    # "one-piece", "ONE PIECE", and "One Piece" all normalize to
    # "one piece"; "Bleach: Thousand-Year Blood War" normalizes to
    # "bleach thousand year blood war".
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
        # Fallback: every query word must appear in the title. This lets
        # "dragon ball kai" find "Dragon Ball Z Kai" (a plain substring
        # check fails because of the "Z").
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
    """Resolve (anime, season, episode) or raise a 404."""
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
    """Rate a single episode out of 10. Only logged-in users can submit a
    review; everyone can view the aggregate score and the review list."""
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
    """Returns the live average rating, vote breakdown, and every review
    written for this anime, computed straight from the database."""

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
    """Accepts a star rating (1-5) plus an optional username and review
    text, stores it, and returns the freshly recalculated average across
    every rating submitted so far."""

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


# ---------------------------------------------------------------------------
# Mood Finder -- recommends real catalog titles instead of a hardcoded list.
# ---------------------------------------------------------------------------
# Each mood maps to the genre tags (from the real catalog) that best match
# it. A title only needs ONE of the listed genres to qualify.
MOOD_GENRE_MAP = {
    "happy":   ["Comedy", "Slice of Life"],
    "sad":     ["Drama"],
    "action":  ["Action", "Adventure"],
    "romance": ["Romance"],
    "horror":  ["Horror", "Thriller"],
    "fantasy": ["Fantasy", "Adventure"],
    "chill":   ["Slice of Life", "Music"],
    "mystery": ["Mystery", "Thriller"],
    "comedy":  ["Comedy"],
    "scifi":   ["Sci-Fi", "Mecha"],
    "sports":  ["Sports"],
    "mind":    ["Psychological", "Mystery"],
}

MOOD_TITLES = {
    "happy":   "Feel-Good Picks",
    "sad":     "Grab the Tissues",
    "action":  "Pure Adrenaline",
    "romance": "Heart Fluttering",
    "horror":  "Sleep With the Lights On",
    "fantasy": "Magic & Adventure",
    "chill":   "Slow Down & Relax",
    "mystery": "Solve It Yourself",
    "comedy":  "Guaranteed Laughs",
    "scifi":   "Beyond Tomorrow",
    "sports":  "Game On",
    "mind":    "Prepare to Be Confused",
}


def _resolve_image(image):
    """Mirror the anime_img template filter for JSON responses."""
    if not image:
        return ""
    if image.startswith(("http://", "https://")):
        return image
    return url_for("static", filename="images/anime/" + image)


@app.route("/api/mood/<mood>")
def api_mood_recommendations(mood):
    """Return real catalog anime matching a mood, ranked by live community
    rating (falling back to the seed rating when a title has no votes yet),
    with a bit of shuffling in the eligible pool so re-picking the same
    mood ("Surprise Me") doesn't always return the exact same set."""
    import random

    genres = MOOD_GENRE_MAP.get(mood)
    if not genres:
        return jsonify({"success": False, "error": "Unknown mood"}), 404

    genres_lower = [g.lower() for g in genres]
    all_stats = get_all_anime_stats()

    pool = []
    for slug, entry in anime_database.items():
        entry_genres = entry.get("genre", "").lower()
        if not any(g in entry_genres for g in genres_lower):
            continue

        stats = all_stats.get(slug, {"votes": 0, "average": 0})
        try:
            seed_rating = float(entry.get("rating") or 0)
        except (TypeError, ValueError):
            seed_rating = 0.0
        live_rating = stats["average"] if stats["votes"] > 0 else seed_rating

        # Skip titles with basically no data to rank on -- keeps low-quality
        # / placeholder entries out of recommendations.
        if live_rating <= 0 and not entry.get("synopsis"):
            continue

        pool.append({
            "slug": slug,
            "title": entry.get("title", slug),
            "image": _resolve_image(entry.get("image", "")),
            "genre": entry.get("genre", ""),
            "rating": round(live_rating, 1),
            "votes": stats["votes"],
            "status": entry.get("status", ""),
            "episodes": entry.get("total_episodes", 0) or 0,
            "synopsis": (entry.get("synopsis") or "")[:160],
            "url": url_for("anime", anime_slug=slug),
            "_sort": (live_rating, entry.get("member_count", 0) or 0),
        })

    pool.sort(key=lambda e: e["_sort"], reverse=True)

    # Take a generous top slice (quality bar), then randomly sample from it
    # so refreshing the mood surfaces different-but-still-good picks.
    top_pool = pool[:40] if len(pool) > 40 else pool
    sample_size = min(9, len(top_pool))
    results = random.sample(top_pool, sample_size) if top_pool else []
    results.sort(key=lambda e: e["_sort"], reverse=True)
    for r in results:
        del r["_sort"]

    return jsonify({
        "success": True,
        "mood": mood,
        "title": MOOD_TITLES.get(mood, "Recommended Anime"),
        "results": results,
    })


if __name__ == "__main__":
    create_tables()
    # Keep the airing schedule fresh for the life of the app.
    threading.Thread(target=_schedule_loop, daemon=True).start()
    # Full auto-enrichment: runs the airing apply + TVmaze backfill + HD
    # upgrade every 6 hours so the catalog stays current without manual
    # intervention. The daemon thread is safe to ignore on import.
    threading.Thread(target=_full_enrich_loop, daemon=True).start()
    # Bind to 0.0.0.0 and honor the PORT env var so the managed preview can
    # reach the dev server. The reloader subprocess is disabled because the
    # platform manages the process lifecycle.
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        use_reloader=False,
    )