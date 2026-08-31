#!/usr/bin/env python3
"""Fetch AniList bannerImage (1920px HD) for every catalog title and apply
it as the hero "banner" on anime detail pages.

Resumable: progress is saved to anime_banners.json after every batch, so a
killed run just needs to be re-run with the same --limit to continue.

Usage:
    python3 scripts/fetch_banners.py --limit 4200     # fetch the next 4200
    python3 scripts/fetch_banners.py --limit 4200     # resume
    python3 scripts/fetch_banners.py --apply          # merge into anime_data.json
"""
import argparse
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_banners.json")
API_URL = "https://graphql.anilist.co"
PAGE_SIZE = 50
SLEEP = 0.8  # seconds between requests (AniList limit is 90/min)


def load(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def fetch_batch(ids):
    query = """query ($ids: [Int]) {
      Page(page: 1, perPage: 50) {
        media(id_in: $ids, type: ANIME) { id bannerImage }
      }
    }"""
    while True:
        resp = requests.post(API_URL, json={"query": query, "variables": {"ids": ids}}, timeout=45)
        if resp.status_code == 429:
            print("  rate limited, sleeping 20s...", flush=True)
            time.sleep(20)
            continue
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"AniList error: {data['errors']}")
        return data.get("data", {}).get("Page", {}).get("media", [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=2000, help="ids to try this run")
    ap.add_argument("--apply", action="store_true", help="merge cached banners into anime_data.json")
    args = ap.parse_args()

    data = load(DATA_FILE)
    cache = load(CACHE_FILE)

    if args.apply:
        applied = 0
        missing = 0
        for slug, entry in data.items():
            aid = entry.get("anilist_id")
            url = cache.get(str(aid)) if aid else None
            if url:
                if entry.get("banner") != url:
                    entry["banner"] = url
                    applied += 1
            else:
                missing += 1
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, DATA_FILE)
        print(f"applied {applied} banners (cached: {len(cache)}, "
              f"entries without banner: {missing})", flush=True)
        return

    # Collect ids missing from the cache.
    pending = []
    seen = set()
    for slug, entry in data.items():
        aid = entry.get("anilist_id")
        if aid and str(aid) not in cache and aid not in seen:
            seen.add(aid)
            pending.append(aid)
    print(f"total ids: {len(seen)}, cached: {len(cache)}, pending: {len(pending)}", flush=True)

    todo = pending[: args.limit]
    if not todo:
        print("nothing to fetch — run with --apply", flush=True)
        return

    got = 0
    none = 0
    for start in range(0, len(todo), PAGE_SIZE):
        batch = todo[start : start + PAGE_SIZE]
        try:
            media = fetch_batch(batch)
        except Exception as exc:
            print(f"stopped at {got} fetched: {exc}", flush=True)
            break
        found = {str(m["id"]): m.get("bannerImage") for m in media}
        for aid in batch:
            if str(aid) in found:
                url = found[str(aid)]
                if url:
                    cache[str(aid)] = url
                    got += 1
                else:
                    cache[str(aid)] = None  # remember: no banner exists
                    none += 1
        save_json(CACHE_FILE, cache)
        print(f"  batch {start // PAGE_SIZE + 1}: +{sum(1 for a in batch if str(a) in found)} "
              f"found ({got} banners, {none} none) | {len(cache)} cached", flush=True)
        time.sleep(SLEEP)
    print(f"done this run: {len(cache)} cached total", flush=True)


if __name__ == "__main__":
    main()
