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
  - --plan   writes the todo file (slug + title + year) for every title that
             has at least one named episode, so workers never load the 50MB
             catalog (prevents OOM in low-memory boxes).
  - --fetch  reads the todo and processes a window into its OWN cache file
             (use a distinct --cache per worker; --offset slices the todo).
  - --apply  merges every anime_ep_thumbs*.json cache into anime_data.json as
             episode["thumb"] = <full TVmaze image URL>.

Thumbnails are keyed by "season:number" (our season index is 1-based and maps
to TVmaze's season number), so each season's episodes match correctly.

--todo lets you point --plan/--fetch at a custom retry list (e.g. only the
titles that previously came back as __error__), so a re-sweep after TVmaze /
Kitsu data appears doesn't re-process every successful title.

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
import unicodedata
from difflib import SequenceMatcher

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
TODO_FILE = os.path.join(ROOT, "anime_ep_thumbs_todo.json")

API = "https://api.tvmaze.com"
HEADERS = {"User-Agent": "Otakul/1.0 (episode-thumbnail enrichment)"}
SLEEP = 0.55  # TVmaze guidance: ~20 requests / 10 seconds, keep it polite

# Kitsu (kitsu.io) is the anime-specific fallback: free, keyless JSON:API with
# real per-episode thumbnails served from media.kitsu.app. Used when TVmaze
# has no match, and covers a huge slice of the long tail TVmaze misses.
KITSU = "https://kitsu.io/api/edge"
KITSU_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "Otakul/1.0 (episode-thumbnail enrichment)",
}
_ORDINALS = {"1": "1st", "2": "2nd", "3": "3rd"}


