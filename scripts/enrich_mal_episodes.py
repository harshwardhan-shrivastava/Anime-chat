#!/usr/bin/env python3
"""
Enrich real episode titles from MyAnimeList (MAL) for titles in anime_data.json
that still lack per-episode names.

IMDb blocks datacenter scraping (HTTP 202/403 on episode pages), so MAL is used
as the episode-title source instead: AniList maps each anilist_id -> mal_id
(bulk, 50 per request), then MAL's episode list page is scraped for titles.

Parallel + resumable like the JustWatch grind (see enrich_streaming.py):
  - --plan  writes a lightweight anime_mal_todo.json (slug, mal_id) list so
            parallel workers never have to load the 49MB catalog (prevents
            OOM in low-memory containers).
  - --fetch reads that todo list and fetches a window into its OWN cache file
            (use a distinct --cache per worker; --offset slices the todo).
  - --apply merges every anime_mal_episodes*.json cache into the catalog and
            back into the base cache.

MAL throttles aggressively: keep the number of parallel workers low (3-4) and
never resume-fetch without a cooldown after a block. Transient failures are
cached as {"__error__": "..."} markers so the next cycle retries them.

Usage:
    python3 scripts/enrich_mal_episodes.py --malids 400     # build id cache (chunked)
    python3 scripts/enrich_mal_episodes.py --plan           # recompute todo list
    python3 scripts/enrich_mal_episodes.py --fetch 220 --offset 0    --cache anime_mal_episodes_w0.json
    python3 scripts/enrich_mal_episodes.py --fetch 220 --offset 220  --cache anime_mal_episodes_w1.json
    python3 scripts/enrich_mal_episodes.py --fetch 220 --offset 440  --cache anime_mal_episodes_w2.json
    python3 scripts/enrich_mal_episodes.py --apply          # merge all caches into catalog

No API key needed.
"""

import argparse
import glob
import html
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.common import load_json as _load_json, save_json  # noqa: E402

DATA_FILE = os.path.join(ROOT, "anime_data.json")
MAL_IDS_CACHE = os.path.join(ROOT, "anime_mal_ids.json")
EPISODES_CACHE = os.path.join(ROOT, "anime_mal_episodes.json")
TODO_FILE = os.path.join(ROOT, "anime_mal_todo.json")

ANILIST_URL = "https://graphql.anilist.co"
MAL_EP_URL = "https://myanimelist.net/anime/{mal_id}/_/episode"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
GQL_HEADERS = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
PAGE_SIZE = 50
SLEEP = 0.35  # seconds between MAL title fetches (avoid 429s)

# Cloudflare/MAL block pages are short and contain one of these markers.
_BLOCK_MARKERS = (
    "just a moment",
    "cf-challenge",
    "attention required",
    "you have been blocked",
    "cf-error",
    "request unsuccessful",
    "incapsula",
)


def load_json(path):
    """Cache loader used by the whole script family: missing file -> {}."""
    return _load_json(path, {})


def title_is_real(t):
    """Reject placeholder titles ('' or 'Episode N' / 'Untitled')."""
    if not t:
        return False
    t = t.strip()
    if not t or t.lower() in ("untitled", "tba", "tbd"):
        return False
    return True


def fetch_mal_ids_batch(ids):
    q = {
        "query": "query($ids:[Int]){ Page(perPage: 50){ media(id_in:$ids){ id idMal } } }",
        "variables": {"ids": ids},
    }
    for attempt in range(5):
        try:
            r = requests.post(ANILIST_URL, json=q, headers=GQL_HEADERS, timeout=20)
            if r.status_code == 200:
                out = {}
                for m in r.json()["data"]["Page"]["media"]:
                    if m.get("idMal"):
                        out[str(m["id"])] = m["idMal"]
                return out
            time.sleep(3)
        except Exception:
            time.sleep(3)
    return {}


def build_mal_ids(chunk):
    data = load_json(DATA_FILE)
    cache = load_json(MAL_IDS_CACHE)

    # anilist_id -> slug, skipping entries that already have real titles
    need = []
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if not aid:
            continue
        key = str(aid)
        if key in cache:
            continue
        if any(title_is_real(ep.get("title"))
               for s in (e.get("seasons") or [])
               for ep in (s.get("episodes") or [])):
            cache[key] = cache.get(key) or 0
            continue
        need.append((key, slug))

    print(f"titles needing mal_id lookup: {len(need)}", flush=True)
    done = 0
    for i in range(0, len(need), PAGE_SIZE):
        chunk_ids = [k for k, _ in need[i:i + PAGE_SIZE]]
        mapping = fetch_mal_ids_batch([int(k) for k in chunk_ids])
        for k in chunk_ids:
            cache[k] = mapping.get(k, 0)  # 0 = no MAL entry
        done += len(chunk_ids)
        if done % (PAGE_SIZE * 4) == 0 or done == len(need):
            save_json(MAL_IDS_CACHE, cache)
            print(f"  mal_id cache: {len(cache)} mapped ({done}/{len(need)} this run)", flush=True)
        if chunk and done >= chunk:
            break
        time.sleep(1.0)
    save_json(MAL_IDS_CACHE, cache)
    found = sum(1 for v in cache.values() if v)
    print(f"DONE mal_id cache: {len(cache)} entries, {found} with a MAL id", flush=True)


