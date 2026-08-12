#!/usr/bin/env python3
"""Rebuild long-running story shows into accurate story-arc chunks.

Long shows (DBZ, One Piece, Bleach, ...) were chunked along arbitrary
TVmaze/season boundaries with generic "Season N" labels. This script
re-chunks them along the canonical story arcs (verified against episode
titles, TVmaze season data and TMDB's arc episode-group data) so each
section shows the real arc name and episode count.

The card's episodes are kept in their existing global aired order (which
matches TVmaze's flat episode order) and only re-sliced + renumbered per
arc, so titles/thumbs are preserved.

Usage:
    python3 scripts/rebuild_arcs.py
"""
import os
import sys

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from scripts.enrich_airing import (  # noqa: E402
    DATA_FILE,
    load_json,
    save_json,
)

# slug -> list of (arc name, first global episode, last global episode)
# Boundaries verified against TVmaze episode titles + TMDB episode groups.
ARCS = {
    "dragon-ball-z": [
        ("Saiyan Saga", 1, 35),
        ("Namek Saga", 36, 107),
        ("Cell Saga", 108, 194),
        ("Majin Buu Saga", 195, 291),
    ],
    "dragon-ball-super": [
        ("Battle of Gods Saga", 1, 14),
        ("Resurrection 'F' Saga", 15, 27),
        ("Universe 6 Saga", 28, 46),
        ("'Future' Trunks Saga", 47, 76),
        ("Universe Survival Saga", 77, 131),
    ],
    "one-piece": [
        ("East Blue Saga", 1, 61),
        ("Alabasta Saga", 62, 130),
        ("Sky Island Saga", 131, 195),
        ("Water 7 Saga", 196, 325),
        ("Thriller Bark Saga", 326, 381),
        ("Summit War Saga", 382, 516),
        ("Fish-Man Island Saga", 517, 574),
        ("Dressrosa Saga", 575, 746),
        ("Zou Saga", 747, 784),
        ("Whole Cake Island Saga", 785, 854),
        ("Reverie Saga", 855, 889),
        ("Wano Country Saga", 890, 1084),
        ("Egghead Arc", 1085, 1122),
        ("Elbaph Arc", 1123, 1181),
    ],
    "bleach": [
        ("Agent of the Shinigami Arc", 1, 20),
        ("Soul Society Arc", 21, 63),
        ("Bount Arc", 64, 109),
        ("Arrancar Arc", 110, 167),
        ("The New Captain Shūsuke Amagai Arc", 168, 189),
        ("Fake Karakura Town Arc", 190, 229),
        ("Zanpakutō Unknown Tales Arc", 230, 265),
        ("Fake Karakura Town Arc (Part 2)", 266, 316),
        ("Gotei 13 Invading Army Arc", 317, 342),
        ("The Lost Substitute Soul Reaper Arc", 343, 366),
    ],
    "fairy-tail": [
        ("Macao Arc", 1, 2),
        ("Daybreak Arc", 3, 4),
        ("Eisenwald Arc", 5, 10),
        ("Sub-Zero Emperor Lyon Arc", 11, 20),
        ("Phantom Lord Arc", 21, 29),
        ("Loke Arc", 30, 32),
        ("Tower of Heaven Arc", 33, 40),
        ("The Battle of Fairy Tail Arc", 41, 51),
        ("Oración Seis Arc", 52, 68),
        ("Daphne Arc", 69, 75),
        ("Edolas Arc", 76, 95),
        ("Tenrou Island Arc", 96, 122),
        ("X791 Arc", 123, 124),
        ("Key of the Starry Sky Arc", 125, 150),
        ("Grand Magic Games Arc", 151, 175),
    ],
    "hitman-reborn": [
        ("Daily Life Arc", 1, 73),
        ("Future Arc", 74, 141),
        ("Inheritance Succession Arc", 142, 157),
        ("The Choice Arc", 158, 177),
        ("Future Final Battle Arc", 178, 203),
    ],
}

# TVmaze show ids used only for the alignment sanity check.
TVMAZE_IDS = {
    "dragon-ball-z": 2103,
    "dragon-ball-super": 2368,
    "one-piece": 1505,
    "bleach": 1905,
    "fairy-tail": 2069,
    "hitman-reborn": 16151,
}


def _flat_tvmaze(sid):
    try:
        r = requests.get(f"https://api.tvmaze.com/shows/{sid}/episodes", timeout=30)
        if r.status_code == 200:
            return r.json() or []
    except Exception:
        pass
    return []


def _norm(t):
    return (t or "").strip().lower().replace("…", "...").replace("’", "'")


def main():
    data = load_json(DATA_FILE)

    for slug, arcs in ARCS.items():
        entry = data.get(slug)
        if not entry:
            print(f"SKIP {slug}: not in catalog")
            continue

        flat = []
        for s in entry.get("seasons") or []:
            for ep in s.get("episodes") or []:
                flat.append(ep)

        total = arcs[-1][2]
        if len(flat) != total:
            print(f"ERROR {slug}: card has {len(flat)} eps but arcs cover {total}")
            continue

        # Alignment sanity check against TVmaze flat order.
        tvm = _flat_tvmaze(TVMAZE_IDS[slug])
        if tvm:
            mismatches = 0
            for i, ep in enumerate(flat[: min(len(flat), len(tvm))]):
                card_t = _norm(ep.get("title"))
                tv_t = _norm(tvm[i].get("name"))
                if card_t and card_t != tv_t:
                    mismatches += 1
            if mismatches > 5:
                print(f"ERROR {slug}: {mismatches} title mismatches vs TVmaze "
                      f"(order may differ) — aborting this show")
                continue
            print(f"{slug}: alignment OK ({mismatches} title mismatches)")

        new_seasons = []
        for name, start, end in arcs:
            chunk = [dict(ep) for ep in flat[start - 1:end]]
            for i, ep in enumerate(chunk, 1):
                ep["number"] = i
            new_seasons.append({"name": name, "episodes": chunk})

        entry["seasons"] = new_seasons
        entry["watch_order"] = [name for name, _, _ in arcs]
        entry["total_episodes"] = len(flat)
        print(f"{entry['title']}: rebuilt into {len(arcs)} arcs "
              f"({', '.join(f'{n} ({e-s+1})' for n, s, e in arcs)})")

    save_json(DATA_FILE, data)
    print("DONE")


if __name__ == "__main__":
    sys.exit(main())
