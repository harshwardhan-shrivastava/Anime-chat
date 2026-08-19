#!/usr/bin/env python3
"""Upgrade posters AniList can't serve in HD using Kitsu's HD posters.

AniList serves most covers in the large (~460px) flavor, but roughly a third
of the catalog (mostly PNGs and older uploads) only exists as the medium
(~230px) flavor — those cards render noticeably soft next to the HD ones.
Kitsu hosts the same shows' posters at 550x780+ (poster_image large/original),
so we look each show up on Kitsu through its public mappings endpoint
(anilist/anime -> kitsu id, cross-checked against our title), and swap the
poster — and any cover-type banner — to the Kitsu large URL.

Kitsu URLs pass through the anime_img / anime_img_large filters untouched, and
media.kitsu.app is already in the pages' preconnect/dns-prefetch lists.

Usage (same resumable pattern as the other enrich scripts — kill/limit a run
and re-run to continue, progress is saved every 25 titles):
    python3 scripts/upgrade_posters_kitsu.py                 # default budget (150s)
    python3 scripts/upgrade_posters_kitsu.py --budget 300    # longer run
    python3 scripts/upgrade_posters_kitsu.py --apply         # merge cache into anime_data.json
"""

import argparse
import os
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from scripts.common import read_json as _read_json, save_json  # noqa: E402
from scripts.enrich_ep_thumbnails import (
    KITSU,
    KITSU_HEADERS,
    _kitsu_get,
    _kitsu_search,
    _norm,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
CACHE_FILE = os.path.join(ROOT, "anime_poster_kitsu.json")

SLEEP = 0.35
MATCH_THRESHOLD = 0.4


def _kitsu_title_forms(attrs):
    """All name forms of a Kitsu anime entry, for matching our English title.

    Kitsu's canonical title is often the romanized Japanese name while the
    catalog uses the English title, so we compare against every form Kitsu
    exposes (titles.en / en_us, canonicalTitle, slug, abbreviatedTitles)."""
    forms = []
    titles = attrs.get("titles") or {}
    for key in ("en", "en_us", "en_jp"):
        v = titles.get(key)
        if v:
            forms.append(v)
    canon = attrs.get("canonicalTitle")
    if canon:
        forms.append(canon)
    slug = attrs.get("slug")
    if slug:
        forms.append(slug.replace("-", " "))
    for v in attrs.get("abbreviatedTitles") or []:
        if v:
            forms.append(v)
    return forms


def _title_ratio(title, attrs):
    return max(
        (SequenceMatcher(None, _norm(title), _norm(f)).ratio()
         for f in _kitsu_title_forms(attrs)),
        default=0.0,
    )


def load_json(path):
    """Unreadable/corrupt cache reads as empty so a run can just continue."""
    return _read_json(path, {})


def _poster_url(kitsu_id, attrs=None):
    """Kitsu poster URL (large flavor, 550x780) for a kitsu anime id.

    When attrs are present (from a search hit) we take the API's verified
    poster URL; otherwise we build it from the deterministic CDN pattern."""
    if attrs:
        p = (attrs.get("posterImage") or {})
        for key in ("large", "original"):
            u = p.get(key)
            if u:
                return u
    if kitsu_id:
        return f"https://media.kitsu.app/anime/poster_images/{kitsu_id}/large.jpg"
    return None


def _mapping_url(session, anilist_id, title):
    """Resolve an AniList id to a Kitsu poster via the mappings endpoint.

    Returns (url, "mapping") on success, (None, None) when the mapping is
    missing or the matched Kitsu entry doesn't look like our show."""
    if not anilist_id:
        return None, None
    j = _kitsu_get(
        f"{KITSU}/mappings",
        params={
            "filter[externalSite]": "anilist/anime",
            "filter[externalId]": str(anilist_id),
            "include": "item",
            "page[limit]": 1,
        },
    )
    if not j:
        return None, None
    for inc in j.get("included") or []:
        if inc.get("type") != "anime":
            continue
        attrs = inc.get("attributes") or {}
        ratio = _title_ratio(title, attrs)
        if ratio < MATCH_THRESHOLD:
            return None, None
        url = _poster_url(inc.get("id"), attrs)
        return (url, "mapping") if url else (None, None)
    return None, None


def _search_url(title, year):
    """Fallback: search Kitsu by title and return the best hit's poster."""
    hits = _kitsu_search(title)
    if not hits:
        return None, None
    scored = []
    for kid, ctitle, cy in hits:
        s = SequenceMatcher(None, _norm(title), _norm(ctitle)).ratio()
        if year and cy and str(cy) == str(year):
            s += 0.2
        scored.append((s, kid, ctitle))
    scored.sort(key=lambda x: -x[0])
    best_score, best_id, _ = scored[0]
    if best_score < 0.45:
        return None, None
    j = _kitsu_get(f"{KITSU}/anime/{best_id}")
    if not j:
        return None, None
    attrs = j.get("data", {}).get("attributes") or {}
    if _title_ratio(title, attrs) < MATCH_THRESHOLD:
        return None, None
    url = _poster_url(best_id, attrs)
    return (url, "search") if url else (None, None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="merge the cached Kitsu URLs into anime_data.json")
    parser.add_argument("--budget", type=int, default=150,
                        help="max seconds to spend per run (default 150)")
    args = parser.parse_args()

    data = load_json(DATA_FILE)

    if args.apply:
        cache = load_json(CACHE_FILE)
        changed = 0
        banners = 0
        for slug, url in cache.items():
            entry = data.get(slug)
            if not entry or not url:
                continue
            if entry.get("image") != url:
                entry["image"] = url
                changed += 1
            banner = entry.get("banner") or ""
            if isinstance(banner, str) and "/cover/" in banner and banner != url:
                entry["banner"] = url
                banners += 1
        if changed or banners:
            save_json(DATA_FILE, data)
        print(f"[apply] switched {changed} posters to Kitsu HD (+{banners} cover-type banners)",
              flush=True)
        return

    cache = load_json(CACHE_FILE)
    todo = [
        (slug, entry)
        for slug, entry in data.items()
        if "/cover/medium/" in str(entry.get("image") or "")
        and str(slug) not in cache
    ]
    print(f"[fetch] {len(todo)} medium-only posters to upgrade "
          f"({len(cache)} already cached)", flush=True)

    session = requests.Session()
    session.headers.update(KITSU_HEADERS)
    start = time.time()
    done = 0
    stats = {"mapping": 0, "search": 0, "failed": 0}
    for slug, entry in todo:
        if time.time() - start > args.budget:
            break
        title = entry.get("title") or slug
        year = str(entry.get("release") or "")[:4]
        url, source = _mapping_url(session, entry.get("anilist_id"), title)
        if not url:
            url, source = _search_url(title, year)
        if url:
            cache[str(slug)] = url
            stats[source] += 1
        else:
            cache[str(slug)] = ""  # mark as attempted so we don't retry every run
            stats["failed"] += 1
        done += 1
        if done % 25 == 0:
            save_json(CACHE_FILE, cache)
            print(f"[fetch] {done} done (mapping {stats['mapping']}, "
                  f"search {stats['search']}, failed {stats['failed']})",
                  flush=True)
        time.sleep(SLEEP)

    save_json(CACHE_FILE, cache)
    print(f"[fetch] {done} processed this run — totals: mapping "
          f"{stats['mapping']}, search {stats['search']}, failed {stats['failed']}",
          flush=True)
    if stats["failed"]:
        print(f"[fetch] {stats['failed']} had no confident Kitsu match — they stay "
              f"on AniList medium. Run with --apply to merge what we have.",
              flush=True)
    else:
        print("Run with --apply to merge the cached URLs into anime_data.json",
              flush=True)


if __name__ == "__main__":
    main()
