import calendar
import functools
import json
import os
import random
import re
import threading
import time
from collections import defaultdict
from datetime import timedelta

import requests

from dotenv import load_dotenv
load_dotenv()

from flask import make_response, Flask, render_template, request, jsonify, g, url_for, flash, redirect, abort

from anime_data import anime_database, preload_catalog
from review_vote_gate import apply_review_vote_gate
apply_review_vote_gate()
from review_history_patch import apply_review_history_fix
apply_review_history_fix()
from dev_boost import apply_dev_boost
apply_dev_boost()
from characters_data import search_characters, index_stats, reload_characters
from database import (
    create_tables,
    get_connection,
    get_anime_stats,
    get_all_anime_stats,
    add_review,
    add_episode_review,
    get_all_reviews,
    get_all_episode_reviews,
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
    get_taste_slugs,
    MAX_USER_LISTS,
    get_site_stats,
    get_community_chat_stats,
    is_community_member,
    get_all_community_member_counts,
    get_user_xp,
    get_user_rank,
    get_all_user_ranks,
    xp_progress,
    toggle_review_like,
    get_review_likes,
    add_review_reply,
    get_review_replies,
    add_war_entry,
    get_war_entries,
    get_all_wars,
    create_warzone,
    get_warzones,
    get_warzone,
    add_warzone_entry,
    migrate_replies_to_war,
    reward_war_leaders,
    settle_war_outcomes,
    get_war_effects,
    get_bulk_review_likes,
    get_user_review_history,
    get_user_review,
    delete_user_review,
    delete_episode_review,
    set_profile_public,
    recalculate_user_xp,
)
from auth import auth, load_logged_in_user
from review_votes import (
    toggle_anime_review_vote,
    get_user_anime_review_votes,
    get_bulk_reviewer_ranks,
    anime_grade_engine,
    review_vote_xp,
    review_level_for_xp,
    review_rank_for_xp,
    get_bulk_review_points,
    can_dislike,
    toggle_reason_vote,
    get_review_reasons,
    toggle_war_vote,
    RANK_COLORS,
    RANK_WEIGHTS,
    RATING_LABELS,
    RATING_BANDS,
    VOTE_RATE,
    rating_band,
    rating_label,
    TRUSTED_RANKS,
    GRADE_ORDER,
    get_target_review_ids,
    overall_review_xp,
    format_xp_label,
)
from chat import chat_bp
from profile_routes import bp as profile_bp

from threads import init_threads
from ota_chan import init_ota_chan

# ---- Rating Power vote gate + local-SQLite lock fix, on the plain vote route ----
# vote_review calls the module-global `toggle_review_like` at call time, so
# rebinding it here (same pattern as review_history_patch) keeps hand-crafted
# below-C like/dislike requests blocked everywhere, not just in the UI. Under
# Rating Power: dislikes and likes on RED (1-4) reviews require C rank (500
# XP)+; D-rank accounts can still like GREEN/GREY reviews (+3); F accounts
# can't vote at all. The reimplement also commits the vote BEFORE
# recalculating the author's XP — the original leaves the INSERT uncommitted
# while recalc opens a second connection, which deadlocks on local SQLite (it
# only works on Turso because that backend is autocommit).
def _gated_toggle_review_like(user_id, review_type, review_id, is_like):
    voter_rank = get_user_rank(user_id)
    if voter_rank == "F":
        raise PermissionError("Your account is flagged — reviewing power suspended.")
    if not can_dislike(voter_rank):
        if not is_like:
            raise PermissionError(
                "Dislikes require C rank (500 XP) — D-rank accounts can only like."
            )
        # D can like GREEN/GREY reviews, but liking a RED (1-4) verdict is C+.
        _db2 = __import__("database")
        _c = _db2.get_connection()
        _cur = _c.cursor()
        if review_type == "episode":
            _cur.execute("SELECT rating FROM episode_reviews WHERE id=?", (review_id,))
        else:
            _cur.execute("SELECT rating FROM reviews WHERE id=?", (review_id,))
        _rr = _cur.fetchone()
        _c.close()
        if _rr is None:
            raise PermissionError("Review not found.")
        if int(_rr["rating"] or 0) <= 4:
            raise PermissionError("Liking a negative review requires C rank (500 XP).")
    import database as _db
    conn = _db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type=? AND review_id=?",
        (user_id, review_type, review_id),
    )
    existing = cursor.fetchone()
    review_author_id = None
    if review_type == "episode":
        cursor.execute("SELECT user_id FROM episode_reviews WHERE id=?", (review_id,))
        rr = cursor.fetchone()
        if rr:
            review_author_id = rr["user_id"]
    elif review_type == "anime":
        cursor.execute("SELECT user_id FROM reviews WHERE id=?", (review_id,))
        rr = cursor.fetchone()
        if rr:
            review_author_id = rr["user_id"]
    removed = False
    new_is_like = is_like
    if existing:
        if existing["is_like"] == is_like:
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
            removed = True
        else:
            cursor.execute(
                "UPDATE review_likes SET is_like=? WHERE id=?", (is_like, existing["id"])
            )
    else:
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like) VALUES (?, ?, ?, ?)",
            (user_id, review_type, review_id, is_like),
        )
    conn.commit()  # release the write lock BEFORE the recalc connection writes
    if review_author_id:
        try:
            _db.recalculate_user_xp_preserving_rewards(review_author_id)
        except Exception:
            pass  # the vote is already saved — XP recalc can retry next vote
    conn.close()
    return new_is_like, removed


toggle_review_like = _gated_toggle_review_like


app = Flask(__name__)
# Never let browsers (or proxies) cache static JS/CSS - stale threads.js made
# fixed UI look broken. HTML is already no-store via the after_request hook.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.secret_key = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Sessions last 10 years, so users stay logged in across devices/visits.
app.permanent_session_lifetime = timedelta(days=3650)


app.register_blueprint(auth)
app.register_blueprint(chat_bp)
app.register_blueprint(profile_bp)

# Standalone War Zone (Free / Friendly wars) - registered here so the blueprint
# is mounted alongside the rest of the app.
from war_zone_routes import wz_bp

app.register_blueprint(wz_bp)



init_threads(app)
init_ota_chan(app)


# Make sure the profile/history/list tables exist even if the app is
# imported (not only when run as __main__). Idempotent.
create_tables()

# Kick off catalog preload in background so the first request isn't blocked
# by the 58 MB JSON parse (avoids Render health-check timeouts).
preload_catalog()


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
    from i18n import t, ja, get_language
    return {
        "current_user": g.get("user"),
        "t": t,
        "ja": ja,
        "current_lang": get_language(),
        "is_developer": is_developer,
        "get_warzones": get_warzones,
    }


def is_developer(username):
    """True if this username is an Otakul developer (badge tag + S+ boost)."""
    from dev_accounts import is_dev_username
    return is_dev_username(username)


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


@app.template_filter("jp_title")
def jp_title(anime):
    """Return the Japanese native title for an anime dict when the site is
    in 日本語 mode; otherwise (or when we don't have a native title) return
    the English/romaji title. User-facing anime names are translated, which
    is exactly what the language toggle promises — user content (reviews,
    chat, guild names) is never touched."""
    from i18n import get_language
    if get_language() != "ja":
        return anime.get("title", "") if isinstance(anime, dict) else str(anime or "")
    if not isinstance(anime, dict):
        return str(anime or "")
    slug = anime.get("slug", "")
    info = _jp_titles_map().get(slug)
    if info and info.get("native"):
        return info["native"]
    return anime.get("title", "")


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


def _apply_episode_state(entry, st, nxt):
    """Mirror apply_airing's released/TBC logic on the in-memory catalog.

    Runs off the fresh AniList schedule data so the deployed site keeps
    every episode's released state and next-episode countdown accurate
    without loading a second copy of the catalog (safe on Render's 512MB
    free tier, where the heavy disk-based enrichment is disabled).
    """
    try:
        from scripts.enrich_airing import _global_number, _is_placeholder
    except Exception:
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


# ---------------------------------------------------------------------------
# Live TVmaze title/thumb fill for aired episodes (runs inside the airing
# worker, on every host including Render — in-memory only, no disk writes,
# so it can't OOM the 512MB free tier like the heavy disk enrichment).
# ---------------------------------------------------------------------------