def parse_mal_episodes(html_text):
    """Extract {episode_number: title} from a MAL episode list page.
    Returns None when the page looks like a Cloudflare block/challenge."""
    low = html_text.lower()
    if len(html_text) < 15000 or any(m in low for m in _BLOCK_MARKERS):
        return None
    out = {}
    # new layout: <tr class="episode-list-data"> ... <td class="episode-number nowrap" data-raw="1"> ... <td class="episode-title fs12"><a ...>Title</a>
    for m in re.finditer(r'<tr class="episode-list-data">(.*?)</tr>', html_text, re.S):
        row = m.group(1)
        nm = re.search(r'episode-number[^"]*"\s*data-raw="(\d+)"', row)
        tm = re.search(r'episode-title[^"]*"><a[^>]*>([^<]+)</a>', row, re.S)
        if nm and tm:
            title = html.unescape(tm.group(1)).strip()
            if title_is_real(title):
                out[int(nm.group(1))] = title
    return out


def fetch_episodes_for(mal_id):
    """Fetch ALL episode titles for a MAL id, following offset pagination.
    Returns (episode_dict, err) where err is None on success, 'blocked' for a
    Cloudflare page, or an http/network error string."""
    out = {}
    offset = 0
    while True:
        url = MAL_EP_URL.format(mal_id=mal_id) + (f"?offset={offset}" if offset else "")
        ok = False
        page = {}
        for attempt in range(4):
            try:
                r = requests.get(url, headers=HEADERS, timeout=20)
                if r.status_code == 200:
                    parsed = parse_mal_episodes(r.text)
                    if parsed is None:
                        return out, "blocked"
                    page = parsed
                    out.update(page)
                    ok = True
                    break
                if r.status_code in (429, 403, 202, 405):
                    time.sleep(8 + attempt * 5)
                    continue
                # 404 = no episode list (movies, OVAs, etc.) - legit empty
                return {}, f"http{r.status_code}"
            except Exception:
                time.sleep(5)
        if not ok:
            return out, "failed"
        if len(page) < 100:
            break
        offset += 100
        if offset > 2000:  # safety cap
            break
        time.sleep(0.3)
    return out, (None if out else "empty")


def build_todo(data, ids_cache, base_eps):
    """Deterministic global todo list: every slug that still needs a MAL
    fetch, in stable catalog order (parallel workers slice this by offset)."""
    todo = []
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if not aid:
            continue
        mal_id = ids_cache.get(str(aid))
        if not mal_id or slug in base_eps:
            continue
        if any(title_is_real(ep.get("title"))
               for s in (e.get("seasons") or [])
               for ep in (s.get("episodes") or [])):
            continue
        todo.append((slug, mal_id))
    return todo


def plan_todo():
    """Recompute anime_mal_todo.json from the current catalog + caches."""
    data = load_json(DATA_FILE)
    ids_cache = load_json(MAL_IDS_CACHE)
    base_eps = load_json(EPISODES_CACHE)
    todo = build_todo(data, ids_cache, base_eps)
    save_json(TODO_FILE, todo)
    print(f"PLAN: {len(todo)} titles still need a MAL fetch", flush=True)


def fetch_episode_titles(chunk, offset=0, cache_file=None):
    todo = load_json(TODO_FILE)
    window = todo[offset:offset + chunk] if chunk else todo[offset:]
    eps_cache = load_json(cache_file) if cache_file else {}
    print(f"todo: {len(todo)} total, this window {len(window)} "
          f"({offset}..{offset + len(window)})", flush=True)

    done = 0
    fails = 0
    blocked = 0
    for slug, mal_id in window:
        if slug in eps_cache:  # worker already handled it (resume)
            continue
        eps, err = fetch_episodes_for(mal_id)
        if err == "blocked":
            eps_cache[slug] = {"__error__": "blocked"}
            blocked += 1
        elif err and err not in ("empty", "http404"):
            eps_cache[slug] = {"__error__": err}
        else:
            eps_cache[slug] = eps  # {} = legit no episode list
        if err and err not in ("empty", "http404"):
            fails += 1
        done += 1
        if done % 20 == 0 or done == len(window):
            save_json(cache_file or EPISODES_CACHE, eps_cache)
            good = sum(1 for v in eps_cache.values() if v and "__error__" not in v)
            print(f"  fetched {done}/{len(window)} (fails {fails}, blocked {blocked}) | "
                  f"cached {len(eps_cache)}, {good} with titles", flush=True)
        if chunk and done >= chunk:
            break
        time.sleep(SLEEP)
    save_json(cache_file or EPISODES_CACHE, eps_cache)
    good = sum(1 for v in eps_cache.values() if v and "__error__" not in v)
    print(f"DONE episode cache: {len(eps_cache)} entries, {good} with real titles", flush=True)


