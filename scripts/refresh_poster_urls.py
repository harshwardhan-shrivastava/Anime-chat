#!/usr/bin/env python3
"""Refresh stale AniList cover URLs with fresh, working HD URLs.

AniList occasionally re-uploads cover art. The old URL keeps working for a
while and then starts 404ing — but only for the /cover/large/ (and
/cover/extraLarge/) flavors; /cover/medium/ keeps resolving. The site serves
the large flavor at render time, so shows with stale URLs end up falling back
to the low-res medium poster.

This script asks the AniList API for each entry's *current* coverImage and
stores the best available flavor (large, else extraLarge, else medium) in a
resume cache. `--apply` merges the cache into anime_data.json.

Usage (same resumable pattern as the other enrich scripts — kill/limit a run
and re-run to continue, progress is saved every batch):
    python3 scripts/refresh_poster_urls.py            # fetch in batches
    python3 scripts/refresh_poster_urls.py --apply    # merge into anime_data.json
"""

import argparse
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.common import read_json as _read_json, save_json  # noqa: E402

DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_poster_refresh.json")

API = "https://graphql.anilist.co"
QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      coverImage { extraLarge large medium }
    }
  }
}
"""


def load_json(path):
    """Unreadable/corrupt cache reads as empty so a run can just continue."""
    return _read_json(path, {})


def best_cover(cover):
    """Return the best (HD) cover URL from an AniList API coverImage object.

    AniList's field names are misleading: the `extraLarge` field holds the
    /cover/large/ (true ~460px HD) URL, the `large` field holds the
    /cover/medium/ URL, and `medium` holds /cover/small/. We want the true
    large flavor, so prefer `extraLarge` first."""
    for key in ("extraLarge", "large", "medium"):
        url = (cover or {}).get(key)
        if url:
            return url
    return None


def fetch_batch(session, ids):
    for attempt in range(3):
        try:
            r = session.post(
                API,
                json={"query": QUERY, "variables": {"ids": ids}},
                timeout=25,
            )
            if r.status_code == 200:
                media = r.json().get("data", {}).get("Page", {}).get("media", [])
                return {m["id"]: best_cover(m.get("coverImage")) for m in media}
            if r.status_code == 429:
                time.sleep(5)
                continue
        except Exception:
            pass
        time.sleep(2)
    return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="merge the cached fresh URLs into anime_data.json")
    args = parser.parse_args()

    data = load_json(DATA_FILE)

    if args.apply:
        cache = load_json(CACHE_FILE)
        changed = 0
        banners = 0
        for slug, entry in data.items():
            fresh = cache.get(str(slug))
            if not fresh:
                continue
            old_image = entry.get("image")
            if old_image != fresh:
                entry["image"] = fresh
                changed += 1
            # Cover-type banners are just the poster reused as the hero
            # background — keep them in sync so the background stays HD too.
            # Real wide banners use the /banner/ folder and pass through.
            banner = entry.get("banner") or ""
            if isinstance(banner, str) and "/cover/" in banner and banner != fresh:
                entry["banner"] = fresh
                banners += 1
        if changed or banners:
            save_json(DATA_FILE, data)
        print(f"[apply] updated {changed} poster URLs (+{banners} cover-type banners) in anime_data.json", flush=True)
        return

    cache = load_json(CACHE_FILE)
    todo = [
        (slug, entry.get("anilist_id"))
        for slug, entry in data.items()
        if entry.get("anilist_id") and entry.get("image")
        and str(slug) not in cache
    ]
    print(f"[fetch] {len(todo)} posters to refresh ({len(cache)} already cached)", flush=True)

    session = requests.Session()
    session.headers["Accept"] = "application/json"
    done = 0
    for i in range(0, len(todo), 50):
        batch = todo[i:i + 50]
        ids = [aid for _, aid in batch if aid]
        if not ids:
            continue
        fresh = fetch_batch(session, ids)
        for slug, aid in batch:
            url = fresh.get(aid)
            if url:
                cache[str(slug)] = url
        save_json(CACHE_FILE, cache)
        done += len(batch)
        print(f"[fetch] {done}/{len(todo)} (batch {i // 50 + 1})", flush=True)
        time.sleep(0.7)

    print(f"[fetch] done — {len(cache)} fresh URLs cached in {os.path.basename(CACHE_FILE)}", flush=True)
    print("Run with --apply to merge them into anime_data.json", flush=True)


if __name__ == "__main__":
    main()
