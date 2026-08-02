#!/usr/bin/env python3
"""Merge cached AniList airing-schedule data (produced by
scripts/fetch_anime_catalog.py --schedule) into the current anime_data.py
without regenerating the whole catalog.

This is a fast fallback for when a full --build is too heavy for a
short-lived sandbox shell. The same merge also runs inside --build.
"""
import json
import sys

sys.path.insert(0, ".")
import anime_data  # noqa: E402

sched = json.load(open("anime_schedule.json", encoding="utf-8"))
merged = 0
for slug, entry in anime_data.anime_database.items():
    aid = entry.get("anilist_id")
    info = sched.get(str(aid)) if aid else None
    if not info:
        continue
    nxt = info.get("nextAiringEpisode") or {}
    if nxt.get("episode"):
        entry["next_episode"] = nxt["episode"]
    if nxt.get("airingAt"):
        entry["next_episode_at"] = nxt["airingAt"]
    sd = info.get("startDate") or {}
    if sd.get("year"):
        entry["start_year"] = sd.get("year")
    if sd.get("month"):
        entry["start_month"] = sd.get("month")
    if sd.get("day"):
        entry["start_day"] = sd.get("day")
    merged += 1

with open("anime_data.py", "w", encoding="utf-8") as f:
    f.write(
        "# Auto-generated anime database -- %d titles (script: scripts/fetch_anime_catalog.py).\n"
        "anime_database = " % len(anime_data.anime_database)
    )
    json.dump(anime_data.anime_database, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"Merged schedule data into {merged} entries. Wrote anime_data.py.")
