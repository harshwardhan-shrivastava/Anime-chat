#!/usr/bin/env python3
"""Repair auto-chunked fake seasons in anime_data.json.

A previous enrichment pass chunked every episode list that lacked real
season data into uniform ~26-episode "Season N" buckets (Slam Dunk became
Season 1-4 of 26/26/26/23; Detective Conan got 39 "seasons"). This script
detects those chunk artifacts and repairs them:

  * Only trust the local TVmaze thumb index (anime_ep_thumbs_w*.json) when
    it shows a COMPLETE run of real seasons STARTING at season 1 (that is
    the proof the index uses real TVmaze numbering). If a complete run
    covers >=50% of the card's episodes, rebuild "Season N" boundaries
    from the real run sizes (+ remainder as the last season).
  * Otherwise (no real season signal: Slam Dunk, Conan, Saiki, MHA with a
    season-1 gap in its index...) collapse to a single "Episodes" card,
    matching how Bleach/DBZ-style no-season shows are stored.
  * Season-sibling cards (slug ends in -season-N) are repaired to hold
    ONLY their own season: first try mirroring the parent's (now-fixed)
    real split, then fall back to the sibling's own thumb index (a
    season-sibling index starts at the sibling's season, so its first
    complete run is its real size). A rent-a-girlfriend-season-2 page
    therefore shows exactly the 12 eps of season 2, not the whole
    show's fake 26/15 chunks.

Run with --from-backup to start from the pristine anime_data.json.bak-<date>
(the first buggy version of this script left some cards half-mutated); the
backup itself is never modified.
"""
import json
import glob
import os
import re
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

DATA = "anime_data.json"
BACKUP = DATA + ".bak-%s" % date.today().strftime("%Y%m%d")
SIB_RE = re.compile(r"-season-(\d+)$")
GEN_NAME_RE = re.compile(r"^Season \d+$")
MAX_TV_SEASON = 25  # real season numbers; Conan's year-keys (1996:1) are excluded
MIN_COVERAGE = 0.5  # thumb run must cover >=50% of the card's episodes


def is_chunk_artifact(seasons):
    """Seasons look like auto-chunks: generic 'Season N' names and uniform
    large buckets (~26 eps) with at most one short tail / a 26+small split."""
    if not seasons or len(seasons) < 2:
        return False
    names = [str(s.get("name") or "").strip() for s in seasons]
    if not all(GEN_NAME_RE.match(n) for n in names):
        return False
    counts = [len(s.get("episodes") or []) for s in seasons]
    if max(counts) < 24:
        return False
    if len(seasons) == 2:
        a, b = counts[0], counts[1]
        return a >= 24 and b <= a - 8
    return len(set(counts)) <= 2