_TVMAZE_FILL_ONGOING_QUOTA = 12   # ongoing shows looked up per cycle
_TVMAZE_FILL_COMPLETED_QUOTA = 6   # completed shows per cycle (long tail)
_TVMAZE_FILL_ONGOING_TTL = 6 * 3600      # retry an ongoing miss after 6h
_TVMAZE_FILL_COMPLETED_TTL = 24 * 3600   # completed shows: once a day max
_tvmaze_fill_state = {"last_attempt": {}}


def _tvmaze_live_fill():
    """Fill real titles + HD thumbs for aired episodes straight from TVmaze.

    A freshly-aired episode keeps showing 'TBC' with no still until someone
    re-enriches the static catalog and redeploys. This closes that gap: every
    airing cycle we look up the most popular ongoing shows that still have
    aired episodes missing a title or thumb, and patch the in-memory catalog
    from TVmaze (real names + original_untouched HD stills). New episodes
    appear with their name and artwork within ~10 minutes of TVmaze
    publishing them, on Render included.

    Bounded: at most _TVMAZE_FILL_QUOTA shows per cycle, misses backed off
    (6h ongoing / 24h completed), so TVmaze sees a handful of requests per
    10-minute cycle.
    """
    try:
        from scripts.enrich_airing import _backfill_one, _needs_backfill
    except Exception:
        return
    now = time.time()
    candidates = []
    for slug, entry in anime_database.items():
        status = entry.get("status")
        if status == "Ongoing":
            nxt = entry.get("next_episode")
            aired = (nxt - 1) if nxt else (entry.get("total_episodes") or 0)
            backoff = _TVMAZE_FILL_ONGOING_TTL
        elif status == "Completed":
            aired = entry.get("total_episodes") or 0
            backoff = _TVMAZE_FILL_COMPLETED_TTL
        else:
            continue
        if aired <= 0 or not _needs_backfill(entry, aired):
            continue
        last = _tvmaze_fill_state["last_attempt"].get(slug, 0.0)
        if now - last < backoff:
            continue
        candidates.append((status, entry.get("member_count") or 0, entry, aired))
    candidates.sort(key=lambda c: -c[1])

    def _run(entries, quota):
        for _, _, entry, aired in entries[:quota]:
            try:
                t, th = _backfill_one(entry, aired)
            except Exception as exc:
                t, th = 0, 0
                print(f"[tvmaze-fill] {entry.get('slug')} failed: {exc}", flush=True)
            _tvmaze_fill_state["last_attempt"][entry.get("slug")] = now
            if t or th:
                print(f"[tvmaze-fill] {entry.get('slug')}: +{t} titles, +{th} HD thumbs", flush=True)
            time.sleep(0.15)

    # Ongoing first (new episodes must appear asap), completed after.
    _run([c for c in candidates if c[0] == "Ongoing"], _TVMAZE_FILL_ONGOING_QUOTA)
    _run([c for c in candidates if c[0] != "Ongoing"], _TVMAZE_FILL_COMPLETED_QUOTA)


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
        except Exception:
            continue
        time.sleep(1.0)

    # Patch aired-but-missing titles/thumbs from TVmaze (in-memory, bounded).
    try:
        _tvmaze_live_fill()
    except Exception as exc:
        print(f"[tvmaze-fill] pass failed: {exc}", flush=True)

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
    if sort == "underrated":
        return (rating, -popularity)
    if sort == "trending":
        return (year, popularity)
    return (year, popularity)


def _catalog_entries(sort="latest", genre=None, limit=None):
    all_stats = get_all_anime_stats()
    _ensure_airing_schedule()
    real_members = get_all_community_member_counts()

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
            "member_count": real_members.get(slug, 0),
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
    latest = _catalog_entries(sort="latest", limit=64)
    recommended, recommended_genres = _home_picks()
    site_stats = get_site_stats()

    # --- For You cards first, then New cards ---
    rec_list = []
    new_list = []
    seen = set()
    for a in (recommended or []):
        slug = a.get("slug")
        if slug and slug not in seen:
            a["badge_label"] = "FOR YOU"
            a["_section"] = "for_you"
            rec_list.append(a)
            seen.add(slug)
    for a in (latest or []):
        slug = a.get("slug")
        if slug and slug not in seen:
            a["badge_label"] = "NEW"
            a["_section"] = "new"
            new_list.append(a)
            seen.add(slug)

    # For You section first, then New section
    ordered = rec_list + new_list

    return render_template(
        "index.html",
        anime_list=ordered,
        for_you_list=rec_list,
        new_list=new_list,
        page_title="Discover Anime",
        genres=_genre_list(),
        site_stats=site_stats,
    )


BROWSE_PAGE_SIZE = 60


def _browse_page(entries, page):
    """Slice a full sorted catalog list into one page. Returns
    (page_entries, page, has_more, loaded_so_far)."""
    total = len(entries)
    start = (page - 1) * BROWSE_PAGE_SIZE
    page_entries = entries[start:start + BROWSE_PAGE_SIZE]
    loaded = min(start + BROWSE_PAGE_SIZE, total)
    return page_entries, page, start + BROWSE_PAGE_SIZE < total, loaded, total


@app.route("/privacy")
def privacy_policy():
    """Privacy policy page — explains data collection, third parties,
    cookies, rights, and contact info."""
    return render_template("privacy.html", genres=_genre_list())


@app.route("/changelog")
def changelog():
    """What's New page — the changelog box listing everything shipped."""
    return render_template("changelog.html", genres=_genre_list())


@app.route("/browse")
def browse():
    sort = request.args.get("sort", "popular")
    if sort not in SORT_TITLES:
        sort = "popular"
    page = request.args.get("page", type=int) or 1
    partial = request.args.get("partial") == "1"

    page_entries, page, has_more, loaded, total = _browse_page(
        _decorate(_catalog_entries(sort=sort), sort), page
    )
    next_url = None
    if has_more:
        args = {k: v for k, v in request.args.items() if k not in ("page", "partial")}
        args["page"] = page + 1
        args["partial"] = "1"
        next_url = url_for(request.endpoint, **request.view_args, **args)

    ctx = dict(
        anime_list=page_entries,
        page_title=SORT_TITLES[sort],
        active_sort=sort,
        sort_titles=SORT_TITLES,
        genres=_genre_list(),
        has_more=has_more,
        next_url=next_url,
        loaded_so_far=loaded,
        total=total,
    )
    if partial:
        return render_template("_browse_grid.html", **ctx)
    return render_template("browse.html", **ctx)


@app.route("/category/<genre>")
def category(genre):
    page = request.args.get("page", type=int) or 1
    partial = request.args.get("partial") == "1"

    page_entries, page, has_more, loaded, total = _browse_page(
        _decorate(_catalog_entries(sort="popular", genre=genre), "popular"), page
    )
    next_url = None
    if has_more:
        args = {k: v for k, v in request.args.items() if k not in ("page", "partial")}
        args["page"] = page + 1
        args["partial"] = "1"
        next_url = url_for(request.endpoint, genre=genre, **args)

    ctx = dict(
        anime_list=page_entries,
        page_title=f"{genre} Anime",
        active_genre=genre,
        sort_titles=SORT_TITLES,
        genres=_genre_list(),
        has_more=has_more,
        next_url=next_url,
        loaded_so_far=loaded,
        total=total,
    )
    if partial:
        return render_template("_browse_grid.html", **ctx)
    return render_template("browse.html", **ctx)


