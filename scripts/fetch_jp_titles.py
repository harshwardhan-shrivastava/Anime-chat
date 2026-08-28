#!/usr/bin/env python3
"""Fetch Japanese native titles from AniList for every anime in the catalog.

Resumable + fast: builds a reverse anilist_id -> [slugs] map once, fetches
50 ids per GraphQL query, saves progress after every batch so it can be
re-run safely. Run it several times until it prints "ALL DONE".
"""
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT, "anime_data.json")
JP_PATH = os.path.join(ROOT, "anime_jp_titles.json")

ANILIST_URL = "https://graphql.anilist.co"

QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      title { romaji english native }
    }
  }
}
"""


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def main(max_batches=120):
    catalog = load_json(DATA_PATH, {})
    jp = load_json(JP_PATH, {})

    # Reverse map: anilist_id -> [slugs]
    by_id = {}
    for slug, entry in catalog.items():
        aid = entry.get("anilist_id")
        if aid:
            by_id.setdefault(aid, []).append(slug)

    missing_ids = []
    for aid in by_id:
        if not any(s in jp for s in by_id[aid]):
            missing_ids.append(aid)

    print(f"catalog={len(catalog)} already={len(jp)} missing_ids={len(missing_ids)}", flush=True)
    if not missing_ids:
        print("ALL DONE", flush=True)
        return 0

    done = 0
    for b in range(max_batches):
        chunk = missing_ids[b * 50:(b + 1) * 50]
        if not chunk:
            break
        for attempt in range(4):
            try:
                r = requests.post(
                    ANILIST_URL,
                    json={"query": QUERY, "variables": {"ids": chunk}},
                    headers={"Accept": "application/json"},
                    timeout=15,
                )
                if r.status_code == 429:
                    time.sleep(5)
                    continue
                r.raise_for_status()
                data = r.json()
                break
            except Exception as exc:
                print(f"batch {b} attempt {attempt} failed: {exc}", flush=True)
                time.sleep(3)
                data = None
        else:
            print(f"batch {b} gave up after retries", flush=True)
            continue

        media = (data or {}).get("data", {}).get("Page", {}).get("media", [])
        for m in media:
            mid = m.get("id")
            t = m.get("title") or {}
            native = (t.get("native") or "").strip()
            romaji = (t.get("romaji") or "").strip()
            if not native and not romaji:
                continue
            for slug in by_id.get(mid, []):
                if slug not in jp:
                    jp[slug] = {"native": native, "romaji": romaji}
        done += len(chunk)
        if b % 10 == 0:
            with open(JP_PATH, "w", encoding="utf-8") as f:
                json.dump(jp, f, ensure_ascii=False, indent=1)
            print(f"progress: batch {b}, done {done}/{len(missing_ids)}, saved {len(jp)}", flush=True)
        time.sleep(0.5)

    with open(JP_PATH, "w", encoding="utf-8") as f:
        json.dump(jp, f, ensure_ascii=False, indent=1)
    print(f"run complete: saved {len(jp)} titles", flush=True)
    return 1 if done < len(missing_ids) else 0


if __name__ == "__main__":
    sys.exit(main())