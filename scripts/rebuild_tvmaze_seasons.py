#!/usr/bin/env python3
"""Rebuild flat 100+ episode kids'/cartoon shows into their TVmaze season
structure with real episode names.

These cards (Doraemon, Shin-chan, Chiikawa, Beyblade X, Pokémon Horizons)
stored everything in a single flat "Season 1" with mostly empty placeholder
episodes. TVmaze tracks their real per-year/per-season structure, so this
script rebuilds each card from TVmaze's flat episode list: every episode
gets its real name, and episodes are grouped into the show's actual seasons
(years for the long-runners) with episode counts.

Existing card thumbs are preserved where their episode matches a TVmaze
episode.

Usage:
    python3 scripts/rebuild_tvmaze_seasons.py
"""
import os
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

# slug -> (tvmaze show id, season-name strategy)
# strategy:
#   ("year-from-premiere",)          -> name from premiere year of TVmaze season
#   ("season-number",)               -> "Season N"
#   ("named", [names...])            -> explicit arc names in season order
BUILD = {
    "doraemon-2005": (55578, "year-from-premiere"),
    "shin-chan": (29247, "season-number-as-year"),
    "chiikawa": (66868, "season-number-as-year"),
    "beyblade-x": (71763, "season-number"),
    "pok-mon-horizons-the-series": (
        92823,
        ("named", ["Liko and Roy's Departure", "The Search for Laqua",
                   "Rising Hope", "Wonder Voyage"]),
    ),
}

PLACEHOLDER = {"", "tbc", "tba", "tbd", "to be released", "untitled"}


def _norm(t):
    return (t or "").strip().lower().replace("…", "...").replace("’", "'")


def _tvmaze_season_meta(sid):
    """season number -> premiere year (from /seasons)."""
    meta = {}
    try:
        r = requests.get(f"https://api.tvmaze.com/shows/{sid}/seasons", timeout=25)
        if r.status_code == 200:
            for s in r.json() or []:
                pre = s.get("premiereDate") or ""
                if pre:
                    meta[s.get("number")] = pre[:4]
    except Exception:
        pass
    return meta


def _season_name(slug, strategy, season_num, meta):
    if isinstance(strategy, tuple) and strategy[0] == "named":
        names = strategy[1]
        return names[season_num - 1] if 1 <= season_num <= len(names) else f"Season {season_num}"
    if strategy == "season-number-as-year":
        return str(season_num)
    if strategy == "year-from-premiere":
        year = meta.get(season_num)
        return year if year else f"Season {season_num}"
    return f"Season {season_num}"


def main():
    data = load_json(DATA_FILE)

    for slug, (sid, strategy) in BUILD.items():
        entry = data.get(slug)
        if not entry:
            print(f"SKIP {slug}: not in catalog")
            continue

        r = requests.get(f"https://api.tvmaze.com/shows/{sid}/episodes", timeout=30)
        if r.status_code != 200:
            print(f"SKIP {slug}: TVmaze error {r.status_code}")
            continue
        flat = r.json() or []

        # Preserve card thumbs for episodes that match a TVmaze episode.
        # Kids' show titles are multi-story strings split by " / "; compare
        # the first story segment so translation differences don't block a
        # match (e.g. "Mom's Mornings are Busy" vs "Mama's Morning").
        def _seg(t):
            seg = _norm(t).split(" / ")[0].split("/")[0].strip()
            return seg[:40] if seg else ""

        card_thumbs = {}
        for s in entry.get("seasons") or []:
            for ep in s.get("episodes") or []:
                seg = _seg(ep.get("title"))
                if ep.get("thumb") and seg:
                    card_thumbs[seg] = ep["thumb"]

        meta = _tvmaze_season_meta(sid)
        chunks = {}
        for te in flat:
            sn = te.get("season")
            chunks.setdefault(sn, []).append(te)

        new_seasons = []
        for sn in sorted(chunks):
            eps = []
            for i, te in enumerate(chunks[sn], 1):
                ep = {"number": i}
                tname = (te.get("name") or "").strip()
                if tname and tname.lower() not in PLACEHOLDER and \
                        not tname.lower().startswith("episode "):
                    ep["title"] = tname
                thumb = _tvmaze_ep_image(te) or card_thumbs.get(_seg(tname))
                if thumb:
                    ep["thumb"] = thumb
                eps.append(ep)
            name = _season_name(slug, strategy, sn, meta)
            new_seasons.append({"name": name, "episodes": eps})

        entry["seasons"] = new_seasons
        entry["watch_order"] = [s["name"] for s in new_seasons]
        entry["total_episodes"] = sum(len(s["episodes"]) for s in new_seasons)
        preview = ', '.join(f'{s["name"]} ({len(s["episodes"])})'
                            for s in new_seasons[:4])
        more = ' …' if len(new_seasons) > 4 else ''
        print(f"{entry['title']}: {len(flat)} eps -> {len(new_seasons)} seasons ({preview}{more})")

    save_json(DATA_FILE, data)
    print("DONE")


if __name__ == "__main__":
    sys.exit(main())
