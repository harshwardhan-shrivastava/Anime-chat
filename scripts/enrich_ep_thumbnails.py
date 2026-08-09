#!/usr/bin/env python3
"""
Enrich per-episode thumbnail images from TVmaze — a completely KEYLESS API
(no account, no API key, no subscription). TVmaze serves real 16:9 episode
stills from its public image CDN (static.tvmaze.com), so the Flask app can
hotlink them directly. Fills episode["thumb"]; the episode list + episode page
show the thumbnail instead of the official poster when one exists.

(Tried IMDb: blocks datacenter IPs with HTTP 202. Tried TheTVDB: now requires
a paid subscriber PIN. TMDB: free but needs an account/API key. TVmaze needs
none of that, so it wins.)

Parallel + resumable like the MAL grind (see enrich_mal_episodes.py):
  - --plan   writes anime_ep_thumbs_todo.json (slug + title + year) for every
             title that has at least one named episode, so workers never load
             the 50MB catalog (prevents OOM in low-memory boxes).
  - --fetch  reads the todo and processes a window into its OWN cache file
             (use a distinct --cache per worker; --offset slices the todo).
  - --apply  merges every anime_ep_thumbs*.json cache into anime_data.json as
             episode["thumb"] = <full TVmaze image URL>.

Thumbnails are keyed by "season:number" (our season index is 1-based and maps
to TVmaze's season number), so each season's episodes match correctly.

Usage:
    python3 scripts/enrich_ep_thumbnails.py --plan
    python3 scripts/enrich_ep_thumbnails.py --fetch 60 --offset 0 --cache anime_ep_thumbs_w0.json
    python3 scripts/enrich_ep_thumbnails.py --fetch 60 --offset 60 --cache anime_ep_thumbs_w1.json
    python3 scripts/enrich_ep_thumbnails.py --apply
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
TODO_FILE = os.path.join(ROOT, "anime_ep_thumbs_todo.json")

API = "https://api.tvmaze.com"
HEADERS = {"User-Agent": "AnimeChat/1.0 (episode-thumbnail enrichment)"}
SLEEP = 0.55  # TVmaze guidance: ~20 requests / 10 seconds, keep it polite

# Cache files only (NOT anime_ep_thumbs_todo.json, which is a list).
CACHE_PATTERNS = ("anime_ep_thumbs.json", "anime_ep_thumbs_w*.json")


def _cache_files():
    files = []
    for pat in CACHE_PATTERNS:
        files.extend(sorted(glob.glob(os.path.join(ROOT, pat))))
    return sorted(set(files))

_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def _norm(s):
    return _SLUG_CLEAN.sub("", (s or "").lower())


def title_is_real(t):
    if not t:
        return False
    t = t.strip()
    if not t or t.lower() in ("untitled", "tba", "tbd"):
        return False
    return True


def _get_json(url, params=None, retries=5):
    """GET a TVmaze URL with 429-aware backoff. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(8 + attempt * 5)  # rate-limited: back off
                continue
            if r.status_code == 404:
                return None
            time.sleep(3 + attempt * 3)
        except Exception:
            time.sleep(4)
    return None


def _search_anims(query):
    """Return Animation-typed TVmaze shows matching a query (relevance order)."""
    data = _get_json(f"{API}/search/shows", params={"q": query})
    out = []
    if data:
        for item in data:
            s = item.get("show") or {}
            if (s.get("type") or "").lower() == "animation":
                out.append(s)
    return out


def search_series(title, year):
    """Find the best TVmaze show id for a title (+year). Returns (id, score).

    TVmaze lists many anime under their Japanese titles ('Your Lie in April'
    lives there as 'Shigatsu wa Kimi no Uso') and full English titles often
    fail its search ('KONOSUBA -God's blessing...' needs just 'KONOSUBA'), so
    we (1) try the full title, (2) retry with the first two distinctive words,
    and (3) always accept the best-scoring Animation-typed result — the
    catalog is 100% anime, so an Animation hit for an anime-title query is the
    right show (or a same-franchise entry)."""
    anims = _search_anims(title)
    if not anims:
        short = re.sub(r"[\[\(].*?[\]\)]", " ", title)
        short = re.sub(r"[^A-Za-z0-9 ]+", " ", short)
        words = [w for w in short.split() if len(w) >= 3]
        for retry in ([" ".join(words[:2])] if len(words) >= 2 else []) + (words[:1] if words else []):
            if retry.lower() == title.lower():
                continue
            anims = _search_anims(retry)
            if anims:
                break
    if not anims:
        return None, 0.0
    best, best_score = None, 0.0
    for s in anims:
        score = SequenceMatcher(None, _norm(title), _norm(s.get("name") or "")).ratio()
        p = (s.get("premiered") or "")[:4]
        if year and p and str(p) == str(year):
            score += 0.2
        if score > best_score:
            best, best_score = s.get("id"), score
    return best, best_score


