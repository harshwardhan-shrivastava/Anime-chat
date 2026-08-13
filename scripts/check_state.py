#!/usr/bin/env python3
"""Inspect the current thumbnail-fix pipeline state (read-only)."""
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
    fix = fix if isinstance(fix, list) else []
    print("fixtodo total:", len(fix))

    # Every worker cache family -> slugs with usable results
    covered = {}  # slug -> source file
    errors = {}   # slug -> error type
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
                errors.setdefault(k, v["__error__"])
            elif isinstance(v, dict) and v:
                covered.setdefault(k, os.path.basename(f))

    fix_set = set(fix)
    done = fix_set & set(covered)
    err = fix_set & set(errors)
    still = fix_set - set(covered) - set(errors)
    print("fixtodo matched with thumbs:", len(done))
    print("fixtodo errored:", len(err), sorted(errors.values()))
    print("fixtodo still pending:", len(still))

    from collections import Counter
    print("error breakdown:", dict(Counter(errors[k] for k in err)))

    # What do fx caches contain that is NOT in fixtodo (stale)?
    extra = set(covered) | set(errors)
    extra -= fix_set
    print("cache slugs not in fixtodo:", len(extra))

    # Check a couple of the user's priority shows directly
    data = load("anime_data.json")
    for key in ["you-and-i-are-polar-opposites",
                "you-and-i-are-polar-opposites-season-2",
                "boku-to-kimi-no-taisetsu-na-hanashi",
                "100-girlfriends-who-really-really-really-really-really-love-you-season-2",
                "100-girlfriends-season-3",
                "renai-flops", "the-100-girlfriends-who-really-really-really-really-really-love-you-season-3",
                "reincarnated-as-a-slime-season-3", "that-time-i-got-reincarnated-as-a-slime-season-3",
                "that-time-i-got-reincarnated-as-a-slime-season-4"]:
        e = data.get(key)
        if e is None:
            continue
        total = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [])
        with_thumbs = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [] if ep.get("thumb"))
        named = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [] if ep.get("title"))
        print(f"SHOW {key}: eps={total} thumbs={with_thumbs} named={named} status={e.get('status')}")
        for si, s in enumerate(e.get("seasons") or [], start=1):
            for ep in (s.get("episodes") or [])[:60]:
                t = ep.get("title") or ""
                th = ep.get("thumb") or ""
                hd = "original_untouched" in th
                mark = "HD" if hd else ("thumb" if th else "--")
                print(f"   S{si} Ep{ep.get('number')}: {mark} | {t[:60]}")


if __name__ == "__main__":
    main()
