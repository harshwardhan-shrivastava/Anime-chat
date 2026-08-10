#!/usr/bin/env python3
"""
Name-aware episode-thumbnail matcher.

The main --fetch pipeline keys thumbnails by "<tvmaze-season>:<number>", which
fails whenever TVmaze's season values don't line up with our per-card season
indexes (long-runners use AIR-YEAR seasons: Naruto's episodes are keyed
2002:1, 2003:1 ...). This script recovers those by matching EPISODE NAMES
instead of positions:

  - Pass A (name match): for every episode in the card that has a REAL title
    (not a placeholder like "Season 1 - Episode 1"), find the best TVmaze
    episode by normalized-name similarity (>= 0.8). Each TVmaze episode can be
    used at most once (greedy best-first assignment, airdate tiebreak).
  - Pass B (exact positional): for seasons where NOTHING matched by name AND
    the season's named-episode count exactly equals a matched TVmaze show's
    episode-with-image count, fill positionally in airdate order. This only
    fires when counts match exactly, so images can't land on wrong episodes.

Writes results as {"<our-season-index>:<episode-number>": url} into a cache
file, exactly like --fetch caches, so --apply picks them up unchanged.

Usage:
    python3 scripts/match_ep_thumbs.py --match 200 --offset 0 --cache anime_ep_thumbs_m0.json
    python3 scripts/match_ep_thumbs.py --match 200 --offset 200 --cache anime_ep_thumbs_m1.json
    python3 scripts/enrich_ep_thumbnails.py --apply
"""

import argparse
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.enrich_ep_thumbnails import (
    DATA_FILE,
    _get_json,
    _norm,
    _search_anims,
    load_json,
    save_json,
    search_series,
    split_season_suffix,
    title_is_real,
    API,
)

NAME_THRESHOLD = 0.80
SLEEP = 0.5

_PH_RE = re.compile(
    r"(?:^|[-\s])(?:Season|S)\s*\d+\s*-\s*Episode\s*\d+$|^Episode\s*\d+$",
    re.I,
)


def _is_placeholder(title):
    return bool(title) and bool(_PH_RE.search(title))


def _ep_image(ep):
    img = (ep.get("image") or {}).get("medium_landscape") or \
          (ep.get("image") or {}).get("original") or \
          (ep.get("image") or {}).get("medium")
    if img and isinstance(img, str) and "medium_landscape" in img:
        img = img.replace("/medium_landscape/", "/original_untouched/")
    return img if isinstance(img, str) else None


def _fetch_show_eps(title, year, extra_queries=()):
    """Return (show_id, list_of_episodes_with_images) for the best TVmaze hit.

    Tries the base title, then any extra queries (e.g. a distinct season name
    like 'Naruto Shippuden'). All episodes with images, airdate-sorted."""
    best = None
    for q in [title] + list(extra_queries):
        if not q:
            continue
        sid, score = search_series(q, year)
        if not sid:
            continue
        eps = _get_json(f"{API}/shows/{sid}/episodes")
        if not eps:
            continue
        with_img = [e for e in eps if _ep_image(e)]
        if with_img:
            best = (sid, with_img)
            break
    if not best:
        return None, []
    sid, eps = best
    eps = sorted(eps, key=lambda e: (e.get("airdate") or "", e.get("season") or 0, e.get("number") or 0))
    return sid, eps


def _fetch_all_shows(title, year, extra_queries=()):
    """Fetch episodes-with-images from EVERY matched TVmaze show.

    Long-runner cards split their seasons across separate TVmaze entries
    (Naruto S1 vs 'Naruto: Shippūden'), so we keep each show's episodes
    separate and let the positional pass match each season to the show whose
    episode count fits best. Returns list of (show_id, eps_sorted)."""
    out = []
    seen_ids = set()
    for q in [title] + list(extra_queries):
        if not q:
            continue
        sid, score = search_series(q, year)
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        eps = _get_json(f"{API}/shows/{sid}/episodes")
        if not eps:
            continue
        with_img = [e for e in eps if _ep_image(e)]
        if not with_img:
            continue
        with_img.sort(key=lambda e: (e.get("airdate") or "", e.get("season") or 0, e.get("number") or 0))
        out.append((sid, with_img))
        time.sleep(SLEEP * 0.4)
    return out


