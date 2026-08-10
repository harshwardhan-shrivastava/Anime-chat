#!/usr/bin/env python3
"""
Keep currently-airing shows honest using live AniList airing data.

For every catalog entry whose baked status is Ongoing/Upcoming we fetch live
AniList data (status, total episodes, nextAiringEpisode and the full airing
schedule), then:

  * normalize the status (RELEASING -> Ongoing, FINISHED -> Completed, ...)
  * refresh next_episode / next_episode_at / start_date
  * fix total_episodes (removes the "0 Eps" chips on brand-new cours)
  * for Ongoing shows: mark episodes past the aired count as released:false
    with title "TBC" and drop their (leaked/filler) thumbnails, so only
    officially-released episodes show names + thumbs
  * backfill real episode titles for aired episodes from the MAL cache
  * create a season structure for Ongoing shows that had none yet (new cours
    such as 100 Girlfriends S3 / That Time I Got Reincarnated as a Slime S4)

Usage:
    python3 scripts/enrich_airing.py --plan --todo anime_airing_todo.json
    python3 scripts/enrich_airing.py --fetch 300 --offset 0 --cache anime_airing_a0.json --todo anime_airing_todo.json
    python3 scripts/enrich_airing.py --apply
"""

import argparse
import json
import os
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
MAL_FILE = os.path.join(ROOT, "anime_mal_episodes.json")
CACHE_PATTERNS = ("anime_airing_a*.json",)

API = "https://graphql.anilist.co"

STATUS_MAP = {
    "FINISHED": "Completed",
    "RELEASING": "Ongoing",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}

