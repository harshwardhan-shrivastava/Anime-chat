#!/usr/bin/env python3
"""Fill the missing episodes of hand-added catalog cards from TVmaze.

Some of the first-57 user-added cards only cover part of their run (e.g.
One Piece stops at ep 402 of 1181, Dragon Ball Z at 167 of 291). TVmaze has
the full run with real episode names and (for most) per-episode stills, so
this script:

* upgrades placeholder titles ("East Blue Saga - Episode 1", "Season 2 -
  Episode 5", "Episode 3", TBC ...) in the existing seasons to the real
  names from TVmaze (matched by global aired order),
* appends the missing episodes as new seasons (chunked along TVmaze season
  boundaries, labeled "Season N" continuing the card's numbering),
* renames obviously mislabeled seasons (e.g. Inuyasha's "The Final Act"
  season, whose content is actually main-series episodes 54-79; the real
  Final Act lives on its own card),
* keeps total_episodes consistent with the actual episode count.

The card's episodes are assumed to be in aired order matching TVmaze's flat
episode order (season 1..N in the JSON response), which holds for the
hand-added cards.

Usage:
    python3 scripts/fill_episode_gaps.py
"""
import json
import os
import re
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.enrich_airing import (  # noqa: E402
    DATA_FILE,
    _tvmaze_ep_image,
    load_json,
    save_json,
)

TVM_API = "https://api.tvmaze.com"

# slug -> (tvmaze show id, target episode count)
# Target = the show's full run on the MAIN card only; sequel series that have
# their own cards (Bleach TYBW, Fairy Tail Series 2/Final, Inuyasha: The
# Final Act, HxH 2011, SAO II/Alicization, Tokyo Ghoul :re, ...) are excluded
# on purpose.
TARGETS = {
    "one-piece": (1505, 1181),
    "dragon-ball-z": (2103, 291),
    "dragon-ball-super": (2368, 131),
    "bleach": (1905, 366),
    "fairy-tail": (2069, 175),
    "attack-on-titan": (919, 89),
    "inuyasha": (4247, 167),
    "hitman-reborn": (16151, 203),
    "sword-art-online": (2059, 25),
}

# Remove this whole season from the card (duplicate of the separate
# sword-art-online-alicization card).
DROP_SEASONS = {
    "sword-art-online": {"Alicization Arc"},
}

# Rename seasons whose content is not what the label says (kept in aired
# order, so this only affects the display name).
RENAME_SEASONS = {
    "inuyasha": {"The Final Act": "Season 3"},
}

# Any of: "Season 2 - Episode 5", "East Blue Saga - Episode 1",
# "Episode 3", "Episode 1179", "TBC"/"TBA"/"TBD"/"To be released".
_PH_RE = re.compile(
    r"(?:^|[-–]\s*)\s*Episode\s*\d+\s*$|"
    r"Season\s*\d+\s*[-–]\s*Episode\s*\d+\s*$|"
    r"^Episode\s*\d+\s*$",
    re.I,
)


def _is_placeholder(title):
    if not title:
        return True
    t = title.strip()
    if t.lower() in ("tbc", "tba", "tbd", "to be released", "untitled"):
        return True
    return bool(_PH_RE.search(t))


def _tvmaze_flat(sid):
    try:
        r = requests.get("%s/shows/%s/episodes" % (TVM_API, sid), timeout=25)
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:
        return []


def _upgrade_existing(seasons, flat, target):
    """Replace placeholder titles and add thumbs in existing seasons."""
    titles = 0
    thumbs = 0
    old_by_global = {}
    idx = 1
    for s in seasons:
        for ep in s.get("episodes") or []:
            old_by_global[idx] = ep
            idx += 1
    for g, ep in old_by_global.items():
        if g > target or g > len(flat):
            continue
        te = flat[g - 1]
        if _is_placeholder(ep.get("title")):
            tname = (te.get("name") or "").strip()
            if tname and not _is_placeholder(tname):
                ep["title"] = tname
                titles += 1
        if not ep.get("thumb"):
            img = _tvmaze_ep_image(te)
            if img:
                ep["thumb"] = img
                thumbs += 1
    return titles, thumbs


