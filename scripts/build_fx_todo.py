#!/usr/bin/env python3
"""Build a fresh re-match todo: fixtodo slugs still pending + match_todo-only
slugs not yet covered by any fx/m cache. Writes anime_ep_thumbs_fx_todo.json.
Read-only apart from writing the new todo file."""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    fix = load("anime_ep_thumbs_fixtodo.json") or []
    mt = load("anime_ep_thumbs_match_todo.json") or []
    data = load("anime_data.json") or {}

    # Slugs already covered (matched or errored) by fx/m caches
    covered = set()
    for f in sorted(glob.glob("anime_ep_thumbs_fx*.json")) + \
             sorted(glob.glob("anime_ep_thumbs_m*.json")) + \
             sorted(glob.glob("anime_ep_thumbs_ma*.json")):
        if "todo" in os.path.basename(f):
            continue
        d = load(f)
        if not isinstance(d, dict):
            continue
        covered.update(d.keys())

    todo = []
    seen = set()
    for slug in list(fix) + [s for s in mt if s not in set(fix)]:
        if slug in seen or slug in covered:
            continue
        seen.add(slug)
        if slug in data:  # keep only slugs that exist in the catalog
            todo.append(slug)

    out = "anime_ep_thumbs_fx_todo.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(todo, f)
    print(f"FX_TODO: {len(todo)} slugs -> {out}")


if __name__ == "__main__":
    main()
