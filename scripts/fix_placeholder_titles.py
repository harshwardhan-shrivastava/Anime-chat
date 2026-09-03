#!/usr/bin/env python3
"""Replace placeholder "Season N - Episode N" episode titles with the real
TVmaze episode names.

A past enrichment pass wrote generated labels ("Season 1 - Episode 1",
"Season 2 - Episode 13"...) as episode titles for shows whose episode-name
source was missing at the time (Fullmetal Alchemist: Brotherhood, Death Note,
Monster, Kuroko, Hajime no Ippo, Soul Eater...). The real names exist on
TVmaze today, so this pass re-fetches each affected show's episode list and
swaps the placeholder titles for the real names.

Safety rules:
  * Only placeholder titles are touched (regex below). Real episode names
    that happen to exist are never overwritten.
  * A title is only applied when the fetched TVmaze name is itself a real
    name (not None / "Episode N" / "TBC" style).
  * A show is only edited when its episode layout lines up with TVmaze
    unambiguously: identical total counts (flattened order), a single
    bucket whose numbering maps 1:1 onto the flattened TVmaze order, or
    per-bucket number-set equality against a TVmaze season.
  * Movies/specials/TBC/unreleased entries and multi-show franken-cards that
    don't line up are skipped untouched.

Usage:
    python3 scripts/fix_placeholder_titles.py            # whole catalog
    python3 scripts/fix_placeholder_titles.py --limit a,b,c
"""

import argparse
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from enrich_ep_thumbnails import (  # noqa: E402
    _get_json,
    load_json,
    save_json,
    search_series,
    title_is_real,
    API,
)

DATA_FILE = os.path.join(ROOT, "anime_data.json")

# "Season 1 - Episode 1", "S2 - Episode 3", "Season1-Episode 4", "Episode 5"
JUNK_RE = re.compile(
    r"(?:^|[\-\s])(?:Season|S)\s*\d+\s*-\s*Episode\s*\d+$|^Episode\s*\d+$",
    re.I,
)
SEASON_JUNK_RE = re.compile(
    r"(?:^|[\-\s])(?:Season|S)\s*\d+\s*[-–—]?\s*Episode\s*\d+$", re.I
)

SLEEP = 0.35


def flat_episodes(entry):
    """All catalog episodes of an entry in display order."""
    out = []
    for season in entry.get("seasons") or []:
        eps = sorted(
            (ep for ep in (season.get("episodes") or []) if isinstance(ep, dict)),
            key=lambda e: (e.get("number") if isinstance(e.get("number"), int) else 0),
        )
        out.append((season, eps))
    return out


def tv_episodes(show_id):
    """TVmaze episodes (no specials), air-order sorted."""
    eps = _get_json(f"{API}/shows/{show_id}/episodes")
    if not eps:
        return []
    eps = [e for e in eps if (e.get("season") or 0) != 0]
    eps.sort(key=lambda e: (e.get("airdate") or "9999", e.get("season") or 0,
                            e.get("number") or 0))
    return eps


def find_show(entry):
    """Best TVmaze show id for an entry, or None."""
    title = (entry.get("title") or "").strip()
    if not title:
        return None
    for q in (title, re.sub(r"\s*[-:]\s*.*$", "", title).strip()):
        if len(q) < 4:
            continue
        sid, score = search_series(q, entry.get("release") or "")
        if sid and score >= 0.5:
            return sid
        time.sleep(SLEEP)
    return None