SINGLE_TYPES = {"Movie", "OVA", "Special", "Music", "ONA"}


def fill_singles():
    """Single-episode Movie/OVA/Special/Music/ONA entries have no MAL episode
    list, so instead of showing 'Episode 1' in the UI, use the entry's own
    title as the episode title (e.g. 'A Silent Voice'). Multi-episode entries
    are left alone so the MAL fetch can still fill real names."""
    data = load_json(DATA_FILE)
    filled = 0
    skipped_series = 0
    for slug, e in data.items():
        if e.get("type") not in SINGLE_TYPES:
            continue
        seasons = e.get("seasons") or []
        total = sum(len(s.get("episodes") or []) for s in seasons)
        if total != 1:
            continue
        title = (e.get("title") or "").strip()
        if not title:
            continue
        changed = False
        for s in seasons:
            for ep in s.get("episodes") or []:
                if not title_is_real(ep.get("title")):
                    ep["title"] = title
                    filled += 1
                    changed = True
        if changed:
            skipped_series += 1
    save_json(DATA_FILE, data)
    print(f"FILLED: {filled} episode titles for {skipped_series} single-episode entries", flush=True)


def apply_titles():
    data = load_json(DATA_FILE)

    # Merge EVERY anime_mal_episodes*.json cache (base + parallel workers),
    # later files overriding earlier ones for the same slug. Error markers are
    # skipped so the next plan cycle retries those titles.
    merged = {}
    pattern = os.path.join(ROOT, "anime_mal_episodes*.json")
    files = sorted(glob.glob(pattern))
    print(f"merging {len(files)} cache files", flush=True)
    for fname in files:
        cache = load_json(fname) or {}
        for slug, eps in cache.items():
            if isinstance(eps, dict) and "__error__" in eps:
                continue
            merged[slug] = eps

    filled_titles = 0
    filled_entries = 0
    for slug, e in data.items():
        cached = merged.get(slug)
        if not cached:
            continue
        seasons = e.get("seasons") or []
        if not seasons:
            continue
        offset = 0
        changed = False
        for s in seasons:
            episodes = s.get("episodes") or []
            for ep in episodes:
                if ep.get("title") and title_is_real(ep.get("title")):
                    continue
                gnum = offset + (ep.get("number") or 0)
                t = cached.get(str(gnum))
                if t:
                    ep["title"] = t
                    filled_titles += 1
                    changed = True
            offset += len(episodes)
        if changed:
            filled_entries += 1
    save_json(DATA_FILE, data)
    # Fold clean results back into the base cache so the next plan skips them.
    base = load_json(EPISODES_CACHE)
    base.update({k: v for k, v in merged.items() if v is not None})
    save_json(EPISODES_CACHE, base)
    print(f"APPLIED: {filled_entries} entries updated, {filled_titles} episode titles filled", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--malids", type=int, default=0, help="chunk size for mal_id lookups (0 = all)")
    ap.add_argument("--plan", action="store_true", help="recompute the todo list")
    ap.add_argument("--fetch", type=int, default=0, help="window size for episode fetches (0 = all)")
    ap.add_argument("--offset", type=int, default=0, help="start index into the todo list (parallel workers)")
    ap.add_argument("--cache", default=None, help="cache file to write (unique per parallel run)")
    ap.add_argument("--apply", action="store_true", help="merge all caches into anime_data.json")
    ap.add_argument("--fill-singles", action="store_true", help="title single-episode Movie/OVA/Special/Music/ONA entries with their own title")
    args = ap.parse_args()

    if args.apply:
        apply_titles()
        return
    if args.fill_singles:
        fill_singles()
    if args.malids:
        build_mal_ids(args.malids)
    if args.plan:
        plan_todo()
    if args.fetch:
        fetch_episode_titles(args.fetch, offset=args.offset, cache_file=args.cache)
    if not (args.apply or args.fill_singles or args.malids or args.plan or args.fetch):
        ap.print_help()


if __name__ == "__main__":
    main()
