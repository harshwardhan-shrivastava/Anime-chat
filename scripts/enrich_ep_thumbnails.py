#!/usr/bin/env python3
"""
Enrich per-episode thumbnail images from TheTVDB (the source Plex/Jellyfin/Kodi
use for anime episode art). Fills episode["thumb"] with a full artwork CDN URL
so the episode list + episode page can show a real thumbnail per episode.

Parallel + resumable like the MAL grind (see enrich_mal_episodes.py):
  - --plan   writes anime_ep_thumbs_todo.json (slug + title + year) so workers
             never load the 50MB catalog (prevents OOM in low-memory boxes).
  - --fetch  reads the todo and processes a window into its OWN cache file
             (use a distinct --cache per worker; --offset slices the todo).
  - --apply  merges every anime_ep_thumbs*.json cache into anime_data.json as
             episode["thumb"] = <full artwork URL>.

Auth (free tier): create a free account at https://thetvdb.com, open
"API Keys", and generate an apikey + PIN. Provide them as env vars:
    THE_TVDB_API_KEY   (the apikey)
    THE_TVDB_PIN       (the 8-char PIN, only needed once per token)
The bearer token is exchanged once and cached in anime_tvdb_token.json
(valid ~1 month), so later runs only need THE_TVDB_API_KEY.

Usage:
    THE_TVDB_API_KEY=... THE_TVDB_PIN=... python3 scripts/enrich_ep_thumbnails.py --plan
    THE_TVDB_API_KEY=... python3 scripts/enrich_ep_thumbnails.py --fetch 40 --offset 0 --cache anime_ep_thumbs_w0.json
    THE_TVDB_API_KEY=... python3 scripts/enrich_ep_thumbnails.py --fetch 40 --offset 40 --cache anime_ep_thumbs_w1.json
    THE_TVDB_API_KEY=... python3 scripts/enrich_ep_thumbnails.py --apply
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
TOKEN_FILE = os.path.join(ROOT, "anime_tvdb_token.json")

API = "https://api4.thetvdb.com/v4"
ART = "https://artworks.thetvdb.com"

HEADERS = {"User-Agent": "AnimeChat/1.0 (episode-thumbnail enrichment)"}
SLEEP = 0.5  # seconds between TVDB calls (free tier throttles)
PAGE_SIZE = 100

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


def get_token():
    """Return a valid bearer token, refreshing from apikey+pin when needed."""
    cache = load_json(TOKEN_FILE)
    if cache.get("token"):
        return cache["token"]

    apikey = os.environ.get("THE_TVDB_API_KEY", "").strip()
    pin = os.environ.get("THE_TVDB_PIN", "").strip()
    if not apikey:
        print("ERROR: THE_TVDB_API_KEY env var is missing. Create a free key at "
              "https://thetvdb.com (Account -> API Keys) and set it (with the "
              "PIN) via the project's Keys/API keys UI.", flush=True)
        sys.exit(2)

    body = {"apikey": apikey}
    if pin:
        body["pin"] = pin
    for attempt in range(3):
        try:
            r = requests.post(f"{API}/login", json=body, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                token = r.json().get("data", {}).get("token")
                if token:
                    save_json(TOKEN_FILE, {"token": token})
                    return token
            print(f"  tvdb login failed ({r.status_code}): {r.text[:200]}", flush=True)
        except Exception as exc:
            print(f"  tvdb login error: {exc}", flush=True)
        time.sleep(3)
    sys.exit(2)


def _auth_headers():
    return {**HEADERS, "Authorization": f"Bearer {get_token()}"}


def _get(path, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}{path}", headers=_auth_headers(),
                             params=params, timeout=20)
            if r.status_code == 200:
                return r.json().get("data")
            if r.status_code == 401:
                # Token expired: drop cache and refresh once.
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
                get_token()
                continue
            if r.status_code == 404:
                return None
            time.sleep(5 + attempt * 4)
        except Exception:
            time.sleep(5)
    return None


def _norm(s):
    return _SLUG_CLEAN.sub("", (s or "").lower())


def search_series(title, year):
    """Find the best TheTVDB series id for a title (+year). Returns (id, score)."""
    data = _get("/search", {"query": title, "type": "series", "language": "eng"})
    if not data:
        return None, 0
    best, best_score = None, 0.0
    for s in data:
        name = s.get("name") or ""
        score = SequenceMatcher(None, _norm(title), _norm(name)).ratio()
        sy = s.get("year")
        if year and sy and str(sy) == str(year):
            score += 0.25
        if score > best_score:
            best, best_score = s.get("id"), score
    return best, best_score


def fetch_thumbs_for(slug, title, year):
    """Returns {episode_number: thumb_url} for a title, or an error marker."""
    series_id, score = search_series(title, year)
    if not series_id:
        return {"__error__": "no_match"}
    if score < 0.55:
        return {"__error__": "no_match"}

    thumbs = {}
    season = 1
    while season <= 30:  # safety cap
        eps = _get(f"/series/{series_id}/episodes/default/{season}",
                   {"page": 1, "language": "eng"})
        if not eps:
            break
        for ep in eps.get("episodes") or []:
            num = ep.get("number")
            img = ep.get("image")
            if num and img and isinstance(img, str) and img.startswith("/"):
                thumbs[str(num)] = ART + img
        # paginate within the season
        page = 2
        while (eps.get("links") or {}).get("next"):
            eps = _get(f"/series/{series_id}/episodes/default/{season}",
                       {"page": page, "language": "eng"})
            if not eps:
                break
            for ep in eps.get("episodes") or []:
                num = ep.get("number")
                img = ep.get("image")
                if num and img and isinstance(img, str) and img.startswith("/"):
                    thumbs[str(num)] = ART + img
            page += 1
            if page > 60:
                break
            time.sleep(SLEEP)
        season += 1
        time.sleep(SLEEP)

    return thumbs or {"__error__": "no_episodes"}


def plan_todo():
    """Recompute anime_ep_thumbs_todo.json: every slug that has episodes but
    no thumbnail data yet, in stable catalog order."""
    data = load_json(DATA_FILE)
    existing = set()
    for f in glob.glob(os.path.join(ROOT, "anime_ep_thumbs*.json")):
        existing.update(load_json(f).keys())
    todo = []
    for slug, e in data.items():
        if slug in existing:
            continue
        total = sum(len(s.get("episodes") or []) for s in (e.get("seasons") or []))
        if not total:
            continue
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
    episode["thumb"] (keyed by episode number within each season)."""
    data = load_json(DATA_FILE)
    merged = {}
    files = sorted(glob.glob(os.path.join(ROOT, "anime_ep_thumbs*.json")))
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
        for s in e.get("seasons") or []:
            for ep in s.get("episodes") or []:
                t = thumbs.get(str(ep.get("number")))
                if t:
                    ep["thumb"] = t
                    filled_episodes += 1
                    changed = True
        if changed:
            filled_entries += 1
    save_json(DATA_FILE, data)
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