@functools.lru_cache(maxsize=1)
def _jp_titles_map():
    """Load the small Japanese-title cache (slug -> {native, romaji})."""
    try:
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anime_jp_titles.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


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

    jp_map = _jp_titles_map()
    matches = set()
    # First pass: english/romaji title match
    for slug, entry in anime_database.items():
        title = entry.get("title", "")
        if _matches(title):
            matches.add(slug)

    # Second pass: Japanese native / romaji title match (fast, uses small map)
    if any(ord(c) > 0x2e80 for c in q):  # contains CJK/Kana → search Japanese titles
        for slug, info in jp_map.items():
            native = info.get("native") or ""
            romaji = info.get("romaji") or ""
            if native and q in native.lower():
                matches.add(slug)
            elif romaji and _matches(romaji):
                matches.add(slug)
    else:
        # Romaji search: also match romaji field for JP-only titles
        for slug, info in jp_map.items():
            romaji = info.get("romaji") or ""
            if romaji and _matches(romaji):
                matches.add(slug)

    for slug in matches:
        entry = anime_database.get(slug)
        if not entry:
            continue
        info = jp_map.get(slug) or {}
        results.append({
            "slug": slug,
            "title": entry.get("title", ""),
            "jp_title": info.get("native") or "",
            "image": entry.get("image", ""),
            "year": entry.get("release", ""),
            "rating": entry.get("rating", "N/A"),
            "_members": entry.get("member_count", 0),
        })

    # Sort by member count (popularity) so main entries rank above spinoffs
    results.sort(key=lambda r: r.get("_members", 0), reverse=True)
    results = results[:12]
    # Remove internal sort key
    for r in results:
        r.pop("_members", None)

    return jsonify({"success": True, "results": results})


def _smart_recommendations(anime, limit=6):
    """Pick recommendations from the same studio or genre, not random trash."""
    slug = anime.get("slug", "")
    studio = (anime.get("studio") or "").strip()
    genre_str = anime.get("genre") or ""
    genres = set(g.strip().lower() for g in genre_str.split("•") if g.strip())
    scored = []
    for s, entry in anime_database.items():
        if s == slug:
            continue
        score = 0
        # Same studio = high priority
        if studio and (entry.get("studio") or "").strip().lower() == studio.lower():
            score += 10
        # Shared genres
        entry_genres = set(g.strip().lower() for g in (entry.get("genre") or "").split("•") if g.strip())
        score += len(genres & entry_genres) * 3
        # Popularity boost
        members = entry.get("member_count", 0) or 0
        if members > 10000:
            score += 2
        elif members > 1000:
            score += 1
        if score > 0:
            scored.append((score, entry.get("member_count", 0), s, entry))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [{"slug": s, "title": e.get("title", s), "image": e.get("image", "")}
            for _, _, s, e in scored[:limit]]


@app.route("/anime/<anime_slug>")
def anime(anime_slug):
    anime = anime_database.get(anime_slug)
    if anime is None:
        return "Anime not found", 404
    user = g.get("user")
    if user is not None:
        record_view(user["id"], anime_slug)
    # Override static recommendations with smart studio/genre picks
    smart_recs = _smart_recommendations(anime)
    anime_with_recs = dict(anime)
    anime_with_recs["recommendations"] = smart_recs
    # ---- Grade / badge engine for this anime (trusted vs audience) ----
    grade_card = None
    try:
        stats = get_anime_stats(anime_slug)
        revs = stats.get("reviews") or []
        if revs:
            rank_map = get_bulk_reviewer_ranks([r.get("user_id") for r in revs])
            grade_card = anime_grade_engine(revs, rank_map)
    except Exception:
        grade_card = None
    # Overall 'liquid XP' total across every anime review (fills the wide
    # gauge next to the headline grade). Always computed when there is any
    # review data so the gauge reflects the community's full feel.
    overall_xp = 0
    overall_count = 0
    try:
        overall_xp, overall_count = overall_review_xp(
            "anime", get_target_review_ids("anime", anime_slug)
        )
    except Exception:
        overall_xp, overall_count = 0, 0
    overall_negative = overall_xp < 0
    overall_label = format_xp_label(overall_xp)
    overall_tier = review_rank_for_xp(overall_xp) if not overall_negative else None
    user_rank = get_user_rank(user["id"]) if user else None
    # Episode review stats may be stored under either the season display
    # name or the raw season index (AJAX-written rows) — alias the index
    # groups onto the catalog season names so per-episode ratings show.
    episode_stats = get_all_episode_stats(anime_slug)
    for si, s in enumerate((anime_with_recs.get("seasons") or []), 1):
        sname = s.get("name")
        if not sname:
            continue
        idx_group = episode_stats.pop(str(si), None)
        if not idx_group:
            continue
        if sname not in episode_stats:
            episode_stats[sname] = idx_group
            continue
        dst = episode_stats[sname]
        for ep, st in idx_group.items():
            if ep not in dst:
                dst[ep] = st
                continue
            v1, v2 = dst[ep].get("votes", 0), st.get("votes", 0)
            a1, a2 = dst[ep].get("average", 0), st.get("average", 0)
            total = v1 + v2
            dst[ep] = {
                "average": round((a1 * v1 + a2 * v2) / total, 1) if total else 0,
                "votes": total,
            }
    return render_template(
        "anime.html",
        anime=anime_with_recs,
        next_episode_label=_episode_badge(anime),
        episode_stats=episode_stats,
        grade_card=grade_card,
        overall_xp=overall_xp,
        overall_count=overall_count,
        overall_label=overall_label,
        overall_tier=overall_tier,
        overall_negative=overall_negative,
        user_rank=user_rank,
        vote_schedule=_build_rating_power(),
        GRADE_ORDER=GRADE_ORDER,
    )


def _season_name_prongs(anime_slug, season_name):
    """Resolve the (display_name, index) pair for a season given either
    spelling. Episode reviews are stored under both (AJAX writes the raw
    index, the form POST writes the display name), so lookups pass both."""
    entry = anime_database.get(anime_slug)
    seasons = (entry or {}).get("seasons") or []
    try:
        idx = int(season_name)
        if 1 <= idx <= len(seasons):
            return (seasons[idx - 1].get("name") or season_name, season_name)
    except (TypeError, ValueError):
        pass
    if seasons:
        for i, s in enumerate(seasons, 1):
            if s.get("name") == season_name:
                return (season_name, str(i))
    return (season_name, None)


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
    # Never allow an episode still to be inherited from another Bleach series
    # or another cour. The catalog enrichment is the source of truth; the
    # explicit guard also protects old cached review links from contamination.
    if anime_slug == "bleach-thousand-year-blood-war-the-calamity":
        valid_prefix = "1584708" if episode_number == 1 else "159020"
        if episode.get("thumb") and valid_prefix not in episode["thumb"]:
            episode = dict(episode)
            episode.pop("thumb", None)
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
            rating, comment, season_index=season_idx,
        )
        flash(f"Thanks for rating {episode_title}!", "success")
        return redirect(url_for("episode_rate", anime_slug=anime_slug,
                                season_idx=season_idx,
                                episode_number=episode_number))

    stats = get_episode_stats(anime_slug, season_name, episode_number,
                              season_index=season_idx)
    # Overall 'liquid XP' total across this episode's reviews (fills the
    # wide gauge next to the episode's rating).
    overall_xp = 0
    overall_count = 0
    try:
        overall_xp, overall_count = overall_review_xp(
            "episode",
            get_target_review_ids(
                "episode", anime_slug, season_name,
                season_index=season_idx, episode_number=episode_number,
            ),
        )
    except Exception:
        overall_xp, overall_count = 0, 0
    overall_negative = overall_xp < 0
    overall_label = format_xp_label(overall_xp)
    overall_tier = review_rank_for_xp(overall_xp) if not overall_negative else None
    user = g.get("user")
    my_review = get_user_episode_review(
        anime_slug, season_name, episode_number,
        user["id"] if user else None, season_index=season_idx,
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
        overall_xp=overall_xp,
        overall_count=overall_count,
        overall_label=overall_label,
        overall_tier=overall_tier,
        overall_negative=overall_negative,
        my_review=my_review,
    )


@app.route("/community/<anime_slug>")
def community(anime_slug):
    entry = anime_database.get(anime_slug)
    if entry is None:
        return "Anime not found", 404
    chat_stats = get_community_chat_stats(anime_slug)
    is_member = False
    user_rank = "D"
    user_xp = 0
    if g.get("user"):
        is_member = is_community_member(anime_slug, g.user["id"])
        user_xp = get_user_xp(g.user["id"])
        user_rank = get_user_rank(g.user["id"])
    return render_template(
        "community.html",
        anime_name=entry.get("title", anime_slug),
        anime_image=entry.get("image", ""),
        anime_slug=anime_slug,
        chat_stats=chat_stats,
        is_member=is_member,
        user_xp=user_xp,
        user_rank=user_rank,
    )


