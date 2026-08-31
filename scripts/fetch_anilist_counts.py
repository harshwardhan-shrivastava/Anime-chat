#!/usr/bin/env python3
"""Fetch AniList's authoritative `episodes` count for every catalog title
and cache it in anime_anilist_counts.json (resumable).

Used to distinguish phantom episodes (season lists inflated beyond the
real count) from real data when fixing catalog mismatches.

Usage:
    python3 scripts/fetch_anilist_counts.py          # fetch (resumable)
    python3 scripts/fetch_anilist_counts.py --apply  # merge counts into anime_data.json
"""
import argparse
import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_anilist_counts.json")
API_URL = "https://graphql.anilist.co"
PAGE_SIZE = 50
SLEEP = 0.8


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
        media(id_in: $ids, type: ANIME) { id episodes }
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
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    data = load(DATA_FILE)
    cache = load(CACHE_FILE)

    if args.apply:
        applied = 0
        for slug, entry in data.items():
            aid = entry.get("anilist_id")
            count = cache.get(str(aid)) if aid else None
            if count is not None and entry.get("total_episodes") != count:
                entry["total_episodes"] = count
                applied += 1
        save_json(DATA_FILE, data)
        print(f"applied {applied} total_episodes from AniList", flush=True)
        return

    pending = []
    seen = set()
    for slug, entry in data.items():
        aid = entry.get("anilist_id")
        if aid and str(aid) not in cache and aid not in seen:
            seen.add(aid)
            pending.append(aid)
    print(f"cached: {len(cache)}, pending: {len(pending)}", flush=True)

    todo = pending[:10000]
    if not todo:
        print("nothing to fetch", flush=True)
        return

    got = 0
    none = 0
    for start in range(0, len(todo), PAGE_SIZE):
        batch = todo[start:start + PAGE_SIZE]
        try:
            media = fetch_batch(batch)
        except Exception as exc:
            print(f"stopped at {got}: {exc}", flush=True)
            break
        found = {str(m["id"]): m.get("episodes") for m in media}
        for aid in batch:
            if str(aid) in found:
                if found[str(aid)] is not None:
                    cache[str(aid)] = found[str(aid)]
                    got += 1
                else:
                    cache[str(aid)] = None
                    none += 1
        save_json(CACHE_FILE, cache)
        print(f"  batch {start // PAGE_SIZE + 1}: {got} counts, {none} null | {len(cache)} cached", flush=True)
        time.sleep(SLEEP)
    print(f"done: {len(cache)} cached", flush=True)


if __name__ == "__main__":
    main()