def _deaccent(s):
    """Strip diacritics ('Café' -> 'Cafe'). TVmaze/Kitsu search is
    accent-sensitive, and our catalog titles carry accents that break matches."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c))


def _query_variants(title):
    """Search-query candidates for a title: full, de-accented, first two"""
    """words, first word. Used by both TVmaze and Kitsu lookups."""
    out = [title]
    dq = _deaccent(title)
    if dq != title:
        out.append(dq)
    short = re.sub(r"[\[]\(].*?[\]\)]", " ", dq or title)
    short = re.sub(r"[^A-Za-z0-9 ]+", " ", short)
    words = [w for w in short.split() if len(w) >= 3]
    # Numeric tokens often carry the show ('86', '91 Days', '07-Ghost'); try
    # them before generic words so a wrong word-match doesn't win.
    for w in sorted({w for w in short.split() if w.isdigit() and len(w) >= 2},
                    key=len, reverse=True):
        if w.lower() != (title or "").lower():
            out.append(w)
    if len(words) >= 2 and " ".join(words[:2]).lower() != (title or "").lower():
        out.append(" ".join(words[:2]))
    if words and words[0].lower() != (title or "").lower():
        out.append(words[0])
    return out


def _ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return _ORDINALS.get(str(n % 10), f"{n}th")


def _kitsu_get(url, params=None, retries=4):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=KITSU_HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(6 + attempt * 4)
                continue
            time.sleep(2 + attempt * 2)
        except Exception:
            time.sleep(3)
    return None


def _kitsu_search(query):
    """Search Kitsu anime by text. Returns list of (id, canonicalTitle, startDate)."""
    j = _kitsu_get(f"{KITSU}/anime", params={"filter[text]": query, "page[limit]": 5})
    out = []
    if j:
        for a in j.get("data") or []:
            attrs = a.get("attributes") or {}
            out.append((a.get("id"), attrs.get("canonicalTitle") or "",
                        (attrs.get("startDate") or "")[:4]))
    return out


def _kitsu_episode_thumbs(anime_id):
    """Fetch every episode thumbnail for a Kitsu anime entry, keyed by number.
    Returns a dict {number: url} of HD thumbs, or an empty dict."""
    out = {}
    offset = 0
    while True:
        j = _kitsu_get(f"{KITSU}/anime/{anime_id}/episodes",
                       params={"page[limit]": 20, "page[offset]": offset})
        if not j or not j.get("data"):
            break
        for e in j["data"]:
            attrs = e.get("attributes") or {}
            num = attrs.get("number")
            th = (attrs.get("thumbnail") or {}).get("original") or \
                 (attrs.get("thumbnail") or {}).get("medium")
            if num is not None and th:
                out[int(num)] = th
        total = (j.get("meta") or {}).get("count") or 0
        offset += 20
        if offset >= total or len(j["data"]) < 20:
            break
        time.sleep(SLEEP)
    return out


def kitsu_fetch_thumbs(slug, title, year, season_num):
    """Kitsu fallback: returns {"1:number": thumb_url} or an error marker.

    Kitsu splits seasons into SEPARATE anime entries (exactly like our cards),
    so for a season-split card we search the base title + ordinal ('2nd Season')
    and for plain cards we search the title directly. Episodes land under
    '1:num' because each Kitsu entry is one season and our cards keep one
    season object per card."""
    base, _ = split_season_suffix(slug, title)
    queries = [title]
    if season_num is not None:
        queries = [
            f"{base} {_ordinal(season_num)} Season",
            f"{base} Season {season_num}",
            base,
        ]
    # Accent-insensitive variants too (catalog 'Café' vs Kitsu 'Cafe').
    queries = _expand(queries)
    best = None
    for q in queries:
        hits = _kitsu_search(q)
        if not hits:
            continue
        # prefer a hit whose start year matches the card, else best title ratio.
        # The catalog is 100% anime, so a Kitsu hit for an anime-title query is
        # the right show (or a franchise entry) even when the English title
        # differs from Kitsu's canonical (often Japanese) title.
        scored = []
        for aid, ctitle, cy in hits:
            s = SequenceMatcher(None, _norm(title), _norm(ctitle)).ratio()
            if year and cy and str(cy) == str(year):
                s += 0.25
            scored.append((s, aid, ctitle, cy))
        scored.sort(key=lambda x: -x[0])
        if scored and (scored[0][0] >= 0.3 or len(scored) == 1):
            best = scored[0][1]
            break
    if not best:
        return {"__error__": "no_match"}
    thumbs = _kitsu_episode_thumbs(best)
    if not thumbs:
        return {"__error__": "no_episodes"}
    return {f"1:{n}": url for n, url in sorted(thumbs.items())}


def _expand(queries):
    """Expand a query list with de-accented variants of each entry."""
    out = list(queries)
    for q in queries:
        dq = _deaccent(q)
        if dq != q:
            out.append(dq)
    return out


# Cache files only (NOT the todo files, which are lists).
CACHE_PATTERNS = ("anime_ep_thumbs.json", "anime_ep_thumbs_w*.json",
                  "anime_ep_thumbs_k*.json", "anime_ep_thumbs_r*.json",
                  "anime_ep_thumbs_m*.json")


def _cache_files():
    files = []
    for pat in CACHE_PATTERNS:
        files.extend(sorted(glob.glob(os.path.join(ROOT, pat))))
    # Todo/remain lists are lists, not {slug: thumbs} caches — exclude them.
    todo_names = {"anime_ep_thumbs_todo.json", "anime_ep_thumbs_retry.json",
                  "anime_ep_thumbs_remain.json", "anime_ep_thumbs_mtodo.json"}
    return sorted(f for f in set(files) if os.path.basename(f) not in todo_names)

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


# Trailing per-season markers our catalog appends to slugs/titles
# (cards are split by season, e.g. 'foo-season-2', 'foo-part-3', 'foo-cour-2').
SEASON_SUFFIX_RE = re.compile(r"[-\\s]?(?:season|part|cour|s)\\s*(\\d+)$", re.I)


def split_season_suffix(slug, title):
    """Detect a per-season card suffix ('-season-2', '-part-3', ...) from the
    slug and/or title. Returns (base_title, season_number) where season_number
    is None for non-season-split cards.

    TVmaze keeps every season of a show under ONE entry, so searching the full
    '... Season 2' title misses the show. We search the base title instead and
    map that show's Nth season onto this card."""
    m = SEASON_SUFFIX_RE.search(slug or "")
    if m:
        return re.sub(r"[-\\s]?(?:season|part|cour|s)\\s*\\d+$", "", (title or slug), flags=re.I), int(m.group(1))
    m = SEASON_SUFFIX_RE.search(title or "")
    if m:
        return re.sub(r"[-\\s]?(?:season|part|cour|s)\\s*\\d+$", "", (title or slug), flags=re.I), int(m.group(1))
    return title, None