def _build_rating_power():
    """Rating Power legend data for the reviews page: the 10-star verdict
    labels in their three bands (RED / GREY / GREEN), the rank-priced elixir
    schedule with its band mirror, and the vote gates. One vote = the
    VOTER's rank power (D +3 like ... S+ +15, C -2 dislike ... S+ -7):
    green pours it in, red takes it back out, grey stays silent.
    """
    bands = []
    for band_id, title, accent, note in [
        ("negative", "Negative \u00b7 RED", "#f87171",
         "The mirror: a like on RED (1\u20134) TAKES the voter's tier value off the anime/episode \u2014 C \u22125, B \u22127, A \u22129, S \u221211, S+ \u221215. A dislike hands the tier's drain value back (+2 up to +7). A red verdict the elite likes genuinely costs the tank."),
        ("neutral", "Neutral \u00b7 GREY", "#9ca3af",
         "Mid (5/10) never moves XP either way \u2014 it counts in the score, but the tank stays silent."),
        ("positive", "Positive \u00b7 GREEN", "#4ade80",
         "Likes pour in at the voter's rank price: D +3, C +5, B +7, A +9, S +11, S+ +15. Dislikes drain at the same ladder: C \u22122, B \u22123, A \u22124, S \u22125, S+ \u22127."),
    ]:
        ratings = [
            {"n": n, "label": RATING_LABELS[n]}
            for n in sorted(RATING_LABELS)
            if RATING_BANDS[n] == band_id
        ]
        bands.append({"id": band_id, "title": title, "accent": accent, "ratings": ratings, "note": note})
    return {
        "bands": bands,
        # What each rank's vote is worth, both ways. gl/gd = green like /
        # dislike, rl/rd = red like / dislike; None = locked for that rank.
        "tiers": [
            {"rank": "D", "gl": "+3", "gd": None, "rl": None, "rd": None},
            {"rank": "C", "gl": "+5", "gd": "\u22122", "rl": "\u22125", "rd": "+2"},
            {"rank": "B", "gl": "+7", "gd": "\u22123", "rl": "\u22127", "rd": "+3"},
            {"rank": "A", "gl": "+9", "gd": "\u22124", "rl": "\u22129", "rd": "+4"},
            {"rank": "S", "gl": "+11", "gd": "\u22125", "rl": "\u221211", "rd": "+5"},
            {"rank": "S+", "gl": "+15", "gd": "\u22127", "rl": "\u221215", "rd": "+7"},
        ],
        "rate": VOTE_RATE,
        "gate": "Dislikes and likes on RED (1\u20134) reviews require C rank (500 XP). D-rank accounts can still like green/neutral reviews at +3; F accounts can't vote at all.",
    }