def main():
    data = load_json(DATA_FILE)
    stats = {"episodes_added": 0, "seasons_added": 0,
             "titles_filled": 0, "thumbs_added": 0}

    # Drop duplicate seasons first.
    for slug, names in DROP_SEASONS.items():
        entry = data.get(slug)
        if not entry:
            continue
        before = len(entry.get("seasons") or [])
        entry["seasons"] = [s for s in (entry.get("seasons") or [])
                            if s.get("name") not in names]
        entry["watch_order"] = [n for n in (entry.get("watch_order") or [])
                                if n not in names]
        if len(entry.get("seasons") or []) != before:
            print(f"{entry.get('title')}: dropped duplicate season(s) {sorted(names)}")

    for slug, (sid, target) in TARGETS.items():
        entry = data.get(slug)
        if not entry:
            print(f"SKIP {slug}: not in catalog")
            continue
        flat = _tvmaze_flat(sid)
        if not flat:
            print(f"SKIP {slug}: no TVmaze data")
            continue
        if len(flat) < target:
            target = len(flat)

        seasons = entry.get("seasons") or []
        cur = sum(len(s.get("episodes") or []) for s in seasons)

        # Upgrade placeholder titles/thumbs in the existing seasons first —
        # this must run even when the card is already episode-complete.
        t_up, th_up = _upgrade_existing(seasons, flat, target)

        if cur >= target:
            entry["seasons"] = seasons
            print(f"{entry.get('title')}: complete ({cur}/{target}) — "
                  f"{t_up} titles upgraded, {th_up} thumbs added")
            continue

        # Append the missing episodes, chunked along TVmaze season changes.
        missing = flat[cur:target]
        chunks = []
        cur_chunk = []
        cur_season = None
        for te in missing:
            s = te.get("season")
            if s != cur_season and cur_chunk:
                chunks.append(cur_chunk)
                cur_chunk = []
            cur_season = s
            cur_chunk.append(te)
        if cur_chunk:
            chunks.append(cur_chunk)

        next_idx = len(seasons) + 1
        for chunk in chunks:
            eps = []
            for n, te in enumerate(chunk, 1):
                ep = {"number": n}
                tname = (te.get("name") or "").strip()
                if tname and not _is_placeholder(tname):
                    ep["title"] = tname
                img = _tvmaze_ep_image(te)
                if img:
                    ep["thumb"] = img
                eps.append(ep)
            seasons.append({"name": f"Season {next_idx}", "episodes": eps})
            stats["seasons_added"] += 1
            stats["episodes_added"] += len(eps)
            next_idx += 1

        entry["seasons"] = seasons
        stats["titles_filled"] += t_up
        stats["thumbs_added"] += th_up
        print(f"{entry.get('title')}: {cur} -> {cur + len(missing)} eps "
              f"({len(missing)} added, {t_up} titles upgraded, {th_up} thumbs added)")

    # Rename mislabeled seasons + normalize total_episodes for every card we
    # touched (drop + targets), so counts are always consistent.
    touched = set(TARGETS) | set(DROP_SEASONS)
    for slug in touched:
        entry = data.get(slug)
        if not entry:
            continue
        seasons = entry.get("seasons") or []
        for s in seasons:
            sname = s.get("name")
            for old, new in RENAME_SEASONS.get(slug, {}).items():
                if sname == old:
                    s["name"] = new
        entry["watch_order"] = [s.get("name") for s in seasons]
        entry["total_episodes"] = sum(len(s.get("episodes") or []) for s in seasons)

    save_json(DATA_FILE, data)
    print("DONE:", stats)


if __name__ == "__main__":
    sys.exit(main())
