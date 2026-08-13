#!/usr/bin/env python3
"""Deep-dive: what m0 contains, pending slugs, applied state, match_todo."""
import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"__load_error__": str(e)}


def main():
    fix = load("anime_ep_thumbs_fixtodo.json")
    if not isinstance(fix, list):
        fix = []
    fix_set = set(fix)

    # What is m0?
    m0 = load("anime_ep_thumbs_m0.json")
    print("m0 type:", type(m0).__name__, "| keys:", len(m0) if isinstance(m0, dict) else "?")
    if isinstance(m0, dict):
        good = [k for k, v in m0.items() if isinstance(v, dict) and v and "__error__" not in v]
        errs = [k for k, v in m0.items() if isinstance(v, dict) and "__error__" in v]
        print("m0 good:", len(good), "errors:", len(errs))
        print("m0 sample good:", good[:5])
        # how many m0 slugs are in fixtodo?
        print("m0 good slugs in fixtodo:", len(set(good) & fix_set))

    # Pending: fixtodo slugs not in any fx/m cache
    covered = {}
    errs_by = {}
    for f in sorted(glob.glob("anime_ep_thumbs_fx*.json")) + \
             sorted(glob.glob("anime_ep_thumbs_m*.json")) + \
             sorted(glob.glob("anime_ep_thumbs_ma*.json")):
        if "todo" in os.path.basename(f):
            continue
        d = load(f)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, dict) and "__error__" in v:
                errs_by.setdefault(k, v["__error__"])
            elif isinstance(v, dict) and v:
                covered.setdefault(k, os.path.basename(f))
    pending = sorted(fix_set - set(covered) - set(errs_by))
    print("PENDING slugs:", len(pending))
    print("first 40 pending:", pending[:40])
    print("last 40 pending:", pending[-40:])

    # Was the fx apply reflected in anime_data.json?
    data = load("anime_data.json")
    for slug in ["365-days-to-the-wedding", "12-sai-chiccha-na-mune-no-tokimeki-2"]:
        e = data.get(slug)
        if not e:
            print(f"applied? {slug}: NOT IN DATA")
            continue
        thumbs = [(s_, ep.get("number"), (ep.get("thumb") or "")[-45:])
                  for s_, s in enumerate(e.get("seasons") or [], 1)
                  for ep in s.get("episodes") or [] if ep.get("thumb")]
        print(f"applied? {slug}: {len(thumbs)} thumbs | sample {thumbs[:3]}")

    # match_todo content
    mt = load("anime_ep_thumbs_match_todo.json")
    if isinstance(mt, list):
        print("match_todo:", len(mt), "| first 10:", mt[:10])
        print("match_todo ∩ fixtodo:", len(set(mt) & fix_set))
        print("match_todo - fixtodo (new slugs):", len(set(mt) - fix_set), list(set(mt) - fix_set)[:10])
        print("fixtodo - match_todo:", len(fix_set - set(mt)))

    # Priority shows: are they in any todo?
    for s in ["you-and-i-are-polar-opposites-season-2", "the-100-girlfriends-who-really-really-really-really-really-love-you-season-3",
              "that-time-i-got-reincarnated-as-a-slime-season-3", "that-time-i-got-reincarnated-as-a-slime-season-4"]:
        print(f"todo membership {s}: fix={s in fix_set} match={s in (set(mt) if isinstance(mt, list) else set())} pending={'YES' if s in pending else 'no'}")


if __name__ == "__main__":
    main()
