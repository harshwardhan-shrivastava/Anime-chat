#!/usr/bin/env python3
"""Fix "That Time I Got Reincarnated as a Slime" seasons in anime_data.json.

The entry had 3 seasons (26/26/20) with phantom episodes and no S2/S3 titles.
Real structure: S1 24, S2 24, S3 24, S4 24 (96 episodes, continuous numbering).

Sources:
  - /tmp/slime_tvmaze.json : TVMaze episode titles per season (fetched fresh)
  - /tmp/slime_thumbs.json : TVMaze episode thumbs per season (from the
    existing anime_ep_thumbs_w*.json caches)

Run:  python3 scripts/fix_slime_seasons.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
SLUG = "that-time-i-got-reincarnated-as-a-slime"

SEASON_NAMES = ["Season 1", "Season 2", "Season 3", "Season 4"]


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    entry = data.get(SLUG)
    if not entry:
        print(f"ERROR: {SLUG} not found in anime_data.json", flush=True)
        sys.exit(1)

    with open("/tmp/slime_tvmaze.json", "r", encoding="utf-8") as f:
        tvmaze = json.load(f)  # {"1": [{"number","title"}, ...]}
    thumbs = {}
    if os.path.exists("/tmp/slime_thumbs.json"):
        with open("/tmp/slime_thumbs.json", "r", encoding="utf-8") as f:
            thumbs = json.load(f)  # {"1": {"1": url, ...}}

    seasons = []
    offset = 0
    for i, name in enumerate(SEASON_NAMES, start=1):
        tv_eps = tvmaze.get(str(i)) or []
        if not tv_eps:
            print(f"WARN: no TVMaze titles for season {i}, skipping", flush=True)
            continue
        ep_list = []
        for tv in tv_eps:
            n = offset + tv["number"]
            ep = {"number": n, "title": tv["title"] or f"Episode {n}"}
            thumb = (thumbs.get(str(i)) or {}).get(str(tv["number"]))
            if thumb:
                ep["thumb"] = thumb
            ep_list.append(ep)
        seasons.append({"name": name, "episodes": ep_list})
        offset += len(tv_eps)

    old_count = sum(len(s.get("episodes") or []) for s in entry.get("seasons") or [])
    entry["seasons"] = seasons
    entry["watch_order"] = [s["name"] for s in seasons]
    total = sum(len(s["episodes"]) for s in seasons)
    entry["total_episodes"] = total

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

    print(f"rebuilt {SLUG}: {old_count} eps -> {total} eps across "
          f"{len(seasons)} seasons", flush=True)
    for s in seasons:
        print(f"  {s['name']}: {len(s['episodes'])} eps, "
              f"{sum(1 for e in s['episodes'] if e.get('thumb'))} thumbs", flush=True)


if __name__ == "__main__":
    main()
