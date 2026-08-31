#!/usr/bin/env python3
"""Fix phantom-episode data errors across the catalog.

Two reported bugs plus a safe site-wide sweep:

1. Slime: the main entry listed all 4 seasons even though the site has
   separate cards/entries for each season — the main card should only be
   Season 1. The separate season-2 / season-2-part-2 / season-3 / season-4
   entries carried copied garbage (26/26/20 phantom episodes). Rebuild
   them from the TVMaze titles/thumbs caches (/tmp/slime_tvmaze.json,
   /tmp/slime_thumbs.json) and fix the movie/OAD/coleus entries that
   inherited the same phantom structure.

2. The 100 Girlfriends: the main entry's "Season 1" merged season 1 + 2
   (24 eps) while the real S1 has 12. Split it: main keeps S1's 12 eps,
   the season-2 entry keeps S2's 12 eps.

3. Sweep: single-season entries whose season list exceeds
   total_episodes with trailing titleless episodes (phantoms) get trimmed
   to total_episodes. Only applied when every dropped episode lacks a
   title, so real (titleless) data is never cut.

Run:  python3 scripts/fix_episode_errors.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")

SLIME = "that-time-i-got-reincarnated-as-a-slime"
SLIME_S2 = SLIME + "-season-2"
SLIME_S2P2 = SLIME + "-season-2-part-2"
SLIME_S3 = SLIME + "-season-3"
SLIME_S4 = SLIME + "-season-4"
SLIME_MOVIE = SLIME + "-the-movie-scarlet-bond"
SLIME_OAD = SLIME + "-oad"
SLIME_COLEUS = SLIME + "-visions-of-coleus"

G100 = "the-100-girlfriends-who-really-really-really-really-really-love-you"
G100_S2 = G100 + "-season-2"


def load():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def one_season(name, episodes):
    return [{"name": name, "episodes": episodes}]


def numbered(n, start=1):
    return [{"number": i} for i in range(start, start + n)]


def build_slime_seasons(data, tvmaze, thumbs):
    """Return per-slime-entry (slug, seasons, total) rebuilds."""
    def eps(season_key, start=1):
        out = []
        for tv in tvmaze[str(season_key)]:
            n = start + tv["number"] - 1
            ep = {"number": n, "title": tv["title"] or f"Episode {n}"}
            thumb = (thumbs.get(str(season_key)) or {}).get(str(tv["number"]))
            if thumb:
                ep["thumb"] = thumb
            out.append(ep)
        return out

    # Main entry: Season 1 only (24 eps).
    main = (SLIME, one_season("Season 1", eps(1)), 24)

    # Season 2 entry: its own Season 1 = the show's S2 (24 eps).
    s2 = (SLIME_S2, one_season("Season 1", eps(2)), 24)

    # Season 2 part 2: second cour of S2 (eps 13-24 renumbered 1-12).
    part2_eps = []
    for tv in tvmaze["2"]:
        if tv["number"] >= 13:
            n = tv["number"] - 12
            ep = {"number": n, "title": tv["title"] or f"Episode {n}"}
            thumb = (thumbs.get("2") or {}).get(str(tv["number"]))
            if thumb:
                ep["thumb"] = thumb
            part2_eps.append(ep)
    s2p2 = (SLIME_S2P2, one_season("Season 1", part2_eps), len(part2_eps))

    # Season 3 entry: 24 eps.
    s3 = (SLIME_S3, one_season("Season 1", eps(3)), 24)

    # Season 4 entry: rebuild consistently from the same source.
    s4 = (SLIME_S4, one_season("Season 1", eps(4)), 24)

    # Movie / OAD / Coleus: drop inherited phantom episodes.
    movie = (SLIME_MOVIE, one_season("Movie", numbered(1)), 1)
    oad = (SLIME_OAD, one_season("Season 1", numbered(5)), 5)
    coleus = (SLIME_COLEUS, one_season("Season 1", numbered(3)), 3)

    return [main, s2, s2p2, s3, s4, movie, oad, coleus]


def fix_100_girlfriends(data):
    main = data[G100]
    s2 = data[G100_S2]
    main_eps = (main.get("seasons") or [{}])[0].get("episodes") or []
    if len(main_eps) <= 12:
        return False
    s1 = main_eps[:12]
    s2_eps = [
        {k: (v - 12 if k == "number" else v) for k, v in e.items()}
        for e in main_eps[12:]
    ]
    main["seasons"] = one_season("Season 1", s1)
    main["total_episodes"] = 12
    main["watch_order"] = ["Season 1"]
    s2["seasons"] = one_season("Season 1", s2_eps)
    s2["total_episodes"] = len(s2_eps)
    s2["watch_order"] = ["Season 1"]
    return True


def sweep_phantom_single_seasons(data):
    """Trim single-season entries whose season overflows total_episodes
    with trailing titleless (phantom) episodes."""
    fixed = []
    for slug, entry in data.items():
        seasons = entry.get("seasons") or []
        if len(seasons) != 1:
            continue
        season = seasons[0]
        eps = season.get("episodes") or []
        try:
            total = int(entry.get("total_episodes") or 0)
        except (TypeError, ValueError):
            continue
        if total <= 0 or len(eps) <= total:
            continue
        trailing = eps[total:]
        if trailing and all("title" not in e for e in trailing):
            season["episodes"] = eps[:total]
            entry["total_episodes"] = total
            fixed.append((slug, len(eps), total))
    return fixed


def main():
    data = load()
    tvmaze_path = "/tmp/slime_tvmaze.json"
    thumbs_path = "/tmp/slime_thumbs.json"
    if not os.path.exists(tvmaze_path):
        print(f"ERROR: {tvmaze_path} missing — re-fetch slime TVMaze data",
              flush=True)
        sys.exit(1)
    with open(tvmaze_path, "r", encoding="utf-8") as f:
        tvmaze = json.load(f)
    thumbs = {}
    if os.path.exists(thumbs_path):
        with open(thumbs_path, "r", encoding="utf-8") as f:
            thumbs = json.load(f)

    # 1. Slime family
    for slug, seasons, total in build_slime_seasons(data, tvmaze, thumbs):
        entry = data.get(slug)
        if not entry:
            print(f"WARN: {slug} not found", flush=True)
            continue
        old = sum(len(s.get("episodes") or []) for s in entry.get("seasons") or [])
        entry["seasons"] = seasons
        entry["total_episodes"] = total
        entry["watch_order"] = [s["name"] for s in seasons]
        print(f"  {slug}: {old} eps -> {total} eps", flush=True)

    # 2. 100 Girlfriends
    if fix_100_girlfriends(data):
        print(f"  {G100}: split S1/S2 (12 + 12)", flush=True)

    # 3. Safe site-wide sweep
    swept = sweep_phantom_single_seasons(data)
    print(f"  swept {len(swept)} single-season entries with phantom "
          f"episodes", flush=True)

    save(data)
    print(f"saved {DATA_FILE}", flush=True)


if __name__ == "__main__":
    main()
