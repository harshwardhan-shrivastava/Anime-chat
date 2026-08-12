#!/usr/bin/env python3
"""Add missing seasons to hand-added catalog cards.

Some of the first-57 user-added anime are missing whole seasons (Blue
Exorcist S2-S5, Psycho-Pass S2-S3, D.Gray-man Hallow, Seven Deadly Sins
S3-S4). TVmaze has every season with real episode names and (for most)
per-episode stills. This script rebuilds each card's season list from the
mapped TVmaze seasons: real episode titles replace the generated
"Season X - Episode Y" placeholders, existing thumbs are kept, and missing
thumbs are filled from TVmaze where available.

Usage:
    python3 scripts/add_missing_seasons.py
"""
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.enrich_airing import (  # noqa: E402
    DATA_FILE,
    _hd_url,
    _tvmaze_ep_image,
    load_json,
    save_json,
)

TVM_API = "https://api.tvmaze.com"

# slug -> (tvmaze show id, ordered list of (our season name, tvmaze season))
# tvmaze season None means "spread the existing episodes across the TVmaze
# seasons in order" (D.Gray-man's 103-ep run is TVmaze S1(51) + S2(52)).
TARGETS = {
    "blue-exorcist": {
        "tvmaze": 5896,
        "seasons": [
            ("Season 1", 1),
            ("Kyoto Saga", 2),
            ("Shimane Illuminati Saga", 3),
            ("Beyond the Snow Saga", 4),
            ("Beyond the Snow Saga 2", 5),
        ],
    },
    "psycho-pass": {
        "tvmaze": 1939,
        "seasons": [
            ("Season 1", 1),
            ("Season 2", 2),
            ("Season 3", 3),
        ],
    },
    "dgray-man": {
        "tvmaze": 6840,
        "seasons": [
            ("Season 1", None),
            ("Hallow", 3),
        ],
    },
    "seven-deadly-sins": {
        "tvmaze": 8524,
        "seasons": [
            ("Season 1", 1),
            ("Revival of the Commandments", 2),
            ("Wrath of the Gods", 3),
            ("Dragon's Judgement", 4),
        ],
    },
}

_PH_RE = re.compile(
    r"(?:^|[\-\s])(?:Season|S)\s*\d+\s*-\s*Episode\s*\d+$|^Episode\s*\d+$",
    re.I,
)


def _is_placeholder(title):
    if not title:
        return True
    t = title.strip().lower()
    if t in ("tbc", "tba", "tbd", "to be released", "untitled"):
        return True
    return bool(_PH_RE.search(title))


def _tvmaze_episodes(sid):
    try:
        r = requests.get("%s/shows/%s/episodes" % (TVM_API, sid), timeout=15)
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:
        return []


def main():
    data = load_json(DATA_FILE)
    stats = {"seasons_added": 0, "titles_filled": 0, "thumbs_added": 0}

    for slug, cfg in TARGETS.items():
        entry = data.get(slug)
        if not entry:
            print(f"SKIP {slug}: not in catalog")
            continue
        eps_all = _tvmaze_episodes(cfg["tvmaze"])
        if not eps_all:
            print(f"SKIP {slug}: no TVmaze episodes")
            continue

        by_season = {}
        for e in eps_all:
            by_season.setdefault(e.get("season"), []).append(e)
        by_num = {}
        for s in sorted(by_season):
            by_num[s] = {e.get("number"): e for e in by_season[s]}

        # Existing seasons keyed by name, preserving thumbs/flags.
        existing = {}
        for s in entry.get("seasons") or []:
            existing.setdefault(s.get("name"), s)

        # D.Gray-man: the flat 103-ep "Season 1" spans TVmaze S1+S2.
        flat_span = {}
        if cfg["seasons"][0][1] is None:
            flat_span = {}
            idx = 1
            for s in sorted(by_season):
                for e in by_season[s]:
                    flat_span[idx] = e
                    idx += 1

        new_seasons = []
        for name, tseason in cfg["seasons"]:
            old = existing.get(name)
            old_eps = (old or {}).get("episodes") or []
            old_by_num = {e.get("number"): e for e in old_eps}

            if tseason is None:
                pool = flat_span
                count = len(old_eps)
            else:
                pool = by_num.get(tseason) or {}
                count = len(pool)

            episodes = []
            for n in range(1, count + 1):
                te = pool.get(n)
                if te is None:
                    continue
                old_ep = old_by_num.get(n) or {}
                ep = dict(old_ep)
                ep["number"] = n
                if _is_placeholder(ep.get("title")):
                    tname = (te.get("name") or "").strip()
                    if tname and not _is_placeholder(tname):
                        ep["title"] = tname
                        stats["titles_filled"] += 1
                if not ep.get("thumb"):
                    img = _tvmaze_ep_image(te)
                    if img:
                        ep["thumb"] = img
                        stats["thumbs_added"] += 1
                episodes.append(ep)
                time.sleep(0)
            if not old and tseason is not None:
                stats["seasons_added"] += 1
            new_seasons.append({"name": name, "episodes": episodes})

        entry["seasons"] = new_seasons
        entry["watch_order"] = [name for name, _ in cfg["seasons"]]
        print(f"{entry.get('title')}: {len(new_seasons)} seasons "
              f"({[len(s['episodes']) for s in new_seasons]})")

    save_json(DATA_FILE, data)
    print("DONE:", stats)


if __name__ == "__main__":
    sys.exit(main())