@app.route("/reviews")
def reviews_page():
    """Global reviews feed — highest-ranked first, then newest."""
    raw = get_all_reviews(limit=200)
    user = g.get("user")
    review_ids = [r["id"] for r in raw]
    like_counts = get_bulk_review_likes("anime", review_ids)
    user_votes = get_user_anime_review_votes(review_ids, user["id"]) if user else {}
    rank_map = get_bulk_reviewer_ranks([r["user_id"] for r in raw])
    point_map = get_bulk_review_points("anime", review_ids)
    war_effects = get_war_effects("anime", review_ids)
    reviews = []
    for r in raw:
        entry = anime_database.get(r["anime_slug"])
        r["anime_title"] = entry.get("title", r["anime_slug"]) if entry else r["anime_slug"]
        r["anime_image"] = entry.get("image") if entry else None
        counts = like_counts.get(r["id"], {"likes": 0, "dislikes": 0})
        r["likes"] = counts["likes"]
        r["dislikes"] = counts["dislikes"]
        r["band"] = rating_band(r.get("rating"))
        pts = point_map.get(r["id"])
        if pts:
            r["review_xp"] = (pts.get("like_points") or 0) + (pts.get("dislike_points") or 0)
            r["contested"] = pts.get("contested", 0)
        else:
            r["review_xp"] = review_vote_xp(r["likes"], r["dislikes"], r.get("rating"))
            r["contested"] = 0
        # A settled Reply War shifts the review it was fought over once:
        # a decisive Negative winner deducts, a decisive Positive adds.
        _we = war_effects.get(r["id"], {"penalty": 0, "bonus": 0})
        r["war_penalty"] = _we.get("penalty", 0)
        r["war_bonus"] = _we.get("bonus", 0)
        r["review_xp"] = r["review_xp"] - r["war_penalty"] + r["war_bonus"]
        r["review_level"] = review_level_for_xp(r["review_xp"])
        r["xp_lvl"] = review_rank_for_xp(r["review_xp"])
        r["user_vote"] = user_votes.get(r["id"])
        rinfo = rank_map.get(r["user_id"], {"rank": "D", "xp": 0})
        r["rank"] = rinfo["rank"] if isinstance(rinfo, dict) else rinfo
        r["user_xp"] = rinfo["xp"] if isinstance(rinfo, dict) else 0
        r["xp_pct"] = xp_progress(r["user_xp"])[1]
        r["rank_color"] = RANK_COLORS.get(r["rank"], "#9ca3af")
        r["is_trusted"] = r["rank"] in TRUSTED_RANKS
        reviews.append(r)

    # ---- Episode reviews (second tab) ----
    raw_ep = get_all_episode_reviews(limit=200)
    ep_ids = [r["id"] for r in raw_ep]
    ep_like_counts = {}
    try:
        ep_like_counts = get_bulk_review_likes("episode", ep_ids)
    except Exception:
        ep_like_counts = {}
    ep_user_votes = {}
    if user:
        try:
            from review_votes import get_user_review_votes
            ep_user_votes = get_user_review_votes("episode", ep_ids, user["id"])
        except Exception:
            ep_user_votes = {}
    ep_rank_map = get_bulk_reviewer_ranks([r["user_id"] for r in raw_ep])
    ep_war_effects = get_war_effects("episode", ep_ids)
    episode_reviews = []
    for r in raw_ep:
        entry = anime_database.get(r["anime_slug"])
        r["anime_title"] = entry.get("title", r["anime_slug"]) if entry else r["anime_slug"]
        r["anime_image"] = entry.get("image") if entry else None
        r["episode_thumb"] = None
        r["episode_title"] = None
        r["season_name_display"] = None
        r["season_idx"] = 1
        if entry and entry.get("seasons"):
            # Try matching by name first, then by numeric index.
            # AJAX reviews store the season index (e.g. "1") in season_name,
            # while form-POST reviews store the actual name (e.g. "Season 1").
            matched_season = None
            for si, s in enumerate(entry["seasons"]):
                if s.get("name") == r["season_name"]:
                    r["season_idx"] = si + 1
                    matched_season = s
                    break
            if matched_season is None:
                try:
                    idx = int(r["season_name"])
                    if 1 <= idx <= len(entry["seasons"]):
                        r["season_idx"] = idx
                        matched_season = entry["seasons"][idx - 1]
                except (TypeError, ValueError):
                    pass
            if matched_season:
                r["season_name_display"] = matched_season.get("name") or f"Season {r['season_idx']}"
                for ep in matched_season.get("episodes", []):
                    if ep.get("number") == r["episode_number"]:
                        r["episode_thumb"] = ep.get("thumb") or ep.get("image")
                        r["episode_title"] = ep.get("title")
                        break
        if not r.get("season_name_display"):
            r["season_name_display"] = r["season_name"]
        # No real 16:9 episode still: fall back to the anime poster, and
        # flag it so the card renders it as a poster (fill the box) instead
        # of a landscape thumb (which would letterbox it into a black bar).
        r["episode_thumb_is_poster"] = False
        if not r["episode_thumb"]:
            r["episode_thumb_is_poster"] = True
            r["episode_thumb"] = r["anime_image"]
        counts = ep_like_counts.get(r["id"], {"likes": 0, "dislikes": 0})
        r["likes"] = counts["likes"]
        r["dislikes"] = counts["dislikes"]
        r["band"] = rating_band(r.get("rating"))
        try:
            ep_points = get_bulk_review_points("episode", ep_ids)
            epts = ep_points.get(r["id"])
            if epts:
                r["review_xp"] = (epts.get("like_points") or 0) + (epts.get("dislike_points") or 0)
                r["contested"] = epts.get("contested", 0)
            else:
                r["review_xp"] = review_vote_xp(r["likes"], r["dislikes"], r.get("rating"))
                r["contested"] = 0
        except Exception:
            r["review_xp"] = review_vote_xp(r["likes"], r["dislikes"], r.get("rating"))
            r["contested"] = 0
        # A settled Reply War shifts the review it was fought over once:
        # a decisive Negative winner deducts, a decisive Positive adds.
        _ew = ep_war_effects.get(r["id"], {"penalty": 0, "bonus": 0})
        r["war_penalty"] = _ew.get("penalty", 0)
        r["war_bonus"] = _ew.get("bonus", 0)
        r["review_xp"] = r["review_xp"] - r["war_penalty"] + r["war_bonus"]
        r["review_level"] = review_level_for_xp(r["review_xp"])
        r["xp_lvl"] = review_rank_for_xp(r["review_xp"])
        r["user_vote"] = ep_user_votes.get(r["id"])
        rinfo = ep_rank_map.get(r["user_id"], {"rank": "D", "xp": 0})
        r["rank"] = rinfo["rank"] if isinstance(rinfo, dict) else rinfo
        r["user_xp"] = rinfo["xp"] if isinstance(rinfo, dict) else 0
        r["xp_pct"] = xp_progress(r["user_xp"])[1]
        r["rank_color"] = RANK_COLORS.get(r["rank"], "#9ca3af")
        r["is_trusted"] = r["rank"] in TRUSTED_RANKS
        episode_reviews.append(r)

    # Higher-ranked reviewers first, then more XP, then newest.
    RANK_TIER = {"S+": 0, "S": 1, "A": 2, "B": 3, "C": 4, "D": 5, "F": 6}
    reviews.sort(
        key=lambda x: (RANK_TIER.get(x["rank"], 5), -x["user_xp"], -x["id"])
    )
    episode_reviews.sort(
        key=lambda x: (RANK_TIER.get(x["rank"], 5), -x["user_xp"], -x["id"])
    )

    # ---- Top reviewers leaderboard (aggregated from both feeds) ----
    leaderboard = {}
    for r in list(reviews) + list(episode_reviews):
        uid = r.get("user_id")
        if not uid:
            continue
        entry = leaderboard.get(uid)
        if entry is None:
            entry = {
                "user_id": uid,
                "username": r.get("username") or "user",
                "avatar": r.get("avatar"),
                "avatar_color": r.get("avatar_color") or "#374151",
                "rank": r.get("rank") or "D",
                "user_xp": r.get("user_xp") or 0,
                "xp_pct": r.get("xp_pct") or 0,
                "review_count": 0,
            }
            leaderboard[uid] = entry
        entry["review_count"] += 1
        if (r.get("user_xp") or 0) > entry["user_xp"]:
            entry["user_xp"] = r.get("user_xp") or 0
            entry["xp_pct"] = r.get("xp_pct") or 0
            entry["rank"] = r.get("rank") or "D"
    top_reviewers = sorted(
        leaderboard.values(),
        key=lambda x: (RANK_TIER.get(x["rank"], 5), -x["user_xp"], -x["review_count"]),
    )[:12]

    # ---- Top Graded Anime leaderboard (trusted-score engine) ----
    graded = {}
    for r in reviews:
        slug = r.get("anime_slug")
        if not slug:
            continue
        graded.setdefault(slug, {"slug": slug, "title": r.get("anime_title") or slug, "reviews": []})["reviews"].append(r)
    top_graded = []
    for slug, ginfo in graded.items():
        eng = anime_grade_engine(ginfo["reviews"], rank_map)
        if not eng["grade"]:
            continue
        top_graded.append({
            "slug": slug,
            "title": ginfo["title"],
            "grade": eng["grade"],
            "elite": eng["elite"],
            "xp_tier": eng.get("xp_tier"),
            "hidden_gem": eng.get("hidden_gem", False),
            "trusted_label": eng["trusted_label"],
            "audience_label": eng["audience_label"],
            "trusted_count": eng["trusted_count"],
            "audience_count": eng["audience_count"],
            "trusted_xp": eng["trusted_xp"],
            "trusted_xp_label": eng["trusted_xp_label"],
            "img": anime_database.get(slug).get("image") if anime_database.get(slug) else None,
        })
    # Rank by headline grade tier, then by trusted XP (more XP backing a
    # grade = bigger impact, so it wins same-grade ties -- the Vs tiebreaker).
    GRADE_TIER = {"S+": 0, "S": 1, "A": 2, "B": 3, "C": 4, "D": 5}
    top_graded.sort(key=lambda x: (GRADE_TIER.get(x["grade"], 9), -x["trusted_xp"]))
    top_graded = top_graded[:8]

    # Vote point schedule (D -> S+) shown in the legend box on the page,
    # with the D-equivalence ratio (one vote of this rank = N D-rank votes).
    vote_schedule = _build_rating_power()

    _uid = user["id"] if user else None
    replies_map = get_review_replies("anime", review_ids)
    replies_map.update(get_review_replies("episode", ep_ids))
    migrate_replies_to_war()
    war_map = {}
    war_map.update((("anime", rid), w) for rid, w in get_war_entries("anime", review_ids, _uid).items())
    war_map.update((("episode", rid), w) for rid, w in get_war_entries("episode", ep_ids, _uid).items())
    reward_war_leaders()
    settle_war_outcomes()
    user_rank = get_user_rank(_uid) if _uid else None

    return render_template(
        "reviews.html",
        reviews=reviews,
        episode_reviews=episode_reviews,
        top_reviewers=top_reviewers,
        top_graded=top_graded,
        vote_schedule=vote_schedule,
        RATING_LABELS=RATING_LABELS,
        replies=replies_map,
        war=war_map,
        user_rank=user_rank,
        GRADE_ORDER=GRADE_ORDER,
        anime_review_count=len(reviews),
        episode_review_count=len(episode_reviews),
        RANK_TIER=RANK_TIER,
        current_user=user,
    )


@app.route("/anime-reviews/<anime_slug>", methods=["GET"])
def anime_reviews(anime_slug): 
    if anime_slug not in anime_database:
         return jsonify({"success": False, "error": "Anime not found"}), 404
    stats = get_anime_stats(anime_slug)
    user = g.get("user")
    my_review = None
    if user:
        my_review = get_user_review(anime_slug, user["id"])

    # Attach like/dislike counts (and the user's own votes) to each review.
    review_ids = [r["id"] for r in stats["reviews"] if r.get("id")]
    like_counts = get_bulk_review_likes("anime", review_ids)
    user_votes = get_user_anime_review_votes(review_ids, user["id"]) if user else {}
    rank_map = get_bulk_reviewer_ranks([r.get("user_id") for r in stats["reviews"]])
    for r in stats["reviews"]:
        counts = like_counts.get(r.get("id"), {"likes": 0, "dislikes": 0})
        r["likes"] = counts["likes"]
        r["dislikes"] = counts["dislikes"]
        r["user_vote"] = user_votes.get(r.get("id"))
        rinfo = rank_map.get(r.get("user_id"), {"rank": "D", "xp": 0})
        r["rank"] = rinfo["rank"] if isinstance(rinfo, dict) else rinfo
        r["user_xp"] = rinfo["xp"] if isinstance(rinfo, dict) else 0
        r["xp_pct"] = xp_progress(r["user_xp"])[1]
        r["rank_color"] = RANK_COLORS.get(r["rank"], "#9ca3af")

    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
        "reviews": stats["reviews"],
        "my_review": my_review,
        "logged_in": bool(user),
    })


