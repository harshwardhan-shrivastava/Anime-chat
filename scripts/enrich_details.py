#!/usr/bin/env python3
"""
Enrich anime_data.py with per-title viewing details -- SUB, DUB, EPISODES and
ARCS (watch order) -- in resumable chunks.

Why chunks: the catalog has ~14k titles and AniList's rate limit is 90 req/min,
so the full enrichment is run in parts. Re-running the same command resumes
where the previous run stopped and then moves on to the NEXT batch:

    python3 scripts/enrich_details.py --details 5000    # first 5000 titles
    python3 scripts/enrich_details.py --details 5000    # next 5000, etc.

Each run:
  1) Picks the top N titles (by member count) that don't have sub/dub/seasons.
  2) Ensures real streaming-platform + episode data is cached for them in
     anime_streaming.json (fetches anything missing or thinner than 24
     episodes, in batches of 50, saved per batch so a killed run resumes
     cleanly).
  3) Derives and applies:
       - streaming   -> legal platforms that carry the show
       - dub         -> ["English", "Japanese"] when a dub-carrying platform
                        (Crunchyroll / Netflix / Hulu / HIDIVE / ...) has it
       - subtitles   -> the standard multilingual subtitle set offered by the
                        detected platforms
       - seasons     -> episode breakdown; real episode titles are used where
                        AniList provides them (streamingEpisodes), the rest
                        are numbered placeholders
       - watch_order -> the arc / season list ("Season 1", "Season 2", ...)
  4) Rewrites anime_data.py, preserving every other field untouched.

The language lists are a reasonable generalization from the detected platforms
(the same approach scripts/fetch_anime_catalog.py already takes for its
Sub/Dub platform flags). Hand-curated entries are never overwritten.

No API key needed.
"""

import argparse
import json
import os
import re
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.common import (  # noqa: E402
    STREAM_SITES,
    load_json as _load_json,
    save_json,
)

API_URL = "https://graphql.anilist.co"
STREAMING_CACHE = "anime_streaming.json"
# anime_data.py is now a thin loader over this JSON file (a giant Python
# literal was OOM-killing the app in low-memory containers).
OUT_FILE = "anime_data.json"

PAGE_SIZE = 50
SLEEP = 1.0            # seconds between API calls (AniList limit is 90/min)
EPISODE_CAP = 48       # keep at most this many real episode titles per title
# NOTE: heavy titles (1000+ streaming episodes) can make large batches hang,
# so by default we only fetch ids with NO cached data at all. Pass --upgrade
# to also re-fetch thin caches (fewer than UPGRADE_THRESHOLD real titles).
UPGRADE_THRESHOLD = 24

# Typical audio languages available when a dub-carrying platform has the title.
DUB_LANGS = ["English", "Japanese"]
# Typical subtitle set offered by the major international streaming services.
SUB_LANGS = ["English", "Japanese", "Spanish", "Portuguese", "French", "German"]
REGIONS = ["Availability varies by region"]

