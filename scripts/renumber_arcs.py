#!/usr/bin/env python3
"""Make episode numbering continuous across arcs/seasons for the rebuilt
long-runner cards.

Arc-split cards (DBZ, One Piece, Bleach, Fairy Tail, Hitman Reborn, Dragon
Ball Kai) and the year-season kids' cards (Doraemon, Shin-chan, Chiikawa,
Beyblade X, Pokémon Horizons) were rebuilt with per-season episode numbers,
so every season restarted at "Episode 1". These shows number their episodes
globally, so renumber them continuously: the Saiyan Saga ends at ep 35 and
the Namek Saga starts at ep 36, etc.

Usage:
    python3 scripts/renumber_arcs.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.enrich_airing import (  # noqa: E402
    DATA_FILE,
    load_json,
    save_json,
)

CARDS = [
    # Arc-split story shows.
    "dragon-ball-z",
    "dragon-ball-super",
    "one-piece",
    "bleach",
    "fairy-tail",
    "hitman-reborn",
    # Dragon Ball Kai (+ Final Chapters continues the same global count).
    "dragon-ball-z-kai",
    "dragon-ball-z-kai-the-final-chapters",
    # Kids'/cartoon long-runners rebuilt into year seasons.
    "doraemon-2005",
    "shin-chan",
    "chiikawa",
    "beyblade-x",
    "pok-mon-horizons-the-series",
]


def main():
    data = load_json(DATA_FILE)
    for slug in CARDS:
        entry = data.get(slug)
        if not entry:
            print(f"SKIP {slug}: not in catalog")
            continue
        n = 0
        for s in entry.get("seasons") or []:
            for ep in s.get("episodes") or []:
                n += 1
                ep["number"] = n
        print(f"{entry['title']}: {len(entry.get('seasons') or [])} seasons, "
              f"{n} eps, continuous numbering")
    save_json(DATA_FILE, data)
    print("DONE")


if __name__ == "__main__":
    sys.exit(main())
