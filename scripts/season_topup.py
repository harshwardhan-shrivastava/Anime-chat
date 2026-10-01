#!/usr/bin/env python3
"""Auto-onboard brand-new season anime into the catalog.

The 15-minute enrichment loop (scripts/run_enrichment.py) only ever *updates*
titles that are ALREADY in anime_data.json -- its plan_todo() builds the todo
list from Ongoing/Upcoming entries that exist on disk. So a brand-new season
premiere never gets a card until someone remembers to run
scripts/fetch_anime_catalog.py by hand.

This module closes that gap. On every pipeline run it:

  1. asks AniList for the previous + current + next season's
     RELEASING / NOT_YET_RELEASED titles (new shows, new cours, new seasons --
     FINISHED shows are skipped, the catalog already holds 14k of those);
  2. inserts the ones the catalog is missing, with the same field mapping as
     scripts/fetch_anime_catalog.py (poster, banner, synopsis, studio, genres,
     status, source, type, anilist_id) so nothing downstream changes -- plus
     start_year/month/day from the AniList start date, which build_entry()
     omits and which the app needs for the "EXP MON YEAR" badge and the
     Upcoming ordering;
  3. runs a bounded pass of the existing per-title enrichment for just those
     new slugs -- Sub/Dub + episode list (enrich_details), real per-country
     streaming (enrich_streaming / JustWatch) and TVmaze/Kitsu episode stills
     (enrich_ep_thumbnails).

Dedupe is by anilist_id first, then by normalised title, so a new season of a
show already in the catalog is never duplicated.

It is deliberately cheap when there is nothing new: a handful of AniList
season queries + one dedupe pass. The heavier per-title work only runs for
titles that were actually added, and is capped per run so a big backlog
(e.g. the first run) is filled over several runs instead of hammering the
APIs. JustWatch in particular 403-blocks after a burst, so it is capped lower.

Reusing the existing helpers keeps the JSON schema identical to the rest of
the pipeline; the run_enrichment steps that follow then pick the new cards up
for their first airing refresh in the very same run.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DATA_FILE = os.path.join(ROOT, "anime_data.json")

API = "https://graphql.anilist.co"

# Caps that keep a single 15-minute run bounded. A season change only adds a
# handful of titles, so these mostly protect the FIRST run (or a backlog after
# downtime) from doing thousands of requests in one tick.
MAX_NEW = 40        # insert at most this many new titles per run
MAX_JUSTWATCH = 30  # ...and look up at most this many on JustWatch
PAGES_PER_SEASON = 2  # 2 x 50 = up to 100 titles per season, newest first
SLEEP = 0.8         # seconds between AniList calls (limit is 90 req/min)

SEASON_BY_MONTH = {
    1: "WINTER", 2: "WINTER", 3: "WINTER",
    4: "SPRING", 5: "SPRING", 6: "SPRING",
    7: "SUMMER", 8: "SUMMER", 9: "SUMMER",
    10: "FALL", 11: "FALL", 12: "FALL",
}
SEASON_ORDER = ["WINTER", "SPRING", "SUMMER", "FALL"]

# Formats that make a real, watchable card. MUSIC is audio-only uploads and
# would only add noise to a discussion site.
WANT_FORMATS = {"TV", "TV_SHORT", "ONA", "OVA", "MOVIE", "SPECIAL"}

# The seasons we scan every run, in priority order: current (brand-new
# premieres), next (upcoming shows, so a Fall card exists before episode 1
# airs), then previous (a just-started show can still be listed under the
# season it premiered in -- e.g. late-September Summer titles).
SEASON_WINDOW = (0, 1, -1)

SEASON_QUERY = """
query ($page: Int, $perPage: Int, $season: MediaSeason, $year: Int) {
  Page(page: $page, perPage: $perPage) {
    media(season: $season, seasonYear: $year, sort: POPULARITY_DESC,
          type: ANIME, isAdult: false) {
      id
      status
      format
      episodes
      seasonYear
      startDate { year month day }
      title { romaji english }
      coverImage { extraLarge large }
      bannerImage
      description
      averageScore
      duration
      genres
      studios(isMain: true) { nodes { name } }
      source
      favourites
    }
  }
}
"""


def _load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_json(path, obj):
    """Atomic write (dump to a temp file then rename) so a killed run can
    never leave a truncated catalog behind -- same pattern as the other
    enrichment scripts."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def current_season(now=None):
    """(season, year) for the given moment (UTC by default)."""
    now = now or datetime.now(timezone.utc)
    return SEASON_BY_MONTH[now.month], now.year


def surrounding_seasons(now=None):
    """[(season, year), ...] for the previous, current and next season."""
    season, year = current_season(now)
    i = SEASON_ORDER.index(season)
    out = []
    for delta in SEASON_WINDOW:
        j = i + delta
        y = year
        while j < 0:
            j += len(SEASON_ORDER)
            y -= 1
        while j >= len(SEASON_ORDER):
            j -= len(SEASON_ORDER)
            y += 1
        out.append((SEASON_ORDER[j], y))
    return out