def fetch_thumbs_for(slug, title, year):
    """Returns {"season:number": thumb_url} for a title, or an error marker."""
    series_id, score = search_series(title, year)
    if not series_id:
        return {"__error__": "no_match"}
    if score < 0.55:
        return {"__error__": "no_match"}
    eps = _get_json(f"{API}/shows/{series_id}/episodes")
    if eps is None:
        return {"__error__": "fetch_failed"}
    out = {}
    for ep in eps:
        season = ep.get("season")
        num = ep.get("number")
        # TVmaze exposes 'medium_landscape' (~640px) in its API; the true HD
        # master (often 1920x1080) lives at the SAME folder/filename under
        # 'original_untouched' on their CDN, so we rewrite the URL in place.
        img = (ep.get("image") or {}).get("medium_landscape") or \
              (ep.get("image") or {}).get("original") or \
              (ep.get("image") or {}).get("medium")
        if img and isinstance(img, str) and "medium_landscape" in img:
            img = img.replace("/medium_landscape/", "/original_untouched/")
        if season and num and img and isinstance(img, str):
            out[f"{season}:{num}"] = img
    return out or {"__error__": "no_episodes"}


def plan_todo():
    """Recompute anime_ep_thumbs_todo.json: every slug that has at least one
    named episode but no thumbnail data yet, in stable catalog order."""
    data = load_json(DATA_FILE)
    existing = set()
    for f in _cache_files():
        for slug, val in (load_json(f) or {}).items():
            if isinstance(val, dict) and "__error__" not in val and val:
                existing.add(slug)  # only slugs with real thumbs are done
    todo = []
    for slug, e in data.items():
        if slug in existing:
            continue
        has_named = any(
            title_is_real(ep.get("title"))
            for s in (e.get("seasons") or [])
            for ep in (s.get("episodes") or [])
        )
        if not has_named:
            continue  # only episodes that actually have a name get a thumbnail
        todo.append([slug, e.get("title") or slug, e.get("release") or ""])
    save_json(TODO_FILE, todo)
    print(f"PLAN: {len(todo)} titles need thumbnail lookups", flush=True)


def fetch_window(chunk, offset=0, cache_file=None):
    todo = load_json(TODO_FILE)
    window = todo[offset:offset + chunk] if chunk else todo[offset:]
    cache = load_json(cache_file) if cache_file else {}
    print(f"todo: {len(todo)} total, this window {len(window)} "
          f"({offset}..{offset + len(window)})", flush=True)

    done = 0
    for slug, title, year in window:
        if slug in cache:
            continue
        y = None
        if isinstance(year, int):
            y = year
        else:
            m = re.search(r"(\d{4})", str(year))
            if m:
                y = int(m.group(1))
        cache[slug] = fetch_thumbs_for(slug, title, y)
        done += 1
        if done % 10 == 0 or done == len(window):
            save_json(cache_file or "anime_ep_thumbs.json", cache)
            good = sum(1 for v in cache.values() if v and "__error__" not in v)
            print(f"  looked up {done}/{len(window)} | cached {len(cache)}, "
                  f"{good} with thumbs", flush=True)
        time.sleep(SLEEP)
    save_json(cache_file or "anime_ep_thumbs.json", cache)
    good = sum(1 for v in cache.values() if v and "__error__" not in v)
    print(f"DONE thumb cache: {len(cache)} entries, {good} with thumbs", flush=True)


def apply_thumbs():
    """Merge every anime_ep_thumbs*.json cache into anime_data.json as
    episode["thumb"], keyed by "<season-index>:<episode-number>"."""
    data = load_json(DATA_FILE)
    merged = {}
    files = _cache_files()
    print(f"merging {len(files)} cache files", flush=True)
    for fname in files:
        cache = load_json(fname) or {}
        for slug, thumbs in cache.items():
            if isinstance(thumbs, dict) and "__error__" not in thumbs and thumbs:
                merged[slug] = thumbs

    filled_episodes = 0
    filled_entries = 0
    for slug, e in data.items():
        thumbs = merged.get(slug)
        if not thumbs:
            continue
        changed = False
        for si, s in enumerate(e.get("seasons") or [], start=1):
            for ep in s.get("episodes") or []:
                t = thumbs.get(f"{si}:{ep.get('number')}")
                if t:
                    ep["thumb"] = t
                    filled_episodes += 1
                    changed = True
        if changed:
            filled_entries += 1
    save_json(DATA_FILE, data)
    # Fold clean results into the base cache so the next --plan skips them.
    base = load_json(os.path.join(ROOT, "anime_ep_thumbs.json"))
    base.update(merged)
    save_json(os.path.join(ROOT, "anime_ep_thumbs.json"), base)
    print(f"APPLIED: {filled_entries} entries updated, {filled_episodes} episode "
          f"thumbnails set", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        apply_thumbs()
        return
    if args.plan:
        plan_todo()
    if args.fetch:
        fetch_window(args.fetch, offset=args.offset, cache_file=args.cache)
    if not (args.apply or args.plan or args.fetch):
        ap.print_help()


if __name__ == "__main__":
    main()