def _greedy_name_match(our_eps, tvm_eps):
    """Match our episodes (with real titles) to TVmaze episodes by name.

    our_eps: list of (si, num, norm_title, airdate_rank)
    tvm_eps: list of dicts with name/image
    Returns {f"{si}:{num}": url} for the accepted matches."""
    if not our_eps or not tvm_eps:
        return {}
    used = set()
    out = {}
    # candidates: (score, -airdate_rank, our_idx, tvm_idx) sorted by score desc
    cands = []
    for oi, (si, num, onorm, o_rank) in enumerate(our_eps):
        for ti, te in enumerate(tvm_eps):
            tname = _norm(te.get("name") or "")
            if not tname:
                continue
            score = SequenceMatcher(None, onorm, tname).ratio()
            if score >= NAME_THRESHOLD:
                t_air = (te.get("airdate") or "")[:4]
                try:
                    t_year = int(t_air or "0")
                except ValueError:
                    t_year = 0
                cands.append((score, -o_rank, -t_year, oi, ti))
    cands.sort(reverse=True)
    for score, _, _, oi, ti in cands:
        if oi in used or ti in used:
            continue
        si, num, _, _ = our_eps[oi]
        img = _ep_image(tvm_eps[ti])
        if not img:
            continue
        out[f"{si}:{num}"] = img
        used.add(oi)
        used.add(ti)
    return out


def match_slug(slug, entry):
    """Return {f"{si}:{num}": url} or an error marker for one catalog card."""
    title = entry.get("title") or slug
    year = entry.get("release") or ""
    y = None
    m = re.search(r"(\d{4})", str(year))
    if m:
        y = int(m.group(1))

    base, _ = split_season_suffix(slug, title)
    # Season names that differ from the card title (Naruto / Naruto Shippuden)
    # become extra search queries so each season maps to its own TVmaze show.
    extra = []
    for s in entry.get("seasons") or []:
        sn = (s.get("name") or "").strip()
        if sn and sn.lower() != (base or title).lower() and sn.lower() not in (
                base or title).lower() and len(sn) > 4:
            extra.append(sn)

    sid, tvm_eps = _fetch_show_eps(base or title, y, extra[:3])

    # Collect our episodes missing a thumbnail, split into real / placeholder.
    real_eps = []
    total_named = 0
    for si, s in enumerate(entry.get("seasons") or [], start=1):
        for ep in s.get("episodes") or []:
            t = ep.get("title")
            if not t or ep.get("thumb"):
                continue
            total_named += 1
            if title_is_real(t) and not _is_placeholder(t):
                real_eps.append((si, ep.get("number"), _norm(t), total_named))

    shows = _fetch_all_shows(base or title, y, extra[:3])
    if not shows:
        return {"__error__": "no_episode_images"}

    out = {}
    all_tvm = [e for _, es in shows for e in es]
    if real_eps and all_tvm:
        out.update(_greedy_name_match(real_eps, all_tvm))

    # Pass B: positional fill for seasons with zero name matches.
    matched_keys = set(out.keys())
    for si, s in enumerate(entry.get("seasons") or [], start=1):
        named = [ep for ep in (s.get("episodes") or [])
                 if ep.get("title") and not ep.get("thumb")]
        if not named:
            continue
        if any(f"{si}:{ep.get('number')}" in matched_keys for ep in named):
            continue  # this season already has name-based matches
        nums = sorted(ep.get("number") for ep in named)
        if nums != list(range(1, len(nums) + 1)):
            continue  # only clean 1..N numbering gets positional fill
        # pick the show whose count is closest to this season's named count
        candidates = []
        for show_i, (_, es) in enumerate(shows):
            ratio = len(es) / max(len(named), 1)
            candidates.append((abs(ratio - 1.0), show_i, len(es)))
        candidates.sort(key=lambda c: (c[0], -c[2]))
        _, show_i, show_len = candidates[0]
        if show_len < len(named):
            continue  # show has fewer images than our named eps: unsafe
        if len(named) >= 3 and show_len > len(named) * 1.15:
            continue  # too big a mismatch: probably a different entry
        _, es = shows[show_i]
        for ep, te in zip(named, es):
            img = _ep_image(te)
            if img:
                out[f"{si}:{ep.get('number')}"] = img

    # Pass C: cumulative-offset positional fill for long-runners whose card is
    # a single TVmaze show split into sequential saga seasons (One Piece, Bleach,
    # Gintama, Yo-kai Watch ...). Episodes are airdate-sorted slices: season i
    # starts at the episode right after the previous season's named count, so
    # stills land on the correct sequential episodes. Guards: the card's total
    # named count must fit inside one show's episode list, and the first season
    # must start at 1 (a strong signal the list is a contiguous prefix).
    if len(out) == 0:
        total_named_card = sum(
            sum(1 for ep in (s.get("episodes") or []) if ep.get("title") and not ep.get("thumb"))
            for s in entry.get("seasons") or []
        )
        seasons_named = []
        ok = True
        for s in entry.get("seasons") or []:
            named = [ep for ep in (s.get("episodes") or []) if ep.get("title") and not ep.get("thumb")]
            if not named:
                continue
            nums = sorted(ep.get("number") for ep in named)
            if nums != list(range(1, len(nums) + 1)):
                ok = False
                break
            seasons_named.append((len(named), named))
        if ok and seasons_named:
            # a single dominant TVmaze show must cover the whole card
            big_show = None
            for _, es in shows:
                if len(es) >= total_named_card:
                    big_show = es
                    break
            if big_show is not None:
                offset = 0
                for size, named in seasons_named:
                    for ep, te in zip(named, big_show[offset:offset + size]):
                        img = _ep_image(te)
                        if img:
                            si = None
                            for si2, s in enumerate(entry.get("seasons") or [], start=1):
                                if any(x is ep for x in (s.get("episodes") or [])):
                                    si = si2
                                    break
                            if si:
                                out[f"{si}:{ep.get('number')}"] = img
                    offset += size

    if not out:
        return {"__error__": "no_match"}
    return out