PAGE_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      status
      episodes
      startDate { year month day }
      nextAiringEpisode { episode airingAt }
      airingSchedule(perPage: 100) { nodes { episode airingAt } }
    }
  }
}
"""


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def plan_todo(todo_path):
    """Build the todo list: [slug, anilist_id, title] for Ongoing/Upcoming."""
    data = load_json(DATA_FILE)
    todo = []
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if e.get("status") in ("Ongoing", "Upcoming") and aid:
            todo.append([slug, aid, e.get("title") or slug])
    save_json(todo_path, todo)
    print(f"PLAN: {len(todo)} airing/upcoming entries with anilist_id.")


def fetch_window(count, offset=0, cache_file=None, todo_path=None):
    todo = load_json(todo_path) if todo_path else []
    window = todo[offset:offset + count]
    if not window:
        print("Nothing to fetch in this window.")
        return
    result = {}
    for i in range(0, len(window), 50):
        chunk = window[i:i + 50]
        ids = [aid for _, aid, _ in chunk]
        try:
            r = requests.post(
                API,
                json={"query": PAGE_QUERY, "variables": {"ids": ids}},
                timeout=30,
            )
            if r.status_code == 200:
                media = r.json().get("data", {}).get("Page", {}).get("media", [])
                for m in media:
                    result[str(m["id"])] = {
                        "status": m.get("status"),
                        "episodes": m.get("episodes"),
                        "startDate": m.get("startDate") or {},
                        "nextAiringEpisode": m.get("nextAiringEpisode") or {},
                        "airingSchedule": m.get("airingSchedule") or {},
                    }
            else:
                print(f"HTTP {r.status_code} for ids {ids[:5]}... ({r.text[:120]})")
        except Exception as exc:
            print(f"Request failed for {ids[:5]}...: {exc}")
        time.sleep(0.8)

    if cache_file:
        prev = load_json(cache_file)
        prev.update(result)
        save_json(cache_file, prev)
    print(f"FETCH: {len(result)}/{len(window)} entries cached -> {cache_file}")


def _cache_files():
    files = []
    for pat in CACHE_PATTERNS:
        files.extend(sorted(glob_files(pat)))
    return sorted(set(files))


def glob_files(pattern):
    import glob

    return glob.glob(os.path.join(ROOT, pattern))


def _global_number(seasons, si, ep_number):
    """Map (season idx, ep number) to the show-global episode number.

    Cards that restart numbering each season (S2 starts at 1) accumulate an
    offset; cards that already use global numbering pass the number through.
    """
    offset = 0
    for i, s in enumerate(seasons):
        eps = s.get("episodes") or []
        if i == si:
            first = (eps[0].get("number") or 1) if eps else 1
            if i == 0 or first == 1:
                return offset + (ep_number or 0)
            return ep_number or 0
        offset += len(eps)
    return ep_number or 0


def apply_airing():
    data = load_json(DATA_FILE)
    mal = load_json(MAL_FILE)

    by_aid = {}
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if aid:
            by_aid.setdefault(aid, []).append((slug, e))

    merged = {}
    for fname in _cache_files():
        merged.update(load_json(fname) or {})

    now = int(time.time())
    stats = {
        "status_fixed": 0,
        "next_fixed": 0,
        "total_fixed": 0,
        "tbc_marked": 0,
        "thumbs_removed": 0,
        "titles_backfilled": 0,
        "seasons_created": 0,
    }

    for aid_s, info in merged.items():
        aid = int(aid_s)
        for slug, e in by_aid.get(aid, []):
            st = STATUS_MAP.get(info.get("status"))
            if st:
                e["status"] = st
                stats["status_fixed"] += 1

            nxt = info.get("nextAiringEpisode") or {}
            if nxt.get("airingAt"):
                e["next_episode_at"] = nxt["airingAt"]
                if nxt.get("episode"):
                    e["next_episode"] = nxt["episode"]
                stats["next_fixed"] += 1
            else:
                e.pop("next_episode_at", None)

            sd = info.get("startDate") or {}
            for key in ("year", "month", "day"):
                if sd.get(key):
                    e[f"start_{key}"] = sd[key]

            nodes = (info.get("airingSchedule") or {}).get("nodes") or []
            aired = None
            if nxt.get("episode"):
                aired = nxt["episode"] - 1
            else:
                done = [nd["episode"] for nd in nodes if nd.get("airingAt") and nd["airingAt"] <= now]
                if done:
                    aired = max(done)

            total = info.get("episodes") or 0
            if not total and nodes:
                total = max((nd.get("episode") or 0) for nd in nodes)
            if total:
                e["total_episodes"] = total
                stats["total_fixed"] += 1

            if st != "Ongoing" or not aired:
                continue

            seasons = e.get("seasons") or []
            if not seasons:
                if not total:
                    total = aired
                seasons = [
                    {"name": "Season 1",
                     "episodes": [{"number": n} for n in range(1, total + 1)]}
                ]
                e["seasons"] = seasons
                if not e.get("watch_order"):
                    e["watch_order"] = ["Season 1"]
                stats["seasons_created"] += 1

            mal_titles = mal.get(slug) or {}
            for si, s in enumerate(seasons):
                for ep in s.get("episodes") or []:
                    gnum = _global_number(seasons, si, ep.get("number") or 0)
                    if gnum > aired:
                        if ep.get("released") is not False:
                            ep["released"] = False
                        ep["title"] = "TBC"
                        if ep.get("thumb"):
                            ep.pop("thumb", None)
                            stats["thumbs_removed"] += 1
                        stats["tbc_marked"] += 1
                    else:
                        ep.pop("released", None)
                        if not ep.get("title") and str(ep.get("number")) in mal_titles:
                            ep["title"] = mal_titles[str(ep["number"])]
                            stats["titles_backfilled"] += 1

    save_json(DATA_FILE, data)
    print("APPLIED:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--todo", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        apply_airing()
        return
    if args.plan:
        plan_todo(args.todo or "anime_airing_todo.json")
    if args.fetch:
        fetch_window(args.fetch, offset=args.offset, cache_file=args.cache,
                     todo_path=args.todo or "anime_airing_todo.json")
    if not (args.apply or args.plan or args.fetch):
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
