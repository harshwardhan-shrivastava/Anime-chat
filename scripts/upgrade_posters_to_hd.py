#!/usr/bin/env python3
"""One-time migration: upgrade AniList poster URLs to the large (HD) flavor.

AniList serves the same cover art under two names:
    /media/anime/cover/medium/<file>  <- ~230px wide (what the catalog uses)
    /media/anime/cover/large/<file>   <- ~460px wide (true HD poster)

Same file path, only the flavor folder differs, so upgrading is a pure URL
rewrite — no API calls, no re-fetching. This rewrites every "image" and
"banner" value in anime_data.json that still points at the medium flavor.
The large flavor loads fine on slower connections thanks to the lazy-loading
on cards plus the preconnect hints in the page heads.

Usage:
    python3 scripts/upgrade_posters_to_hd.py
"""

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")

OLD = "/cover/medium/"
NEW = "/cover/large/"


def rewrite(obj, changed):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and OLD in v:
                obj[k] = v.replace(OLD, NEW)
                changed[0] += 1
            else:
                rewrite(v, changed)
    elif isinstance(obj, list):
        for item in obj:
            rewrite(item, changed)
    return changed


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    changed = rewrite(data, [0])
    print(f"upgraded {changed[0]} poster/banner URLs to large flavor", flush=True)

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)
    print("anime_data.json updated", flush=True)


if __name__ == "__main__":
    main()
