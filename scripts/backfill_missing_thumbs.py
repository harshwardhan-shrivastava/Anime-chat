#!/usr/bin/env python3
"""Backfill missing episode thumbnails for shows whose thumb set is stale.

TVmaze publishes episode stills as episodes air, so a catalog snapshot taken
earlier can leave recent shows with a partial thumb set (some episodes have
a real still, later ones fall back to the poster). This script re-runs the
provenance pipeline (scripts/enrich_ep_thumbnails.fetch_thumbs_for, TVmaze
with a Kitsu fallback) for a targeted list of slugs and merges the results
back into anime_data.json.

Targeting rules:
  --slugs a,b,c   explicit slugs (always included)
  --since 2024    auto-target: shows that are missing at least one episode
                  thumb but already have at least one (proves the source has
                  this show) and whose release year >= this value
  --all           auto-target every spotty show regardless of year

Matching: returned keys are "season:number". An episode matches if the
card has a season whose 1-based index equals the returned season, else if
the card has exactly one season, by episode number. A returned thumb only
overwrites an existing one when the URL differs (fixes duplicates).
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from enrich_ep_thumbnails import fetch_thumbs_for, load_json, save_json  # noqa: E402

DATA_FILE = os.path.join(ROOT, "anime_data.json")


def spotty_slugs(cat, since=None):
    out = []
    for slug, e in cat.items():
        eps = [ep for s in (e.get("seasons") or []) for ep in (s.get("episodes") or [])]
        if not eps:
            continue
        has = sum(1 for ep in eps if ep.get("thumb"))
        missing = len(eps) - has
        if 0 < missing < len(eps):
            year = e.get("release") or ""
            if since and year < since:
                continue
            out.append((slug, e.get("title") or slug, year))
    return out


def apply_results(cat, slug, thumbs):
    """Merge a {season:number: url} map into the catalog entry for slug.
    Returns (filled, fixed, skipped) counts."""
    entry = cat.get(slug)
    if not entry:
        return 0, 0, 0
    seasons = entry.get("seasons") or []
    if not seasons:
        return 0, 0, 0
    filled = fixed = skipped = 0
    # Group the returned thumbs by TVmaze season number.
    by_season = {}
    for key, url in (thumbs or {}).items():
        try:
            s, n = key.split(":")
            by_season.setdefault(int(s), {})[int(n)] = url
        except (ValueError, AttributeError):
            continue
    for tvmaze_season, num_urls in by_season.items():
        # Season index match (card season 1 == TVmaze season 1), else the
        # single-season card adopts any season's numbers by episode number.
        targets = None
        for si, s in enumerate(seasons, start=1):
            if si == tvmaze_season:
                targets = s.get("episodes") or []
                break
        if targets is None and len(seasons) == 1:
            targets = seasons[0].get("episodes") or []
        if not targets:
            continue
        for ep in targets:
            num = ep.get("number")
            url = num_urls.get(num)
            if not url:
                continue
            cur = ep.get("thumb")
            if cur == url:
                skipped += 1
            elif cur:
                ep["thumb"] = url
                fixed += 1
            else:
                ep["thumb"] = url
                filled += 1
    return filled, fixed, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", default="", help="comma-separated explicit slugs")
    ap.add_argument("--since", default=None, help="auto-target year threshold (e.g. 2024)")
    ap.add_argument("--all", action="store_true", help="auto-target every spotty show")
    args = ap.parse_args()

    cat = load_json(DATA_FILE)
    targets = []
    explicit = [s.strip() for s in args.slugs.split(",") if s.strip()]
    for s in explicit:
        if s in cat:
            targets.append((s, cat[s].get("title") or s, cat[s].get("release") or ""))
    if args.all or args.since:
        since = None if args.all else args.since
        auto = spotty_slugs(cat, since=since)
        known = {t[0] for t in targets}
        targets += [a for a in auto if a[0] not in known]

    if not targets:
        print("nothing to do", flush=True)
        return

    # Safety copy before any writes.
    backup = DATA_FILE + ".bak"
    if not os.path.exists(backup):
        save_json(backup, cat)
        print(f"backup written: {backup}", flush=True)

    t0 = time.time()
    for i, (slug, title, year) in enumerate(targets, start=1):
        thumbs = fetch_thumbs_for(slug, title, year)
        filled, fixed, skipped = apply_results(cat, slug, thumbs)
        if i % 5 == 0 or i == len(targets):
            save_json(DATA_FILE, cat)  # checkpoint so a kill never loses it all
            print(
                f"[{i}/{len(targets)}] {title[:50]:<50} "
                f"filled={filled} fixed={fixed} skipped={skipped} "
                f"({int(time.time() - t0)}s)",
                flush=True,
            )
    save_json(DATA_FILE, cat)
    print(f"DONE {len(targets)} shows in {int(time.time() - t0)}s", flush=True)


if __name__ == "__main__":
    main()