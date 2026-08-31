#!/usr/bin/env python3
"""Fix total_episodes vs season-list mismatches across the catalog.

AniList's per-id `episodes` count is authoritative for each catalog entry
(after verifying the id's title matches the entry, so wrong ids like
Demon Slayer's 21612=Onigiri can't corrupt the fix). Categories:

1. Multi-season entries (>= 2 seasons) with seasons summing above the
   stale total (Demon Slayer 13 -> 63, MHA 13 -> 159): the seasons are
   real, so total_episodes = sum(seasons).
2. Single-season entries whose list overflows the AniList count (Spy x
   Family 25 -> 12, Solo Leveling 25 -> 12): phantom episodes, trim to
   the real count. Underflows (Red River 12 -> 24) get padded with
   number-only episodes.
3. Single-season entries with no AniList count (ongoing shows) whose
   seasons grew past the stale total: total_episodes = sum(seasons).
4. Multi-season entries with seasons BELOW the total (Conan 1012 vs
   1091): incomplete season data, left alone.

Run:  python3 scripts/fix_catalog_mismatches.py
"""
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
COUNTS_FILE = os.path.join(ROOT, "anime_anilist_counts.json")
TITLES_FILE = "/tmp/anilist_titles2.json"


def norm(s):
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\s*(season|part|cour)\s*\d+", " ", s)
    s = re.sub(r"[^a-z0-9\u3040-\u30ff\u4e00-\u9fff]+", " ", s)
    return s.strip()


def title_matches(entry, info):
    our = entry.get("title", "")
    alts = [info.get("romaji"), info.get("english"), info.get("native")]
    for at in alts:
        if not at:
            continue
        na, nb = norm(at), norm(our)
        ta, tb = set(na.split()), set(nb.split())
        if not ta or not tb:
            continue
        inter = len(ta & tb)
        if inter >= min(2, min(len(ta), len(tb))) and \
                inter / min(len(ta), len(tb)) >= 0.5:
            return True
    return False


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(COUNTS_FILE, "r", encoding="utf-8") as f:
        counts = json.load(f)
    titles = {}
    if os.path.exists(TITLES_FILE):
        with open(TITLES_FILE, "r", encoding="utf-8") as f:
            titles = json.load(f)

    fixed_total = []
    trimmed = []
    padded = []
    ongoing = []
    skipped = []

    for slug, entry in data.items():
        seasons = entry.get("seasons") or []
        if not seasons:
            continue
        ssum = sum(len(s.get("episodes") or []) for s in seasons)
        try:
            total = int(entry.get("total_episodes"))
        except (TypeError, ValueError):
            continue
        if ssum == total:
            continue
        aid = entry.get("anilist_id")
        key = str(aid) if aid else None
        A = counts.get(key) if key else None
        info = titles.get(key) or {}
        if info and not title_matches(entry, info):
            skipped.append((slug, "id mismatch", total, ssum, A))
            continue

        if len(seasons) >= 2:
            if ssum > total:
                entry["total_episodes"] = ssum
                fixed_total.append((slug, total, ssum, A))
            else:
                skipped.append((slug, "seasons below total", total, ssum, A))
        elif A and A > 0:
            if ssum > A:
                season = seasons[0]
                season["episodes"] = season["episodes"][:A]
                entry["total_episodes"] = A
                trimmed.append((slug, total, ssum, A))
            elif ssum < A:
                season = seasons[0]
                have = {e.get("number") for e in season["episodes"]}
                next_num = max(have) + 1 if have else 1
                for i in range(ssum + 1, A + 1):
                    season["episodes"].append({"number": next_num})
                    next_num += 1
                entry["total_episodes"] = A
                padded.append((slug, total, ssum, A))
            else:
                entry["total_episodes"] = A
                fixed_total.append((slug, total, ssum, A))
        else:
            if ssum > total:
                entry["total_episodes"] = ssum
                ongoing.append((slug, total, ssum))
            else:
                skipped.append((slug, "no count", total, ssum, A))

    print(f"multi-season total -> sum : {len(fixed_total)}")
    print(f"single-season trimmed      : {len(trimmed)}")
    print(f"single-season padded       : {len(padded)}")
    print(f"ongoing total -> sum       : {len(ongoing)}")
    print(f"left alone                 : {len(skipped)}")
    print()
    print("--- ongoing (eyeball) ---")
    for x in ongoing[:15]:
        print("  ", x)
    print("--- skipped (eyeball) ---")
    for x in skipped[:15]:
        print("  ", x)
    print()

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)
    print(f"saved {DATA_FILE}", flush=True)


if __name__ == "__main__":
    main()
