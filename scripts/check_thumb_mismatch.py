#!/usr/bin/env python3
"""Cross-check aired-episode thumbs against the matched TVmaze show.

Some cards carry episode stills from a DIFFERENT show (a wrong TVmaze match
made by an older enrichment pass). Those wrong URLs are not shared with any
other card, so the shared-URL contamination check can't see them. This script
re-derives the TVmaze show + season the same way enrich_airing does (title
search + exact episode-name matches) and flags every aired episode whose thumb
does not match that show's still for the same episode number.

Usage:
    python3 scripts/check_thumb_mismatch.py            # scan + print
    python3 scripts/check_thumb_mismatch.py --fix      # replace wrong thumbs
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from scripts.enrich_airing import (  # noqa: E402
    DATA_FILE,
    _search_tvmaze,
    _tvmaze_episodes,
    _norm,
    _global_number,
    _hd_url,
    _tvmaze_ep_image,
    _pick_tvmaze_season,
    TVMAZE_ALIASES,
    load_json,
    save_json,
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true")
    args = ap.parse_args()

    data = load_json(DATA_FILE)
    mismatches = []
    checked = 0

    for slug, entry in data.items():
        if entry.get("status") not in ("Ongoing", "Upcoming"):
            continue
        aired = (entry.get("next_episode") or 0) - 1
        if aired <= 0:
            continue
        has_thumb = any(
            ep.get("thumb")
            for s in (entry.get("seasons") or [])
            for ep in (s.get("episodes") or [])
            if _global_number(entry.get("seasons") or [], 0, ep.get("number") or 0) <= aired
        )
        if not has_thumb:
            continue

        sid = _search_tvmaze(entry.get("title") or slug, entry.get("release") or "")
        if not sid:
            sid = TVMAZE_ALIASES.get(slug)
        if not sid:
            continue
        eps = _tvmaze_episodes(sid)
        if not eps:
            continue
        checked += 1

        by_season = {}
        for e in eps:
            by_season.setdefault(e.get("season"), []).append(e)

        named_hits = []
        for s in entry.get("seasons") or []:
            for ep in s.get("episodes") or []:
                t = ep.get("title")
                if not t:
                    continue
                nt = _norm(t)
                if not nt:
                    continue
                for tseason, teps in by_season.items():
                    for te in teps:
                        if nt == _norm(te.get("name") or ""):
                            named_hits.append(tseason)
                            break

        tseason = _pick_tvmaze_season(
            by_season, slug, named_hits, our_seasons=len(entry.get("seasons") or [])
        )
        if tseason is None:
            continue
        tvm = by_season.get(tseason) or []
        by_num = {e.get("number"): e for e in tvm}

        for si, s in enumerate(entry.get("seasons") or []):
            for ep in s.get("episodes") or []:
                if _global_number(entry.get("seasons") or [], si, ep.get("number") or 0) > aired:
                    continue
                cur = ep.get("thumb")
                if not cur:
                    continue
                te = by_num.get(ep.get("number"))
                if not te:
                    continue
                want = _tvmaze_ep_image(te)
                if want and _hd_url(cur) != want:
                    mismatches.append({
                        "slug": slug,
                        "title": entry.get("title"),
                        "episode": ep.get("number"),
                        "ep_title": ep.get("title"),
                        "tvmaze_show": sid,
                        "tvmaze_season": tseason,
                        "had": cur,
                        "want": want,
                    })

    print(f"checked {checked} running shows | mismatches: {len(mismatches)}")
    save_json(os.path.join(ROOT, "anime_thumb_mismatch.json"), mismatches)
    for m in mismatches:
        print(f"  {m['title'][:45]:47} ep {m['episode']:3} | {m['had'][-55:]} -> {m['want'][-55:]}")

    if not args.fix:
        return 0

    fixed = 0
    removed = 0
    for m in mismatches:
        ep = None
        for s in data[m["slug"]].get("seasons") or []:
            for e in s.get("episodes") or []:
                if e.get("number") == m["episode"]:
                    ep = e
        if ep is None:
            continue
        if m["want"]:
            ep["thumb"] = m["want"]
            fixed += 1
        else:
            # No still on TVmaze for this episode: drop the wrong image so
            # the official-poster fallback shows instead.
            ep.pop("thumb", None)
            removed += 1
    if fixed or removed:
        save_json(DATA_FILE, data)
    print(f"FIXED {fixed} thumbs, REMOVED {removed} wrong thumbs (no TVmaze still)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