def match_window(chunk=0, offset=0, cache_file=None, todo_path=None):
    data = load_json(DATA_FILE)
    if todo_path:
        slugs = [s for s in load_json(todo_path) if s in data]
    else:
        slugs = []
        for slug, e in data.items():
            missing = any(
                (ep.get("title") and not ep.get("thumb"))
                for s in (e.get("seasons") or [])
                for ep in (s.get("episodes") or [])
            )
            if missing:
                slugs.append(slug)
    window = slugs[offset:offset + chunk] if chunk else slugs[offset:]
    cache = load_json(cache_file) if cache_file else {}
    print(f"catalog: {len(slugs)} slugs, window {len(window)} "
          f"({offset}..{offset + len(window)})", flush=True)

    done = 0
    for slug in window:
        if slug in cache:
            continue
        cache[slug] = match_slug(slug, data[slug])
        done += 1
        if done % 10 == 0 or done == len(window):
            save_json(cache_file or "anime_ep_thumbs_m.json", cache)
            good = sum(1 for v in cache.values() if v and "__error__" not in v)
            print(f"  matched {done}/{len(window)} | cached {len(cache)}, "
                  f"{good} with thumbs", flush=True)
        time.sleep(SLEEP)
    save_json(cache_file or "anime_ep_thumbs_m.json", cache)
    good = sum(1 for v in cache.values() if v and "__error__" not in v)
    print(f"DONE match cache: {len(cache)} entries, {good} with thumbs", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--todo", default=None)
    ap.add_argument("--test", default=None)
    args = ap.parse_args()

    if args.test:
        slug = args.test
        data = load_json(DATA_FILE)
        r = match_slug(slug, data.get(slug, {}))
        if isinstance(r, dict) and "__error__" in r:
            print(f"{slug}: ERROR {r['__error__']}")
        else:
            print(f"{slug}: {len(r)} matched | sample: {list(r.items())[:3]}")
        return
    if args.match:
        match_window(chunk=args.match, offset=args.offset, cache_file=args.cache,
                     todo_path=args.todo)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
