#!/usr/bin/env python3
"""
Backfill real episode titles from the existing anime_streaming.json cache into
anime_data.json. The cache holds AniList streamingEpisodes (title/url/site) for
~13.8k titles; only titles matching the "Episode N - Name" pattern are usable.

This pass is offline (no API calls): for every catalog entry whose anilist_id
is cached, it attaches the real episode name to the matching episode in its
seasons. Episodes that already carry a title are left untouched, and episodes
with no cached name keep their numbered placeholder.

Usage:
    python3 scripts/enrich_episode_titles.py
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_streaming.json")

EPISODE_RE = re.compile(
    r"^(?:ep(?:isode)?)\s+(\d+)\s*[-\u2013\u2014:.]?\s*(.*)$", re.IGNORECASE
)


def parse_episode_titles(episodes):
    """Map real episode number -> title from AniList streamingEpisodes."""
    parsed = {}
    for ep in episodes or []:
        title = (ep.get("title") or "").strip()
        m = EPISODE_RE.match(title)
        if not m:
            continue
        n = int(m.group(1))
        label = (m.group(2) or "").strip()
        # Drop AniList's useless "Untitled" placeholders.
        if not label or label.lower() == "untitled":
            continue
        parsed.setdefault(n, label)
    return parsed


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)

    applied = 0
    titles_fixed = 0
    for entry in data.values():
        aid = entry.get("anilist_id")
        if not aid:
            continue
        parsed = parse_episode_titles(cache.get(str(aid)))
        if not parsed:
            continue
        for season in entry.get("seasons") or []:
            for episode in season.get("episodes") or []:
                num = episode.get("number")
                if episode.get("title") or num not in parsed:
                    continue
                episode["title"] = parsed[num]
                applied += 1
        if any(
            ep.get("title")
            for s in (entry.get("seasons") or [])
            for ep in (s.get("episodes") or [])
        ):
            titles_fixed += 1

    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, DATA_FILE)

    with_named = sum(
        1
        for e in data.values()
        if any(ep.get("title") for s in (e.get("seasons") or []) for ep in (s.get("episodes") or []))
    )
    print(f"Applied {applied} episode titles across {titles_fixed} titles.")
    print(f"Titles with >=1 real episode name now: {with_named}")


if __name__ == "__main__":
    sys.exit(main())
