#!/usr/bin/env python3
"""One-time migration: upgrade TVmaze thumbnail URLs to the true-HD flavor.

TVmaze serves the same image under two names:
    /uploads/images/original/<path>            <- compressed proxy (~1 KB)
    /uploads/images/original_untouched/<path>  <- full-res master (1920x1080)

Same file path, only the folder name differs, so upgrading is a pure URL
rewrite — no API calls, no re-fetching. This rewrites every "thumb" value in
anime_data.json and every anime_ep_thumbs*.json cache that still points at
the compressed flavor.

Usage:
    python3 scripts/upgrade_thumbs_to_hd.py
"""

import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")

OLD_FLAVORS = (
    "/uploads/images/medium_landscape/",
    "/uploads/images/medium/",
    "/uploads/images/original/",
)
NEW = "/uploads/images/original_untouched/"


def rewrite_thumbs(obj, label):
    changed = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "thumb" and isinstance(v, str):
                for old in OLD_FLAVORS:
                    if old in v:
                        obj[k] = v.replace(old, NEW)
                        changed += 1
                        break
            else:
                changed += rewrite_thumbs(v, label)
    elif isinstance(obj, list):
        for item in obj:
            changed += rewrite_thumbs(item, label)
    return changed


def main():
    total = 0
    # 1. The catalog itself
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    n = rewrite_thumbs(data, "anime_data.json")
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)
    total += n
    print(f"anime_data.json: {n} thumbs upgraded to HD")
    del data

    # 2. Every resume cache
    for pat in ("anime_ep_thumbs.json", "anime_ep_thumbs_w*.json"):
        for path in sorted(glob.glob(os.path.join(ROOT, pat))):
            with open(path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            m = rewrite_thumbs(cache, path)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, path)
            if m:
                total += m
                print(f"{os.path.basename(path)}: {m} thumbs upgraded to HD")

    print(f"TOTAL: {total} thumbnails upgraded to true HD (1920x1080)")


if __name__ == "__main__":
    main()