def search_series(title, year):
    """Find the best TVmaze show id for a title (+year). Returns (id, score).

    TVmaze lists many anime under their Japanese titles ('Your Lie in April'
    lives there as 'Shigatsu wa Kimi no Uso') and full English titles often
    fail its search ('KONOSUBA -God's blessing...' needs just 'KONOSUBA'), so
    we (1) try the full title, (2) retry with the first two distinctive words,
    and (3) always accept the best-scoring Animation-typed result — the
    catalog is 100% anime, so an Animation hit for an anime-title query is the
    right show (or a same-franchise entry)."""
    base, _ = split_season_suffix("", title)
    title = base or title
    best, best_score = None, 0.0
    seen = set()
    for q in _query_variants(title):
        anims = _search_anims(q)
        if not anims:
            continue
        for s in anims:
            sid = s.get("id")
            if sid in seen:
                continue
            seen.add(sid)
            score = SequenceMatcher(None, _norm(title), _norm(s.get("name") or "")).ratio()
            p = (s.get("premiered") or "")[:4]
            if year and p and str(p) == str(year):
                score += 0.2
            if score > best_score:
                best, best_score = sid, score
        if best_score >= 0.5:
            break  # strong match — no need to keep searching
    if not best:
        return None, 0.0
    # The catalog is 100% anime: an Animation-typed TVmaze hit for an
    # anime-title query is the right show (or a franchise entry) even when the
    # names differ (English vs Japanese title), so accept a modest threshold.
    return best, max(best_score, 0.35)


def fetch_thumbs_for(slug, title, year):
    """Returns {"season:number": thumb_url} for a title, or an error marker.

    For per-season cards (slug/title end in '-season-2' etc.) we search the
    base show, then map that show's Nth season onto this card's own season
    index (our cards keep one season object, so episodes land under '1:num').
    Falls back to Kitsu (anime-specific, keyless) when TVmaze has no match."""
    base_title, season_num = split_season_suffix(slug, title)
    series_id, score = search_series(base_title or title, year)
    if not series_id:
        return kitsu_fetch_thumbs(slug, title, year, season_num)
    if score < 0.35:
        return kitsu_fetch_thumbs(slug, title, year, season_num)
    eps = _get_json(f"{API}/shows/{series_id}/episodes")
    if eps is None:
        return {"__error__": "fetch_failed"}
    out = {}
    for ep in eps:
        season = ep.get("season")
        num = ep.get("number")
        if season_num is not None and season != season_num:
            continue  # per-season card: only keep episodes of that season
        # TVmaze's API returns episode images under 'image.medium' (a
        # medium_landscape URL) and 'image.original' (the original_untouched
        # HD master on their CDN), so we rewrite the medium URL in place to
        # grab the true HD (often 1920x1080) still.
        img = (ep.get("image") or {}).get("medium_landscape") or \
              (ep.get("image") or {}).get("original") or \
              (ep.get("image") or {}).get("medium")
        if img and isinstance(img, str) and "medium_landscape" in img:
            img = img.replace("/medium_landscape/", "/original_untouched/")
        if season and num and img and isinstance(img, str):
            key_season = 1 if season_num is not None else season
            out[f"{key_season}:{num}"] = img
    if not out:
        # TVmaze matched the show but has no episode images: try Kitsu.
        return kitsu_fetch_thumbs(slug, title, year, season_num)
    return out


def plan_todo(todo_path=None):
    """Recompute the todo file: every slug that has at least one named episode
    but no thumbnail data yet, in stable catalog order."""
    todo_path = todo_path or TODO_FILE
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
    save_json(todo_path, todo)
    print(f"PLAN: {len(todo)} titles need thumbnail lookups", flush=True)


def fetch_window(chunk, offset=0, cache_file=None, todo_path=None):
    todo_path = todo_path or TODO_FILE
    todo = load_json(todo_path)
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
            m = re.search(r"(\\d{4})", str(year))
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
    episode[\"thumb\"], keyed by \"<season-index>:<episode-number>\"."""
    data = load_json(DATA_FILE)
    merged = {}
    files = _cache_files()
    print(f"merging {len(files)} cache files", flush=True)
    for fname in files:
        cache = load_json(fname) or {}
        # Skip any stray list-shaped file (todo lists, etc.).
        if not isinstance(cache, dict):
            continue
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
                # Never attach a still to a not-yet-released (TBC) episode.
                if ep.get("released") is False:
                    continue
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
    ap.add_argument("--todo", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        apply_thumbs()
        return
    if args.plan:
        plan_todo(args.todo)
    if args.fetch:
        fetch_window(args.fetch, offset=args.offset, cache_file=args.cache,
                     todo_path=args.todo)
    if not (args.apply or args.plan or args.fetch):
        ap.print_help()


if __name__ == "__main__":
    main()