@app.route("/api/anime-review/<int:review_id>/vote", methods=["POST"])
def vote_anime_review(review_id):
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to vote."}), 401
    data = request.get_json(silent=True) or {}
    is_like = data.get("is_like")
    if is_like is None:
        return jsonify({"success": False, "error": "Missing vote type."}), 400
    try:
        user_vote, removed, likes, dislikes = toggle_anime_review_vote(
            user["id"], review_id, bool(is_like)
        )
    except PermissionError as exc:
        return jsonify({"success": False, "error": str(exc)}), 403
    if user_vote is None and not removed:
        return jsonify({"success": False, "error": "Review not found."}), 404
    return jsonify({
        "success": True,
        "likes": likes,
        "dislikes": dislikes,
        "user_vote": user_vote,
        "removed": removed,
    })


# Simple in-memory vote rate limiter (per user + per IP) to stop bot mass-liking.
_VOTE_LOG = defaultdict(list)
_VOTE_LOCK = threading.Lock()


def _vote_rate_hit(key, limit=40, window_seconds=300):
    now = time.time()
    with _VOTE_LOCK:
        lst = _VOTE_LOG[key]
        lst[:] = [t for t in lst if now - t < window_seconds]
        if len(lst) >= limit:
            return True
        lst.append(now)
        return False


@app.route("/war")
def war_index():
    """War Zone — every Reply War across all reviews, hottest first."""
    user = g.get("user")
    wars = get_all_wars()
    anime_ids = [w["review_id"] for w in wars if w["review_type"] == "anime"]
    ep_ids = [w["review_id"] for w in wars if w["review_type"] == "episode"]
    meta = {}
    for r in get_all_reviews(limit=200):
        if r["id"] in anime_ids:
            meta[("anime", r["id"])] = r
    for r in get_all_episode_reviews(limit=200):
        if r["id"] in ep_ids:
            meta[("episode", r["id"])] = r
    author_ids = [meta[k]["user_id"] for k in meta if meta[k].get("user_id")]
    rank_map = get_bulk_reviewer_ranks(list({uid for uid in author_ids if uid}))
    cards = []
    for w in wars:
        r = meta.get((w["review_type"], w["review_id"]))
        if not r:
            continue
        entry = anime_database.get(r["anime_slug"])
        title = entry.get("title", r["anime_slug"]) if entry else r["anime_slug"]
        rinfo = rank_map.get(r["user_id"], {"rank": "D", "xp": 0})
        w["username"] = r["username"]
        w["author_rank"] = rinfo["rank"] if isinstance(rinfo, dict) else rinfo
        w["anime_title"] = title
        w["anime_image"] = entry.get("image") if entry else None
        w["rating"] = r["rating"]
        w["comment"] = (r["comment"] or "")[:240]
        if w["review_type"] == "episode":
            w["episode_label"] = "Ep {}".format(r["episode_number"])
        cards.append(w)
    return render_template(
        "war.html",
        wars=cards,
        current_user=user,
    )


@app.route("/api/war/<review_type>/<int:review_id>/enter", methods=["POST"])
def war_enter(review_type, review_id):
    """Enter the Reply War with a stance — C rank (500 XP) and above, one
    entry per user per review. The replier picks Positive or Negative; the
    crowd votes; the best like-ratio wins."""
    if review_type not in ("anime", "episode"):
        return jsonify({"success": False, "error": "Bad review type."}), 400
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to enter the war."}), 401
    rank = get_user_rank(user["id"])
    if rank not in ("C", "B", "A", "S", "S+"):
        return jsonify({
            "success": False,
            "error": "War entries require C rank (500 XP) — keep getting likes on your reviews to unlock.",
        }), 403
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're posting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    ok, err, entry = add_war_entry(
        user["id"], review_type, review_id, data.get("content"), data.get("stance") or "negative"
    )
    if not ok:
        return jsonify({"success": False, "error": err or "Could not enter the war."}), 400
    return jsonify({"success": True, "entry": entry})


@app.route("/war/<review_type>/<int:review_id>")
def war_detail(review_type, review_id):
    """A single Reply War: the original review on top, every battler below,
    the live leader (or final podium once the 24h timer ends)."""
    if review_type not in ("anime", "episode"):
        abort(404)
    user = g.get("user")
    raw = get_all_episode_reviews(limit=200) if review_type == "episode" else get_all_reviews(limit=200)
    r = next((x for x in raw if x["id"] == review_id), None)
    if not r:
        abort(404)
    _enrich_war_review(review_type, r)
    war = get_war_entries(review_type, [review_id], user["id"] if user else None).get(review_id)
    my_entry = None
    if war and user:
        my_entry = next((e for e in war["entries"] if e["user_id"] == user["id"]), None)
    user_rank = get_user_rank(user["id"]) if user else None
    return render_template(
        "war_detail.html",
        r=r,
        war=war,
        my_entry=my_entry,
        user_rank=user_rank,
        current_user=user,
    )


def _enrich_war_review(review_type, r):
    """Enrich a single review row into the card shape the war page renders
    (title, image, votes, review XP, author rank) — mirror of the /reviews
    feed enrichment for one review."""
    entry = anime_database.get(r["anime_slug"])
    r["anime_title"] = entry.get("title", r["anime_slug"]) if entry else r["anime_slug"]
    r["anime_image"] = entry.get("image") if entry else None
    counts = get_bulk_review_likes(review_type, [r["id"]]).get(r["id"], {"likes": 0, "dislikes": 0})
    r["likes"] = counts["likes"]
    r["dislikes"] = counts["dislikes"]
    pts = get_bulk_review_points(review_type, [r["id"]]).get(r["id"])
    if pts:
        r["review_xp"] = max(0, (pts.get("like_points") or 0) + (pts.get("dislike_points") or 0))
        r["contested"] = pts.get("contested", 0)
    else:
        r["review_xp"] = review_vote_xp(r["likes"], r["dislikes"], r.get("rating"))
        r["contested"] = 0
    r["review_level"] = review_level_for_xp(r["review_xp"])
    rinfo = get_bulk_reviewer_ranks([r["user_id"]]).get(r["user_id"], {"rank": "D", "xp": 0})
    r["rank"] = rinfo["rank"] if isinstance(rinfo, dict) else rinfo
    r["user_xp"] = rinfo["xp"] if isinstance(rinfo, dict) else 0
    r["xp_pct"] = xp_progress(r["user_xp"])[1]
    r["rank_color"] = RANK_COLORS.get(r["rank"], "#9ca3af")
    r["is_trusted"] = r["rank"] in TRUSTED_RANKS
    if review_type == "episode":
        r["episode_thumb"] = None
        r["episode_title"] = None
        r["season_name_display"] = None
        r["season_idx"] = 1
        if entry and entry.get("seasons"):
            matched_season = None
            for si, s in enumerate(entry["seasons"]):
                if s.get("name") == r["season_name"]:
                    r["season_idx"] = si + 1
                    matched_season = s
                    break
            if matched_season is None:
                try:
                    idx = int(r["season_name"])
                    if 1 <= idx <= len(entry["seasons"]):
                        r["season_idx"] = idx
                        matched_season = entry["seasons"][idx - 1]
                except (TypeError, ValueError):
                    pass
            if matched_season:
                r["season_name_display"] = matched_season.get("name") or f"Season {r['season_idx']}"
                for ep in matched_season.get("episodes", []):
                    if ep.get("number") == r["episode_number"]:
                        r["episode_thumb"] = ep.get("thumb") or ep.get("image")
                        r["episode_title"] = ep.get("title")
                        break
        if not r.get("season_name_display"):
            r["season_name_display"] = r["season_name"]
        r["episode_thumb_is_poster"] = False
        if not r["episode_thumb"]:
            r["episode_thumb_is_poster"] = True
            r["episode_thumb"] = r["anime_image"]