STREAMING_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      streamingEpisodes {
        title
        url
        site
      }
    }
  }
}
"""


def load_json(path):
    """Cache loader used by the whole script family: missing file -> {}."""
    return _load_json(path, {})


def fetch_streaming_batch(ids):
    """Fetch streamingEpisodes for a batch of media ids (max ~50)."""
    resp = requests.post(
        API_URL,
        json={"query": STREAMING_QUERY, "variables": {"ids": ids}},
        timeout=45,
    )
    if resp.status_code == 429:
        raise RuntimeError("rate limited")
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error: {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def platform_info(site, url):
    """Map a streaming episode to (platform_name, has_dub)."""
    host = (site or url or "").lower()
    for key, value in STREAM_SITES.items():
        if key in host:
            return value
    return (None, False)


def parse_episodes(episodes):
    """Map real episode number -> title from AniList streamingEpisodes."""
    parsed = {}
    for ep in episodes:
        title = (ep.get("title") or "").strip()
        m = re.match(r"^(?:ep(?:isode)?)\s+(\d+)\s*[-–—:.]?\s*(.*)$",
                     title, re.IGNORECASE)
        if not m:
            continue
        n = int(m.group(1))
        label = (m.group(2) or "").strip() or f"Episode {n}"
        parsed.setdefault(n, label)
    return parsed


def platform_list(episodes):
    """Unique legal platforms with Sub/Dub status from cached episodes."""
    services = {}
    for ep in episodes:
        name, has_dub = platform_info(ep.get("site"), ep.get("url"))
        if not name or name in services:
            continue
        services[name] = {
            "name": name,
            "status": "Sub • Dub" if has_dub else "Sub",
            "regions": list(REGIONS),
        }
    return list(services.values())


def has_dub_platform(episodes):
    for ep in episodes:
        name, has_dub = platform_info(ep.get("site"), ep.get("url"))
        if name and has_dub:
            return True
    return False


def build_seasons(entry, parsed):
    """Episode breakdown from the real episode count + real titles."""
    total = entry.get("total_episodes") or 0
    max_real = max(parsed) if parsed else 0
    count = max(total, max_real)
    if count <= 0:
        return []

    is_movie = "Movie" in (entry.get("type") or "")
    if is_movie:
        groups = [(1, count, "Movie")]
    elif count <= 26:
        groups = [(1, count, "Season 1")]
    else:
        groups = []
        start, si = 1, 1
        while start <= count:
            end = min(start + 25, count)
            groups.append((start, end, f"Season {si}"))
            start = end + 1
            si += 1

    seasons = []
    for start, end, name in groups:
        episodes = []
        for n in range(start, end + 1):
            item = {"number": n}
            if n in parsed:  # only keep real titles; placeholders are derived
                item["title"] = parsed[n]
            episodes.append(item)
        seasons.append({"name": name, "episodes": episodes})
    return seasons


def ensure_streaming(ids, cache, upgrade=False):
    """Fetch (or refresh) streaming data for ids, resumable per batch."""
    pending = []
    for i in ids:
        key = str(i)
        if key not in cache:
            pending.append(i)
        elif upgrade and len(cache.get(key) or []) < UPGRADE_THRESHOLD:
            pending.append(i)
    print(f"  {len(ids)} ids in window, {len(pending)} need fetching "
          f"({len(ids) - len(pending)} already cached)", flush=True)

    for i in range(0, len(pending), PAGE_SIZE):
        batch = pending[i:i + PAGE_SIZE]
        for attempt in range(3):
            try:
                media = fetch_streaming_batch(batch)
                for m in media:
                    cache[str(m["id"])] = (m.get("streamingEpisodes") or [])[:EPISODE_CAP]
                save_json(STREAMING_CACHE, cache)
                print(f"  batch {i // PAGE_SIZE + 1}/{len(pending) // PAGE_SIZE + 1}: "
                      f"{len(media)} titles enriched", flush=True)
                break
            except Exception as exc:
                print(f"  batch {i // PAGE_SIZE + 1} attempt {attempt + 1} failed: {exc}",
                      flush=True)
                time.sleep(5 + attempt * 5)
        time.sleep(SLEEP)
    return cache


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--details", type=int, metavar="N",
                        help="enrich the top N un-enriched titles (resumable)")
    parser.add_argument("--skip-fetch", action="store_true",
                        help="apply details from the existing cache without "
                             "hitting the API")
    parser.add_argument("--upgrade", action="store_true",
                        help="also re-fetch cached titles with fewer than "
                             "24 real episode titles (slow for heavy shows)")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import anime_data  # noqa: E402

    data = anime_data.anime_database
    print(f"Catalog: {len(data)} titles", flush=True)

    window = [
        e for e in data.values()
        if not (e.get("seasons") or e.get("dub") or e.get("subtitles"))
    ]
    window.sort(key=lambda e: (e.get("member_count") or 0), reverse=True)
    if args.details:
        window = window[:args.details]
    print(f"Un-enriched window: {len(window)} titles", flush=True)

    cache = load_json(STREAMING_CACHE)

    ids = [e["anilist_id"] for e in window if e.get("anilist_id")]
    if not args.skip_fetch:
        ensure_streaming(ids, cache, upgrade=args.upgrade)
    else:
        print("  --skip-fetch: using existing cache only", flush=True)

    # Apply derived details to every window entry.
    for entry in window:
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
            entry["watch_order"] = [
                s["name"] for s in entry.get("seasons") or []
            ]

    # Rewrite anime_data.json (anime_data.py just loads it), preserving every
    # other field untouched. Compact separators keep the file small.
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    enriched = sum(
        1 for e in data.values() if (e.get("dub") and e.get("seasons"))
    )
    print(f"WROTE {OUT_FILE} with {len(data)} total entries.", flush=True)
    print(f"Entries with dub+seasons now: {enriched}", flush=True)


if __name__ == "__main__":
    main()