def _query_season(season, year, page):
    try:
        r = requests.post(
            API,
            json={"query": SEASON_QUERY,
                  "variables": {"page": page, "perPage": 50,
                                "season": season, "year": year}},
            timeout=30,
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("Page", {}).get("media", [])
        print(f"[topup] AniList HTTP {r.status_code} for {season} {year} p{page}",
              flush=True)
    except Exception as exc:
        print(f"[topup] AniList request failed for {season} {year} p{page}: {exc}",
              flush=True)
    return []


def collect_candidates(seasons):
    """Every currently-airing/upcoming title from the given seasons, most
    popular first, de-duplicated by AniList id."""
    seen = set()
    out = []
    for season, year in seasons:
        for page in range(1, PAGES_PER_SEASON + 1):
            media = _query_season(season, year, page)
            time.sleep(SLEEP)
            if not media:
                break
            for m in media:
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                seen.add(mid)
                if m.get("format") not in WANT_FORMATS:
                    continue
                if m.get("status") not in ("RELEASING", "NOT_YET_RELEASED"):
                    continue
                out.append(m)
            if len(media) < 50:
                break
    return out


def _unique_slug(base, year, aid, used):
    """A slug that doesn't collide with an existing card. Rare collisions
    (same title, different show) get the year / AniList id appended."""
    candidates = [base]
    if year:
        candidates.append(f"{base}-{year}")
    candidates.append(f"{base}-{aid}")
    for cand in candidates:
        if cand and cand not in used:
            return cand
    return None


def _apply_start_date(entry, media):
    """Persist the AniList start date in the shape the app reads.

    build_entry() only writes `release` (the season year), so an inserted card
    would show a bland "UPCOMING" badge and sort to the bottom of the Upcoming
    section until an airing refresh happened to reach it. Writing
    start_year/month/day here -- the same fields
    scripts/enrich_airing.apply_airing() and app.py use -- makes a brand-new
    card correct on its very first tick."""
    sd = media.get("startDate") or {}
    for key in ("year", "month", "day"):
        if sd.get(key):
            entry[f"start_{key}"] = sd[key]
    if not entry.get("release") and sd.get("year"):
        entry["release"] = str(sd["year"])
    return entry


def insert_new_titles(data, media, max_new=MAX_NEW):
    """Add catalog entries for media the catalog doesn't have yet.

    Matches on anilist_id first, then on the normalised title (so a new season
    of a show already in the catalog is never duplicated). Returns the list of
    inserted entries."""
    from scripts.fetch_anime_catalog import build_entry, norm, slugify

    existing_ids = {e.get("anilist_id") for e in data.values() if e.get("anilist_id")}
    used_slugs = set(data.keys())
    titles_norm = {norm(e.get("title")) for e in data.values()}

    added = []
    for m in media:
        if len(added) >= max_new:
            break
        aid = m.get("id")
        if not aid or aid in existing_ids:
            continue
        title = ((m.get("title") or {}).get("english")
                 or (m.get("title") or {}).get("romaji") or "")
        if not title:
            continue
        nt = norm(title)
        if not nt or nt in titles_norm:
            continue
        base = slugify(title) or f"anime-{aid}"
        slug = _unique_slug(base, m.get("seasonYear") or "", aid, used_slugs)
        if not slug:
            continue

        entry = build_entry(m)
        entry["slug"] = slug
        _apply_start_date(entry, m)

        data[slug] = entry
        existing_ids.add(aid)
        titles_norm.add(nt)
        used_slugs.add(slug)
        added.append(entry)
    return added


def _attach_recommendations(data, added, n=6):
    """Same shape as fetch_anime_catalog: each new card gets a random sample
    of other posters so its page has a 'you might like' rail."""
    pool = [e for e in data.values() if e.get("image")]
    random.shuffle(pool)
    added_slugs = {e["slug"] for e in added}
    for entry in added:
        picks = []
        for cand in pool:
            if len(picks) >= n:
                break
            if cand.get("slug") in added_slugs or cand.get("slug") == entry["slug"]:
                continue
            picks.append({"slug": cand["slug"], "title": cand.get("title"),
                          "image": cand.get("image")})
        entry["recommendations"] = picks


def _apply_ep_thumbs(entry, thumbs):
    """Attach a {season-index:number -> url} thumb map to a card's episodes.
    Mirrors enrich_ep_thumbnails.apply_thumbs (never touches TBC episodes)."""
    filled = 0
    for si, season in enumerate(entry.get("seasons") or [], start=1):
        for ep in season.get("episodes") or []:
            if ep.get("released") is False:
                continue
            url = thumbs.get(f"{si}:{ep.get('number')}")
            if url:
                ep["thumb"] = url
                filled += 1
    return filled


def _release_year(entry):
    m = re.search(r"(\d{4})", str(entry.get("release") or ""))
    return int(m.group(1)) if m else None


def _enrich_details(added):
    """Sub/Dub + episode list from AniList streaming episodes (the same cache
    and helpers scripts/enrich_details.py uses)."""
    from scripts.enrich_details import (
        ensure_streaming, platform_list, parse_episodes, build_seasons,
        has_dub_platform, DUB_LANGS, SUB_LANGS,
    )

    cache_path = os.path.join(ROOT, "anime_streaming.json")
    cache = _load_json(cache_path) or {}
    ids = [e["anilist_id"] for e in added if e.get("anilist_id")]
    ensure_streaming(ids, cache)
    _save_json(cache_path, cache)

    for entry in added:
        aid = entry.get("anilist_id")
        episodes = cache.get(str(aid), []) if aid else []
        if not entry.get("streaming"):
            entry["streaming"] = platform_list(episodes)
        if not entry.get("dub") and has_dub_platform(episodes):
            entry["dub"] = list(DUB_LANGS)
        if not entry.get("subtitles") and entry.get("streaming"):
            entry["subtitles"] = list(SUB_LANGS)
        if not entry.get("seasons"):
            entry["seasons"] = build_seasons(entry, parse_episodes(episodes))
        if not entry.get("watch_order"):
            entry["watch_order"] = [s["name"] for s in entry.get("seasons") or []]


def _enrich_streaming(added):
    """Real per-country US/JP offers from JustWatch (the same search used by
    scripts/enrich_streaming.py). Capped: JustWatch 403-blocks after a burst."""
    from scripts.enrich_streaming import search_title, node_to_entry

    for entry in added[:MAX_JUSTWATCH]:
        title = entry.get("title") or entry["slug"]
        want_show = "Movie" not in (entry.get("type") or "")
        try:
            node = search_title(title, want_show)
        except Exception as exc:
            print(f"[topup] JustWatch {title}: {exc}", flush=True)
            node = None
        if node:
            info = node_to_entry(node)
            if info:
                entry["streaming"] = info["streaming"]
                entry["dub"] = info["dub"]
                entry["subtitles"] = info["subtitles"]
                entry["availability"] = info.get("availability", {})
        time.sleep(1.2)


def _enrich_thumbs(added):
    """TVmaze (with Kitsu fallback) episode stills for the new cards."""
    from scripts.enrich_ep_thumbnails import fetch_thumbs_for

    total = 0
    for entry in added:
        title = entry.get("title") or entry["slug"]
        try:
            thumbs = fetch_thumbs_for(entry["slug"], title, _release_year(entry))
        except Exception as exc:
            print(f"[topup] thumbs {title}: {exc}", flush=True)
            continue
        if isinstance(thumbs, dict) and "__error__" not in thumbs:
            total += _apply_ep_thumbs(entry, thumbs)
        time.sleep(0.55)
    return total


def enrich_new(added):
    """Run the bounded per-title enrichment for freshly added cards. Each step
    is independent and best-effort: a failure in one never loses the others
    (or the insert itself)."""
    if not added:
        return
    print(f"[topup] enriching {len(added)} new cards", flush=True)

    # Keep every cache file the other scripts write next to the catalog,
    # regardless of the caller's working directory.
    try:
        os.chdir(ROOT)
    except Exception:
        pass

    try:
        _enrich_details(added)
    except Exception as exc:
        print(f"[topup] AniList details step failed: {exc}", flush=True)
    try:
        _enrich_streaming(added)
    except Exception as exc:
        print(f"[topup] JustWatch step failed: {exc}", flush=True)
    try:
        thumbs = _enrich_thumbs(added)
        print(f"[topup] episode thumbnails set: {thumbs}", flush=True)
    except Exception as exc:
        print(f"[topup] thumbnail step failed: {exc}", flush=True)


def season_topup(max_new=MAX_NEW, now=None):
    """Insert any missing current/next-season titles and enrich them.

    Returns the list of slugs that were added. Cheap (a couple of AniList
    calls) when the catalog is already up to date."""
    data = _load_json(DATA_FILE)
    if not data:
        print("[topup] catalog not found - skipping", flush=True)
        return []

    seasons = surrounding_seasons(now)
    print("[topup] scanning " + ", ".join(f"{s} {y}" for s, y in seasons),
          flush=True)
    media = collect_candidates(seasons)
    print(f"[topup] {len(media)} airing/upcoming candidates found", flush=True)

    added = insert_new_titles(data, media, max_new=max_new)
    if not added:
        print("[topup] no new titles to add", flush=True)
        return []

    _attach_recommendations(data, added)
    print(f"[topup] +{len(added)} new titles: "
          + ", ".join(e["slug"] for e in added), flush=True)

    enrich_new(added)
    _save_json(DATA_FILE, data)
    print(f"[topup] wrote {len(data)} total entries", flush=True)
    return [e["slug"] for e in added]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=MAX_NEW,
                    help="max titles to insert this run")
    args = ap.parse_args()
    slugs = season_topup(max_new=args.max_new)
    print(f"season_topup: added {len(slugs)} titles", flush=True)


if __name__ == "__main__":
    main()