@app.route("/api/review/<int:review_id>/remove-reason", methods=["POST"])
def review_remove_reason(review_id):
    """Remove the current user's dislike reason + their dislike vote."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    review_type = data.get("review_type") or "anime"
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM review_reasons WHERE user_id=? AND review_type=? AND review_id=?",
        (user["id"], review_type, review_id),
    )
    cur.execute(
        "DELETE FROM review_likes WHERE user_id=? AND review_type=? AND review_id=? AND is_like=0",
        (user["id"], review_type, review_id),
    )
    conn.commit()
    counts = get_review_likes(review_type, review_id)
    conn.close()
    return jsonify({"success": True, "likes": counts["likes"], "dislikes": counts["dislikes"], "user_vote": None})


@app.route("/api/review/<int:review_id>/war", methods=["POST"])
def review_war_submit(review_id):
    """Enter the Reply War on a review — C rank (500 XP) and above, one
    entry per user per review. The crowd votes; the best like-ratio wins."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to enter the war."}), 401
    rank = get_user_rank(user["id"])
    if rank not in ("C", "B", "A", "S", "S+"):
        return jsonify({
            "success": False,
            "error": "War entries require C rank (500 XP) — keep getting likes on your reviews to unlock.",
        }), 403
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're posting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    review_type = data.get("review_type") or "anime"
    if review_type not in ("anime", "episode"):
        return jsonify({"success": False, "error": "Bad review type."}), 400
    ok, err, entry = add_war_entry(user["id"], review_type, review_id, data.get("content"))
    if not ok:
        return jsonify({"success": False, "error": err or "Could not enter the war."}), 400
    return jsonify({"success": True, "entry": entry})


@app.route("/api/war/<int:entry_id>/vote", methods=["POST"])
def war_vote(entry_id):
    """Vote on a war entry — any logged-in user (the crowd decides)."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to vote."}), 401
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're voting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    is_like = data.get("is_like")
    if is_like is None:
        return jsonify({"success": False, "error": "Missing vote type."}), 400
    try:
        user_vote, likes, dislikes = toggle_war_vote(user["id"], entry_id, bool(is_like))
    except Exception:
        return jsonify({"success": False, "error": "Entry not found."}), 404
    return jsonify({"success": True, "likes": likes, "dislikes": dislikes, "user_vote": user_vote})


@app.route("/api/review/<int:review_id>/reply", methods=["POST"])
def review_reply(review_id):
    """Reply to a review — C rank (500 XP) and above only, so fresh D-rank
    accounts can't use replies to brigade a review section."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to reply."}), 401
    rank = get_user_rank(user["id"])
    if rank not in ("C", "B", "A", "S", "S+"):
        return jsonify({
            "success": False,
            "error": "Replies require C rank (500 XP). Keep reviewing — your next likes will get you there!",
        }), 403
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're replying too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    review_type = data.get("review_type") or "anime"
    if review_type not in ("anime", "episode"):
        return jsonify({"success": False, "error": "Bad review type."}), 400
    ok, err, reply = add_review_reply(user["id"], review_type, review_id, data.get("content"))
    if not ok:
        return jsonify({"success": False, "error": err or "Could not reply."}), 400
    return jsonify({"success": True, "reply": reply})


@app.route("/api/reason/<int:reason_id>/vote", methods=["POST"])
def reason_vote(reason_id):
    """Vote on a dislike-reason (decides if the dislike counts)."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to vote."}), 401
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're voting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    is_like = data.get("is_like")
    if is_like is None:
        return jsonify({"success": False, "error": "Missing vote type."}), 400
    try:
        user_vote, likes, dislikes = toggle_reason_vote(user["id"], reason_id, bool(is_like))
    except Exception:
        return jsonify({"success": False, "error": "Reason not found."}), 404
    return jsonify({"success": True, "likes": likes, "dislikes": dislikes, "user_vote": user_vote})


@app.route("/api/review/<int:review_id>/vote", methods=["POST"])
def vote_review(review_id):
    """Generic like/dislike for anime OR episode reviews (fast, optimistic)."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in to vote."}), 401
    # Rate limit: 40 votes per 5 minutes per user, or 120 per 5 min per IP.
    if _vote_rate_hit("u:" + str(user["id"]), 40, 300) or _vote_rate_hit(
        "ip:" + (request.remote_addr or "?"), 120, 300
    ):
        return jsonify({"success": False, "error": "You're voting too fast. Take a break."}), 429
    data = request.get_json(silent=True) or {}
    review_type = data.get("review_type") or "anime"
    if review_type not in ("anime", "episode"):
        return jsonify({"success": False, "error": "Bad review type."}), 400
    is_like = data.get("is_like")
    if is_like is None:
        return jsonify({"success": False, "error": "Missing vote type."}), 400
    # Prevent voting on your own review
    try:
        if review_type == "episode":
            from database import get_connection as _gc
            _c = _gc(); _cur = _c.cursor()
            _cur.execute("SELECT user_id FROM episode_reviews WHERE id=?", (review_id,))
            _r = _cur.fetchone(); _c.close()
            if _r and _r["user_id"] == user["id"]:
                return jsonify({"success": False, "error": "You can't vote on your own review."}), 400
        elif review_type == "anime":
            from database import get_connection as _gc
            _c = _gc(); _cur = _c.cursor()
            _cur.execute("SELECT user_id FROM reviews WHERE id=?", (review_id,))
            _r = _cur.fetchone(); _c.close()
            if _r and _r["user_id"] == user["id"]:
                return jsonify({"success": False, "error": "You can't vote on your own review."}), 400
    except Exception:
        pass
    try:
        new_is_like, removed = toggle_review_like(
            user["id"], review_type, review_id, bool(is_like)
        )
    except PermissionError as exc:
        return jsonify({"success": False, "error": str(exc)}), 403
    except Exception:
        return jsonify({"success": False, "error": "Review not found."}), 404
    counts = get_review_likes(review_type, review_id)
    user_vote = 1 if (new_is_like and not removed) else (0 if (not new_is_like and not removed) else None)
    return jsonify({
        "success": True,
        "likes": counts["likes"],
        "dislikes": counts["dislikes"],
        "user_vote": user_vote,
        "removed": removed,
    })


@app.route("/rate-anime", methods=["POST"])
def rate_anime():
    user = g.get("user")
    data = request.get_json(silent=True) or {}
    anime_slug = data.get("anime_slug")
    rating = data.get("rating")
    comment = (data.get("comment") or "").strip()[:1000]

    # Only logged-in users can post reviews — no anonymous reviews.
    if not user:
        return jsonify({"success": False, "error": "Please log in to post a review."}), 401
    username = user["username"]
    user_id = user["id"]

    # One review per user per anime.
    if get_user_review(anime_slug, user_id):
        return jsonify({"success": False, "error": "You already reviewed this anime."}), 409

    if not anime_slug or anime_slug not in anime_database:
        return jsonify({"success": False, "error": "Unknown anime"}), 404
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Rating must be a number"}), 400
    if rating < 1 or rating > 10:
        return jsonify({"success": False, "error": "Rating must be between 1 and 10"}), 400

    add_review(anime_slug, username, rating, comment, user_id=user_id)
    stats = get_anime_stats(anime_slug)
    return jsonify({
        "success": True,
        "average": stats["average"],
        "votes": stats["votes"],
        "breakdown": stats["breakdown"],
    })


@app.route("/api/episode-rate", methods=["POST"])
def api_episode_rate():
    """AJAX endpoint for episode rating — instant, no redirect."""
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    anime_slug = data.get("anime_slug")
    season_name = data.get("season_name")
    episode_number = data.get("episode_number")
    rating = data.get("rating")
    comment = (data.get("comment") or "").strip()[:1000]
    if not all([anime_slug, season_name, episode_number]):
        return jsonify({"success": False, "error": "Missing fields."}), 400
    try:
        rating = int(rating)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid rating."}), 400
    if rating < 1 or rating > 10:
        return jsonify({"success": False, "error": "Rating must be between 1 and 10."}), 400
    # The AJAX client sends the season INDEX (e.g. "1"). Resolve it to the
    # display name so stored reviews match the page GET's lookup, but keep
    # the index as season_index so reads hit both spellings.
    season_key, season_index = _season_name_prongs(anime_slug, season_name)
    add_episode_review(
        anime_slug, season_key, int(episode_number),
        user["id"], user["username"], user["avatar_color"],
        rating, comment, season_index=season_index,
    )
    stats = get_episode_stats(anime_slug, season_key, int(episode_number),
                              season_index=season_index)
    return jsonify({
        "success": True,
        "average": stats.get("average", 0),
        "votes": stats.get("votes", 0),
    })


