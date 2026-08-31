#!/usr/bin/env python3
"""Fetch Kitsu coverImage (large = up to 3360px wide HD) for every catalog
title via Kitsu's free JSON:API (no key), and apply them as the hero
"banner" on anime detail pages.

AniList only ships bannerImage for ~6k titles; Kitsu covers nearly every
anime and serves them at up to 3360px wide — bigger than AniList's
1900px banners, so the hero can be full-bleed without upscaling.

How it works:
  - Sweeps Kitsu's `mappings` endpoint filtered to anilist/anime,
    including each mapping's anime `item` so one pass yields
    anilist_id -> Kitsu cover URL.
  - Resumable: progress (offset + found covers) is saved to
    anime_kitsu.json after every page, so a killed run just resumes.

Usage:
    python3 scripts/fetch_kitsu_covers.py            # sweep (resumable)
    python3 scripts/fetch_kitsu_covers.py --apply    # merge into anime_data.json
"""
import argparse
import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_kitsu.json")
API_URL = "https://kitsu.io/api/edge/mappings"
PAGE_SIZE = 20  # Kitsu caps page[limit] at 20
SLEEP = 0.25  # seconds between requests (Kitsu is generous, ~8 req/s)

PROGRESS_KEY = "__offset"


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


def fetch_page(offset):
    params = {
        "filter[externalSite]": "anilist/anime",
        "page[limit]": PAGE_SIZE,
        "page[offset]": offset,
        "include": "item",
        "fields[mappings]": "externalId,item",
        "fields[anime]": "id,coverImage",
    }
    headers = {"Accept": "application/vnd.api+json"}
    while True:
        resp = requests.get(API_URL, params=params, headers=headers, timeout=45)
        if resp.status_code == 429:
            print("  rate limited, sleeping 10s...", flush=True)
            time.sleep(10)
            continue
        resp.raise_for_status()
        return resp.json()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="merge cached Kitsu covers into anime_data.json banners")
    args = ap.parse_args()

    data = load(DATA_FILE)
    cache = load(CACHE_FILE)
    cache.pop(PROGRESS_KEY, None)

    if args.apply:
        applied = 0
        upgraded = 0
        missing = 0
        for slug, entry in data.items():
            aid = entry.get("anilist_id")
            url = cache.get(str(aid)) if aid else None
            if url:
                if entry.get("banner") != url:
                    entry["banner"] = url
                    applied += 1
                elif entry.get("banner"):
                    upgraded += 1
            else:
                missing += 1
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
        print(f"applied {applied} Kitsu covers (cached: {len(cache)}, "
              f"entries still without a cover: {missing})", flush=True)
        return

    resp = fetch_page(0)
    total = resp.get("meta", {}).get("count") or 0
    print(f"total anilist/anime mappings: {total}", flush=True)

    offset = int(load(CACHE_FILE).get(PROGRESS_KEY) or 0)
    if offset >= total:
        print("sweep already complete — run with --apply", flush=True)
        return

    got = 0
    while offset < total:
        try:
            resp = fetch_page(offset)
        except Exception as exc:
            print(f"stopped at offset {offset}: {exc}", flush=True)
            cache[PROGRESS_KEY] = offset
            save_json(CACHE_FILE, cache)
            break
        items = {}
        for inc in resp.get("included", []):
            if inc.get("type") == "anime":
                items[inc["id"]] = inc
        for m in resp.get("data", []):
            ext = m.get("attributes", {}).get("externalId")
            rel = m.get("relationships", {}).get("item", {}).get("data")
            if not ext or not rel:
                continue
            item = items.get(rel["id"])
            cover = (item or {}).get("attributes", {}).get("coverImage") or {}
            url = cover.get("large")
            key = str(ext)
            if url and key not in cache:
                cache[key] = url
                got += 1
        cache[PROGRESS_KEY] = offset + PAGE_SIZE
        save_json(CACHE_FILE, cache)
        offset += PAGE_SIZE
        print(f"  offset {offset}/{total} | covers cached: {len(cache) - 1} "
              f"(+{got} this run)", flush=True)
        time.sleep(SLEEP)
    print(f"done: {len(cache) - 1} covers cached", flush=True)


if __name__ == "__main__":
    main()
