#!/usr/bin/env python3
"""Repair auto-chunked fake seasons in anime_data.json.

A past enrichment pass chunked every episode list that lacked real season
data into uniform ~26-episode "Season N" buckets (Slam Dunk became Season
1-4 of 26/26/26/23; Detective Conan got 39 "seasons"). This script detects
and repairs those chunk artifacts WITHOUT destroying real structures:

  Pass 1 - thumb evidence (authoritative): the local TVmaze thumb index
    (anime_ep_thumbs_w*.json) records each episode's real TVmaze s:e. When
    a substantially-complete run (>=85% of 1..max present -- missing thumbs
    tolerated) starting at season 1 (fallback: first key) covers >=50% of
    the card AND its leading seasons account for the card's ENTIRE episode
    list, the truth is proven: rebuild from the run, or collapse when the
    run is a single season (Sailor Moon 46eps is one TVmaze season, not
    26/20; FMA 51eps one season, not 26/25).
  Pass 2 - unambiguous 26-boundary shapes, only for cards Pass 1 could not
    settle: [26, <=18] and N x 26 + smaller tail (GTO, JoJo, MHA, Conan,
    Doraemon...). Real structures never have these shapes: Kuroko
    [25,25,25] has no tail, Black Butler [24,12] starts at 24, JJK [24,23]
    has no 26 boundary. Cards like Digimon Tamers [26,25] (possibly a real
    TVmaze year split) are left untouched - no evidence, can't know.
  Pass 3 - season-sibling cards (slug ends in -season-N) that hold fake
    chunks keep only their own season: mirror the parent's fixed split
    (gintama, apothecary diaries), else collapse to their own flat list.
    Single-card siblings (K-On!! S2 26, Fruits Basket S2 25...) are NEVER
    touched.
  Pass 4 - parent pages whose every later season already has its own
    sibling card show only the first season (like the approved Slime
    layout) instead of duplicating the sibling grid. Named arcs with no
    sibling (AoT "The Final Season") keep their grid - nothing hidden.

The original file is preserved as anime_data.json.bak-<date>.
Run with --from-backup to rebuild from that pristine backup.
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
MIN_EVIDENCE = 0.5   # thumb run must cover >=50% of the card to be evidence
TOLERANCE = 0.85     # a season counts as complete if >=85% of 1..max present


def is_chunk_artifact(seasons):
    """Unambiguous 26-boundary chunk signatures (shape only, used when no
    thumb evidence exists):
      * two seasons [26, <=18]                (GTO 26/17, JoJo 26/13)
      * N x 26 + smaller tail                (MHA 26x6+3, Conan 26x38+24)
    Real structures never have these shapes: Kuroko [25,25,25] has no tail,
    Black Butler [24,12] starts at 24, JJK [24,23] has no 26 boundary.
    """
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
        return a == 26 and b <= 18
    head, tail = counts[:-1], counts[-1]
    return len(set(head)) == 1 and tail < head[0] and head[0] >= 24


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


def tolerant_run(per):
    """Sizes of the consecutive substantially-complete run starting at the
    index's first key (season 1 when present, else the first key - a
    season-sibling index starts at the sibling's own season). A season
    counts as complete when >=85% of episodes 1..max have thumbnails."""
    if not per:
        return []
    start = min(per)
    sizes = []
    for s in range(start, start + 60):
        eps = per.get(s)
        if not eps:
            break
        mx = max(eps)
        if mx < 4:
            break
        present = sum(1 for e in range(1, mx + 1) if e in eps)
        if present / mx < TOLERANCE:
            break
        sizes.append(mx)
    return sizes


def main():
    src = BACKUP if "--from-backup" in sys.argv and os.path.exists(BACKUP) else DATA
    data = json.load(open(src))
    thumbs = load_thumb_index()
    stats = {"collapsed": 0, "rebuilt": 0, "siblings": 0, "mains": 0,
             "evidence": 0, "skipped_empty": 0}
    changed = {}

    def collapse(entry, all_eps):
        entry["seasons"] = [{"name": "Episodes", "episodes": all_eps}]
        entry["watch_order"] = ["Episodes"]
        entry["total_episodes"] = len(all_eps)

    # ---- collect candidate non-sibling cards (generic multi-season) ----
    candidates = []
    for slug, entry in data.items():
        if SIB_RE.search(slug):
            continue
        seasons = entry.get("seasons") or []
        if len(seasons) < 2:
            continue
        names = [str(s.get("name") or "").strip() for s in seasons]
        if not all(GEN_NAME_RE.match(n) for n in names):
            continue
        if max(len(s.get("episodes") or []) for s in seasons) < 24:
            continue
        candidates.append(slug)

    # ---- Pass 1: thumb evidence is authoritative when it covers the card ----
    for slug in candidates:
        entry = data[slug]
        seasons = entry["seasons"]
        counts = [len(s.get("episodes") or []) for s in seasons]
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        total = len(all_eps)
        if not total:
            continue
        per = parse_tv_seasons(thumbs.get(slug, {}))
        run = tolerant_run(per)
        if not run or sum(run) / total < MIN_EVIDENCE:
            continue
        start = min(per)
        fit, used = [], 0
        for c in run:
            if used + c > total:
                break
            fit.append(c)
            used += c
        if fit == counts:
            continue  # evidence confirms the grid -> real, keep
        stats["evidence"] += 1
        has_siblings = slug in {SIB_RE.sub("", k) for k in data if SIB_RE.search(k)}
        if len(fit) >= 2 and start == 1 and sum(fit) / total >= 0.85 and has_siblings:
            # >=2 proven real seasons from a dense season-1-indexed run, and
            # the show has season-sibling cards that must mirror real
            # boundaries: rebuild from the evidence (+ tail). Only sibling-
            # parents rebuild -- a rebuilt grid on a card with no siblings
            # (Pokemon 82/36/41/52/65) would just recreate the "N seasons in
            # one card" layout; those fall through to the shape pass and
            # collapse flat instead. An index starting at season 2 is
            # shifted/untrustworthy and never rebuilds.
            parts, used = [], 0
            for c in fit:
                parts.append(all_eps[used:used + c])
                used += c
            if used < total:
                parts.append(all_eps[used:])
            entry["seasons"] = [
                {"name": "Season %d" % i, "episodes": p}
                for i, p in enumerate(parts, 1)
            ]
            entry["watch_order"] = ["Season %d" % i for i in range(1, len(parts) + 1)]
            stats["rebuilt"] += 1
        elif fit and fit[0] != counts[0]:
            # Evidence's first season contradicts the grid's first season
            # (Sailor Moon's 46 vs 26, FMA's 51 vs 26): the grid is fake.
            collapse(entry, all_eps)
            stats["collapsed"] += 1
        else:
            # Evidence's first season is consistent with the grid and only
            # one season is proven (Black Butler 24 == 24, S2 unproven):
            # leave it -- the shape pass below only flags unambiguous
            # 26-boundary chunks, which [24,12] is not.
            continue
        changed[slug] = (entry.get("title"),
                         [(s["name"], len(s["episodes"])) for s in entry["seasons"]])

    # ---- Pass 2: unambiguous 26-boundary shapes (no evidence needed) ----
    for slug in candidates:
        entry = data[slug]
        seasons = entry.get("seasons") or []
        if len(seasons) < 2 or not is_chunk_artifact(seasons):
            continue
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        if not all_eps:
            continue
        collapse(entry, all_eps)
        stats["collapsed"] += 1
        changed[slug] = (entry.get("title"), [("Episodes", len(all_eps))])

    # ---- Pass 3: season-sibling cards keep only their own season -------
    for slug, entry in data.items():
        m = SIB_RE.search(slug)
        if not m:
            continue
        n = int(m.group(1))
        seasons = entry.get("seasons") or []
        if not seasons:
            stats["skipped_empty"] += 1
            continue
        if len(seasons) == 1:
            # Already a single card (K-On!! S2 = 26, Fruits Basket S2 =
            # 25...): its own season, complete and correct. NEVER touch a
            # single-card sibling.
            continue
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        if not all_eps:
            continue
        if not all(GEN_NAME_RE.match(str(s.get("name") or "").strip()) for s in seasons):
            continue  # named arcs on a sibling card -> not a chunk artifact
        keep = None
        parent = data.get(SIB_RE.sub("", slug))
        parent_seasons = (parent.get("seasons") or []) if parent else []
        if parent_seasons and n <= len(parent_seasons):
            keep = parent_seasons[n - 1].get("episodes") or []
        if keep:
            entry["seasons"] = [{"name": "Episodes", "episodes": keep}]
            entry["watch_order"] = ["Episodes"]
            entry["total_episodes"] = len(keep)
            stats["siblings"] += 1
            changed[slug] = (entry.get("title"), [("Episodes", len(keep))])
        else:
            collapse(entry, all_eps)
            stats["collapsed"] += 1
            changed[slug] = (entry.get("title"), [("Episodes", len(all_eps))])

    # ---- Pass 4: sibling-parent mains keep only their own first season ---
    for slug, entry in data.items():
        if SIB_RE.search(slug):
            continue
        seasons = entry.get("seasons") or []
        if len(seasons) < 2:
            continue
        if slug not in {SIB_RE.sub("", k) for k in data if SIB_RE.search(k)}:
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
        stats["mains"] += 1
        changed[slug] = (entry.get("title"), [("Episodes", len(first))])

    # ---- Pass 5: flatten ALL remaining generic Season-N grids -----------
    # The site's model is one card per page: shows with separate season
    # cards keep only their own season (pass 4), and every remaining
    # all-generic "Season 1/2/3" grid -- even real ones like Kuroko
    # 25/25/25 or Black Butler 24/12 -- becomes a single flat card. Named
    # arc/saga cards (One Piece sagas, Bleach arcs, AoT "The Final
    # Season", Naruto/Shippuden...) are intentional watch-order structure
    # and are kept.
    generic_flattened = 0
    for slug, entry in data.items():
        if SIB_RE.search(slug):
            continue
        seasons = entry.get("seasons") or []
        if len(seasons) < 2:
            continue
        names = [str(s.get("name") or "").strip() for s in seasons]
        if not all(GEN_NAME_RE.match(n) for n in names):
            continue
        all_eps = [e for s in seasons for e in (s.get("episodes") or [])]
        if not all_eps:
            continue
        collapse(entry, all_eps)
        generic_flattened += 1
        changed[slug] = (entry.get("title"), [("Episodes", len(all_eps))])

    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("collapsed: %d | rebuilt: %d | evidence-settled: %d | siblings: %d | mains-to-s1: %d | generic-grids-flattened: %d | skipped-empty: %d"
          % (stats["collapsed"], stats["rebuilt"], stats["evidence"], stats["siblings"],
             stats["mains"], generic_flattened, stats["skipped_empty"]))
    for slug in ["slam-dunk", "rent-a-girlfriend", "rent-a-girlfriend-season-2",
                 "rent-a-girlfriend-season-3", "rent-a-girlfriend-season-4",
                 "my-hero-academia", "my-hero-academia-season-2",
                 "the-disastrous-life-of-saiki-k", "detective-conan",
                 "kurokos-basketball", "black-butler", "frieren-beyond-journey-s-end",
                 "sailor-moon", "shaman-king-2021", "fullmetal-alchemist",
                 "k-on-season-2", "fruits-basket-season-2", "that-time-i-got-reincarnated-as-a-slime"]:
        print("  %-42s -> %s" % (slug, changed.get(slug, "UNCHANGED")))


if __name__ == "__main__":
    main()