@app.route("/delete-review", methods=["POST"])
def delete_review():
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    review_id = data.get("review_id")
    if not review_id:
        return jsonify({"success": False, "error": "Missing review id."}), 400
    if not delete_user_review(review_id, user["id"]):
        return jsonify({"success": False, "error": "Review not found."}), 404
    return jsonify({"success": True})


@app.route("/delete-episode-review", methods=["POST"])
def delete_episode_review_route():
    user = g.get("user")
    if not user:
        return jsonify({"success": False, "error": "Please log in."}), 401
    data = request.get_json(silent=True) or {}
    anime_slug = data.get("anime_slug")
    season_name = data.get("season_name")
    episode_number = data.get("episode_number")
    if not all([anime_slug, season_name, episode_number]):
        return jsonify({"success": False, "error": "Missing fields."}), 400
    season_key, season_index = _season_name_prongs(anime_slug, season_name)
    if not delete_episode_review(anime_slug, season_key, int(episode_number), user["id"],
                                 season_index=season_index):
        return jsonify({"success": False, "error": "Review not found."}), 404
    return jsonify({"success": True})


def _char_public(entries):
    return [
        {k: v for k, v in e.items() if not k.startswith("_")}
        for e in entries
    ]


@app.route("/healthz")
def healthz():
    """Lightweight health check that does not load the anime catalog."""
    return "ok"


@app.route("/api/user-ranks", methods=["POST"])
def api_user_ranks():
    """Return {xp, rank} for a list of user IDs (used by chat renderers)."""
    data = request.get_json(silent=True) or {}
    ids = data.get("user_ids") or []
    # Sanitise to ints
    clean = []
    for i in ids:
        try:
            clean.append(int(i))
        except (TypeError, ValueError):
            pass
    return jsonify({"ranks": get_all_user_ranks(clean)})


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
    real_members = get_all_community_member_counts()
    return {
        "slug": slug,
        "title": entry.get("title") or slug,
        "image": entry.get("image") or "",
        "rating": entry.get("rating") or "N/A",
        "year": entry.get("release") or "",
        "genre": entry.get("genre") or "",
        "total_episodes": entry.get("total_episodes", 0) or 0,
        "member_count": real_members.get(slug, 0),
        "has_sub": bool(entry.get("subtitles")),
        "has_dub": any(
            str(d).strip().lower() == "english"
            for d in (entry.get("dub") or [])
        ),
        "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
        "live_rating": live_rating,
        "badge_label": "Your Match",
    }


# ==========================================================
# PERSONALIZED HOMEPAGE PICKS
# ==========================================================

RECO_COUNT = 12
RECO_MIX_GENRES = 5
RECO_POOL_PER_GENRE = 120


def _entry_genres(entry):
    return [
        g.strip()
        for g in (entry.get("genre") or "").split(" • ")
        if g.strip() and g.strip().lower() != "anime"
    ]


@functools.lru_cache(maxsize=1)
def _genre_index():
    """genre -> slugs ranked by rating then popularity, built once per process."""
    buckets = {}
    for slug, entry in anime_database.items():
        if not entry.get("image"):
            continue
        try:
            rating = float(entry.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        rank = (rating, entry.get("member_count") or 0)
        for genre in _entry_genres(entry):
            buckets.setdefault(genre, []).append((rank, slug))
    return {
        genre: [slug for _, slug in sorted(items, reverse=True)]
        for genre, items in buckets.items()
    }


def _taste_genres(seeds):
    """Rank genres by how much the user's history/lists lean on them.

    A saved list entry weighs double a passive view, recent activity weighs
    more than old activity, and each title spreads its weight across its own
    genres so an 8-genre show doesn't drown out a focused one.
    """
    weights = {}
    for position, seed in enumerate(seeds):
        entry = anime_database.get(seed["slug"])
        if entry is None:
            continue
        genres = _entry_genres(entry)
        if not genres:
            continue
        weight = (2.0 if seed["saved"] else 1.0) / (1.0 + position / 8.0)
        share = weight / len(genres)
        for genre in genres:
            weights[genre] = weights.get(genre, 0.0) + share
    return sorted(weights.items(), key=lambda kv: (-kv[1], kv[0]))


def _mix_by_genre(seeds, ranked_genres, count, rotation=0):
    """Round-robin across the user's top genres so the row is a real mix.

    Taking the single best-scoring pool would fill every slot from whichever
    genre the user watches most; one pick per genre per pass keeps all of
    their tastes represented. `rotation` offsets each genre's pool so the row
    refreshes over time instead of showing the same twelve titles forever.
    """
    index = _genre_index()
    excluded = {_FRANCHISE_RE.sub("", seed["slug"]) for seed in seeds}
    # _FRANCHISE_RE only strips numbered/season suffixes, so a subtitled
    # sequel ("code-geass" -> "code-geass-lelouch-...") survives it.
    prefixes = tuple(base + "-" for base in excluded)
    queues = []
    for genre, _ in ranked_genres:
        pool = (index.get(genre) or [])[:RECO_POOL_PER_GENRE]
        if not pool:
            continue
        offset = rotation % len(pool)
        queues.append(iter(pool[offset:] + pool[:offset]))

    picks, picked = [], set()
    while queues and len(picks) < count:
        progressed = False
        for queue in queues:
            if len(picks) >= count:
                break
            for slug in queue:
                base = _FRANCHISE_RE.sub("", slug)
                if base in excluded or base in picked:
                    continue
                if prefixes and base.startswith(prefixes):
                    continue
                picked.add(base)
                picks.append(slug)
                progressed = True
                break
        if not progressed:
            break
    return picks


def _reco_cards(slugs, badge):
    """Shape slugs for the shared _anime_card.html partial (no per-slug query)."""
    all_stats = get_all_anime_stats()
    real_members = get_all_community_member_counts()
    cards = []
    for slug in slugs:
        entry = anime_database.get(slug)
        if entry is None:
            continue
        stats = all_stats.get(slug) or {"votes": 0, "average": 0}
        cards.append({
            "slug": slug,
            "title": entry.get("title") or slug,
            "image": entry.get("image") or "",
            "genre": entry.get("genre") or "",
            "rating": entry.get("rating") or "N/A",
            "live_rating": stats["average"] if stats["votes"] > 0 else entry.get("rating", "N/A"),
            "member_count": real_members.get(slug, 0),
            "total_episodes": entry.get("total_episodes", 0) or 0,
            "has_sub": bool(entry.get("subtitles")),
            "has_dub": any(
                str(d).strip().lower() == "english"
                for d in (entry.get("dub") or [])
            ),
            "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
            "badge_label": badge,
        })
    return cards


def _home_picks():
    """(cards, genres) for the logged-in user's homepage row.

    Returns empty lists for guests, brand-new accounts with no activity yet,
    and on any data error — the homepage then renders exactly as before.
    """
    user = g.get("user")
    if user is None:
        return [], []
    try:
        seeds = get_taste_slugs(user["id"])
    except Exception:
        return [], []
    if not seeds:
        return [], []

    ranked_genres = _taste_genres(seeds)[:RECO_MIX_GENRES]
    if not ranked_genres:
        return [], []

    rotation = int(time.time() // 3600) + user["id"]
    slugs = _mix_by_genre(seeds, ranked_genres, RECO_COUNT, rotation=rotation)
    if not slugs:
        return [], []
    return _reco_cards(slugs, "FOR YOU"), [genre for genre, _ in ranked_genres]


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

    # Recalculate all user XP on startup (fixes stale values after deploy)
    def _startup_recalc():
        try:
            conn = get_connection()
            users = conn.execute("SELECT id FROM users").fetchall()
            conn.close()
            for u in users:
                try:
                    recalculate_user_xp(u["id"])
                except Exception:
                    pass
            if users:
                print(f"[startup] Recalculated XP for {len(users)} users")
        except Exception as e:
            print(f"[startup] XP recalc skipped: {e}")

    threading.Thread(target=_startup_recalc, daemon=True).start()

    threading.Thread(target=_schedule_loop, daemon=True).start()

    threading.Thread(target=_full_enrich_loop, daemon=True).start()

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=True,
        use_reloader=False,
    )