def build_name_map(entry, show_eps):
    """Return {ep_id: real_name} for every placeholder-titled episode whose
    position lines up with TVmaze. ep_id is (season_idx, number)."""
    buckets = flat_episodes(entry)
    all_eps = [ep for _, eps in buckets for ep in eps]
    if not all_eps or not show_eps:
        return {}

    tv_names = [e.get("name") for e in show_eps]
    tv_total = len(tv_names)
    cat_total = len(all_eps)

    # Strategy 1: identical totals -> concatenated display order matches the
    # TVmaze air order (covers single-season shows AND continuous-numbered
    # shows like FMA:B / Death Note that TVmaze keeps in one season).
    if tv_total == cat_total:
        order = [(b_idx, ep) for b_idx, (_, eps) in enumerate(buckets)
                 for ep in eps]
        return {
            (b_idx, ep.get("number")): tv_names[pos]
            for pos, (b_idx, ep) in enumerate(order)
            if is_placeholder(ep.get("title")) and is_real_name(tv_names[pos])
        }

    # Strategy 2: single bucket with 1..N global numbering aligned to the
    # flattened TVmaze order (some long-runners whose TVmaze entry splits
    # seasons while the catalog keeps one continuous list).
    if len(buckets) == 1:
        _, eps = buckets[0]
        nums = [ep.get("number") for ep in eps
                if isinstance(ep.get("number"), int)]
        if nums and min(nums) == 1 and max(nums) <= tv_total:
            out = {}
            for ep in eps:
                n = ep.get("number")
                if isinstance(n, int) and is_placeholder(ep.get("title")) \
                        and 1 <= n <= tv_total and is_real_name(tv_names[n - 1]):
                    out[(0, n)] = tv_names[n - 1]
            return out
        return {}

    # Strategy 3: multi-bucket cards whose buckets match TVmaze seasons by
    # episode-number set (catalog buckets already track TVmaze seasons, e.g.
    # Fullmetal Alchemist 2003: S1 1-26 / S2 27-51).
    by_season = {}
    for e in show_eps:
        by_season.setdefault(e.get("season"), []).append(e)
    out = {}
    for b_idx, (_, eps) in enumerate(buckets):
        if not eps:
            continue
        nums = {ep.get("number") for ep in eps if isinstance(ep.get("number"), int)}
        for s_eps in by_season.values():
            s_nums = {e.get("number") for e in s_eps
                      if isinstance(e.get("number"), int)}
            if nums and nums == s_nums:
                names = {e.get("number"): e.get("name") for e in s_eps}
                for ep in eps:
                    n = ep.get("number")
                    if is_placeholder(ep.get("title")) and \
                            is_real_name(names.get(n)):
                        out[(b_idx, n)] = names[n]
                break
    return out


def is_placeholder(title):
    return bool(title) and bool(JUNK_RE.search(str(title).strip()))


def is_real_name(name):
    return bool(name) and title_is_real(str(name)) and \
        not JUNK_RE.search(str(name).strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", help="comma-separated slugs (debug)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    data = load_json(DATA_FILE)
    limit = {s.strip() for s in (args.limit or "").split(",") if s.strip()}

    targets = []
    for slug, entry in data.items():
        if limit and slug not in limit:
            continue
        if not (entry.get("seasons") or []):
            continue
        junk = any(
            SEASON_JUNK_RE.search(str(ep.get("title") or ""))
            for s in entry["seasons"] for ep in (s.get("episodes") or [])
        )
        if junk:
            targets.append(slug)
    if not limit:
        # Keep the whole-catalog sweep scoped to shows that actually carry
        # the generated "Season N - Episode N" labels (plus their plain
        # "Episode N" placeholders, which the same pass can fill).
        targets = [s for s in targets if s]
    print(f"{len(targets)} shows carry placeholder 'Season N - Episode N' "
          f"titles", flush=True)

    total_replaced = 0
    fixed_shows = 0
    for slug in targets:
        entry = data.get(slug)
        if entry is None:
            continue
        show_id = find_show(entry)
        time.sleep(SLEEP)
        if not show_id:
            print(f"  - {slug}: no TVmaze match, skipped", flush=True)
            continue
        show_eps = tv_episodes(show_id)
        time.sleep(SLEEP)
        if not show_eps:
            print(f"  - {slug}: no TVmaze episodes, skipped", flush=True)
            continue
        name_map = build_name_map(entry, show_eps)
        if not name_map:
            print(f"  - {slug}: layout mismatch, skipped", flush=True)
            continue
        changed = 0
        for b_idx, season in enumerate(entry["seasons"]):
            for ep in season.get("episodes") or []:
                n = ep.get("number")
                new = name_map.get((b_idx, n))
                if new is not None and \
                        SEASON_JUNK_RE.search(str(ep.get("title") or "")):
                    ep["title"] = new
                    changed += 1
        if changed:
            if not args.dry_run:
                pass  # data object already mutated; saved once below
            fixed_shows += 1
            total_replaced += changed
            print(f"  + {slug}: {changed} titles fixed", flush=True)

    if not args.dry_run:
        save_json(DATA_FILE, data)
    print(f"done: {fixed_shows} shows, {total_replaced} titles replaced"
          f"{' (dry run)' if args.dry_run else ''}", flush=True)


if __name__ == "__main__":
    main()