def load_thumb_index():
    merged = {}
    for f in sorted(glob.glob("anime_ep_thumbs_w*.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        for slug, mapping in d.items():
            if not isinstance(mapping, dict):
                continue
            out = merged.setdefault(slug, {})
            for k, v in mapping.items():
                out.setdefault(k, v)
    return merged


def parse_tv_seasons(thumbs):
    """{tv_season: set(episode numbers)} from 's:e' keys."""
    per = {}
    for k in (thumbs or {}):
        try:
            s, e = k.split(":", 1)
            s, e = int(s), int(e)
        except (ValueError, TypeError):
            continue
        if 1 <= s <= MAX_TV_SEASON and 1 <= e <= 500:
            per.setdefault(s, set()).add(e)
    return per


def complete_run_sizes(per, min_run=4):
    """Sizes of the complete TVmaze run that starts at season 1.

    A season is complete when every episode 1..max is present. The run must
    begin at season 1 -- an index whose first key is 2 (like MHA) is not
    using real TVmaze numbering, so its boundaries are untrustworthy and we
    return [] (which collapses the card) rather than guess.
    """
    if 1 not in per:
        return []
    return complete_run_from_first_key(per)


def complete_run_from_first_key(per, min_run=4):
    """Sizes of the complete run of consecutive seasons starting at the
    index's FIRST key. Season-sibling cards carry an index that starts at
    the sibling's own season (a -season-2 card's thumb keys begin at 2),
    so the first complete run is that sibling's real episode count.
    """
    if not per:
        return []
    start = min(per)
    sizes = []
    for s in sorted(per):
        if s != start + len(sizes):  # gap in the numbering -> run ends
            break
        eps = per[s]
        mx = max(eps)
        if mx < min_run or any(e not in eps for e in range(1, mx + 1)):
            break
        sizes.append(mx)
    return sizes


def main():
    src = BACKUP if "--from-backup" in sys.argv and os.path.exists(BACKUP) else DATA
    data = json.load(open(src))
    thumbs = load_thumb_index()
    stats = {"collapsed": 0, "rebuilt": 0, "siblings": 0, "skipped_empty": 0, "errors": []}
    changed = {}

    def collapse(entry, all_eps):
        entry["seasons"] = [{"name": "Episodes", "episodes": all_eps}]
        entry["watch_order"] = ["Episodes"]
        entry["total_episodes"] = len(all_eps)

    # ---- Phase 1: fix non-sibling cards with chunk artifacts ----
    for slug, entry in data.items():
        if SIB_RE.search(slug):
            continue
        seasons = entry.get("seasons")
        if not seasons or not is_chunk_artifact(seasons):
            continue
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        total = len(all_eps)
        if not total:
            continue
        per = parse_tv_seasons(thumbs.get(slug, {}))
        sizes = complete_run_sizes(per)
        coverage = (sum(sizes) / total) if sizes else 0.0
        if sizes and coverage >= MIN_COVERAGE and sum(sizes) < total and len(sizes) >= 2:
            # Genuinely seasonal: real boundaries + remainder tail.
            parts, used = [], 0
            for c in sizes:
                if used + c > total:
                    break
                parts.append(all_eps[used:used + c])
                used += c
            if used < total:
                parts.append(all_eps[used:])
            if len(parts) >= 2:
                entry["seasons"] = [
                    {"name": "Season %d" % i, "episodes": p}
                    for i, p in enumerate(parts, 1)
                ]
                entry["watch_order"] = ["Season %d" % i for i in range(1, len(parts) + 1)]
                stats["rebuilt"] += 1
            else:
                collapse(entry, all_eps)
                stats["collapsed"] += 1
        else:
            collapse(entry, all_eps)
            stats["collapsed"] += 1
        changed[slug] = (entry.get("title"),
                         [(s["name"], len(s["episodes"])) for s in entry["seasons"]])

    # ---- Phase 2: season-sibling cards keep only their own season -------
    for slug, entry in data.items():
        m = SIB_RE.search(slug)
        if not m:
            continue
        n = int(m.group(1))
        seasons = entry.get("seasons") or []
        if not seasons:
            stats["skipped_empty"] += 1
            continue
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        if not all_eps:
            continue

        # Resolve the sibling's true episode count, best source first:
        # 1) the parent's fixed split (gintama, apothecary diaries...)
        # 2) the sibling's own thumb index: its first complete TVmaze run
        #    (a season-sibling index starts at the sibling's season, e.g.
        #    -season-2 thumb keys start at 2) is its own real size
        # 3) nothing trustworthy -> keep the whole list flat
        keep = None
        parent = data.get(SIB_RE.sub("", slug))
        parent_seasons = (parent.get("seasons") or []) if parent else []
        if parent_seasons and n <= len(parent_seasons):
            keep = parent_seasons[n - 1].get("episodes") or []
        if not keep:
            per = parse_tv_seasons(thumbs.get(slug, {}))
            sizes = complete_run_from_first_key(per)
            if sizes and sizes[0] >= 6 and sizes[0] <= int(0.8 * len(all_eps)):
                keep = all_eps[: sizes[0]]
        if not keep:
            # Half-mutated or still-generic sibling with no evidence:
            if len(seasons) > 1:
                collapse(entry, all_eps)
                stats["collapsed"] += 1
                changed[slug] = (entry.get("title"), [("Episodes", len(all_eps))])
            continue

        if len(seasons) == 1 and (seasons[0].get("episodes") or []) == keep:
            entry["total_episodes"] = len(keep)  # fix stale counters only
            continue
        entry["seasons"] = [{"name": "Episodes", "episodes": keep}]
        entry["watch_order"] = ["Episodes"]
        entry["total_episodes"] = len(keep)
        stats["siblings"] += 1
        changed[slug] = (entry.get("title"), [("Episodes", len(keep))])

    # ---- Phase 3: sibling-parent mains keep only their own season -------
    # The catalog models each season as its own card (rent-a-girlfriend-
    # season-2, attack-on-titan-season-3, ...), so the parent's page should
    # show a single flat card with the FIRST season's episodes -- like the
    # approved Slime layout -- instead of a Seasons grid that duplicates
    # the sibling cards. Only collapse when EVERY season after the first
    # already has its own sibling card, so no content is ever hidden
    # (AoT's "The Final Season" has no sibling -> its grid stays).
    mains_collapsed = 0
    for slug, entry in data.items():
        if SIB_RE.search(slug):
            continue
        seasons = entry.get("seasons") or []
        if len(seasons) < 2:
            continue
        kids = {SIB_RE.sub("", k) for k in data if SIB_RE.search(k)}
        if slug not in kids:
            continue  # no season-sibling cards at all -> grid is the nav
        first = seasons[0].get("episodes") or []
        if not first:
            continue
        covered = True
        for s in seasons[1:]:
            name = str(s.get("name") or "").strip()
            m = re.match(r"^Season (\d+)$", name)
            if not m or (slug + "-season-" + m.group(1)) not in data:
                covered = False  # named arc or missing sibling -> keep grid
                break
        if not covered:
            continue
        entry["seasons"] = [{"name": "Episodes", "episodes": first}]
        entry["watch_order"] = ["Episodes"]
        entry["total_episodes"] = len(first)
        mains_collapsed += 1
        changed[slug] = (entry.get("title"), [("Episodes", len(first))])

    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("collapsed: %d | rebuilt series: %d | sibling-mirrored: %d | mains-collapsed-to-s1: %d | skipped-empty: %d"
          % (stats["collapsed"], stats["rebuilt"], stats["siblings"], mains_collapsed, stats["skipped_empty"]))
    for slug in ["slam-dunk", "rent-a-girlfriend", "rent-a-girlfriend-season-2",
                 "rent-a-girlfriend-season-3", "rent-a-girlfriend-season-4",
                 "my-hero-academia", "my-hero-academia-season-2",
                 "my-hero-academia-season-7", "the-disastrous-life-of-saiki-k",
                 "detective-conan", "spy-x-family-season-3",
                 "pretty-guardian-sailor-moon-crystal-season-3"]:
        print("  %-42s -> %s" % (slug, changed.get(slug, "UNCHANGED")))


if __name__ == "__main__":
    main()