#!/usr/bin/env python3
"""
Keep currently-airing shows honest using live AniList airing data.

For every catalog entry whose baked status is Ongoing/Upcoming we fetch live
AniList data (status, total episodes, nextAiringEpisode and the full airing
schedule), then:

  * normalize the status (RELEASING -> Ongoing, FINISHED -> Completed, ...)
  * refresh next_episode / next_episode_at / start_date
  * fix total_episodes (removes the "0 Eps" chips on brand-new cours)
  * for Ongoing shows: mark episodes past the aired count as released:false
    with title "TBC" and drop their (leaked/filler) thumbnails, so only
    officially-released episodes show names + thumbs
  * backfill real episode titles for aired episodes from the MAL cache
  * create a season structure for Ongoing shows that had none yet (new cours
    such as 100 Girlfriends S3 / That Time I Got Reincarnated as a Slime S4)
  * --tvmaze: fill REAL titles + HD thumbnails for the newest aired episodes
    straight from TVmaze. The name-based matcher only fills episodes that
    already have a title, so a just-aired episode (no title cached yet) would
    stay blank; this step gives it its official TVmaze name + HD still. Also
    re-fills the stripped cross-contamination pairs (--cross).

Usage:
    python3 scripts/enrich_airing.py --plan --todo anime_airing_todo.json
    python3 scripts/enrich_airing.py --fetch 300 --offset 0 --cache anime_airing_a0.json --todo anime_airing_todo.json
    python3 scripts/enrich_airing.py --apply
    python3 scripts/enrich_airing.py --tvmaze 200 --offset 0 --todo anime_airing_todo.json
"""

import argparse
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")
MAL_FILE = os.path.join(ROOT, "anime_mal_episodes.json")
CACHE_PATTERNS = ("anime_airing_a*.json",)

API = "https://graphql.anilist.co"
TVM_API = "https://api.tvmaze.com"

STATUS_MAP = {
    "FINISHED": "Completed",
    "RELEASING": "Ongoing",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}

PAGE_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      status
      episodes
      startDate { year month day }
      nextAiringEpisode { episode airingAt }
      airingSchedule(perPage: 100) { nodes { episode airingAt } }
    }
  }
}
"""

# TVmaze HD flavors
_TVMAZE_OLD_FLAVORS = (
    "/uploads/images/medium_landscape/",
    "/uploads/images/medium/",
    "/uploads/images/original/",
)
_HD_FLAVOR = "/uploads/images/original_untouched/"

_PH_RE = re.compile(
    r"(?:^|[-\s])(?:Season|S)\s*\d+\s*-\s*Episode\s*\d+$|^Episode\s*\d+$",
    re.I,
)

_SEASON_SUFFIX_RE = re.compile(r"[-\s]?(?:season|part|cour|s)\s*(\d+)$", re.I)


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


def plan_todo(todo_path):
    """Build the todo list: [slug, anilist_id, title] for Ongoing/Upcoming."""
    data = load_json(DATA_FILE)
    todo = []
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if e.get("status") in ("Ongoing", "Upcoming") and aid:
            todo.append([slug, aid, e.get("title") or slug])
    save_json(todo_path, todo)
    print(f"PLAN: {len(todo)} airing/upcoming entries with anilist_id.")


def fetch_window(count, offset=0, cache_file=None, todo_path=None):
    todo = load_json(todo_path) if todo_path else []
    window = todo[offset:offset + count]
    if not window:
        print("Nothing to fetch in this window.")
        return
    result = {}
    for i in range(0, len(window), 50):
        chunk = window[i:i + 50]
        ids = [aid for _, aid, _ in chunk]
        try:
            r = requests.post(
                API,
                json={"query": PAGE_QUERY, "variables": {"ids": ids}},
                timeout=30,
            )
            if r.status_code == 200:
                media = r.json().get("data", {}).get("Page", {}).get("media", [])
                for m in media:
                    result[str(m["id"])] = {
                        "status": m.get("status"),
                        "episodes": m.get("episodes"),
                        "startDate": m.get("startDate") or {},
                        "nextAiringEpisode": m.get("nextAiringEpisode") or {},
                        "airingSchedule": m.get("airingSchedule") or {},
                    }
            else:
                print(f"HTTP {r.status_code} for ids {ids[:5]}... ({r.text[:120]})")
        except Exception as exc:
            print(f"Request failed for {ids[:5]}...: {exc}")
        time.sleep(0.8)

    if cache_file:
        prev = load_json(cache_file)
        prev.update(result)
        save_json(cache_file, prev)
    print(f"FETCH: {len(result)}/{len(window)} entries cached -> {cache_file}")


def _cache_files():
    files = []
    for pat in CACHE_PATTERNS:
        files.extend(sorted(glob_files(pat)))
    return sorted(set(files))


def glob_files(pattern):
    import glob

    return glob.glob(os.path.join(ROOT, pattern))


def _global_number(seasons, si, ep_number):
    """Map (season idx, ep number) to the show-global episode number."""
    offset = 0
    for i, s in enumerate(seasons):
        eps = s.get("episodes") or []
        if i == si:
            first = (eps[0].get("number") or 1) if eps else 1
            if i == 0 or first == 1:
                return offset + (ep_number or 0)
            return ep_number or 0
        offset += len(eps)
    return ep_number or 0


# ---------------------------------------------------------------------------
# TVmaze name + HD-thumb backfill for newest aired episodes
# ---------------------------------------------------------------------------

def _hd_url(url):
    """Return the true-HD (original_untouched) flavor of a TVmaze image URL."""
    if not isinstance(url, str):
        return url
    for old in _TVMAZE_OLD_FLAVORS:
        if old in url:
            return url.replace(old, _HD_FLAVOR)
    return url


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _is_placeholder(title):
    """True when a title is a generated/placeholder label that should be
    replaced with the real episode name (e.g. 'Season 1 - Episode 1',
    'Episode 5', or the airing marker 'TBC')."""
    if not title:
        return True
    t = title.strip().lower()
    if t in ("tbc", "tba", "tbd", "to be released", "untitled"):
        return True
    return bool(_PH_RE.search(title))


def _season_suffix(slug):
    m = _SEASON_SUFFIX_RE.search(slug or "")
    return int(m.group(1)) if m else None


def _tvmaze_ep_image(ep):
    img = (
        (ep.get("image") or {}).get("original_untouched")
        or (ep.get("image") or {}).get("medium_landscape")
        or (ep.get("image") or {}).get("original")
        or (ep.get("image") or {}).get("medium")
    )
    return _hd_url(img) if isinstance(img, str) else None


def _search_tvmaze(title, year):
    """Best TVmaze show id for a title."""
    from difflib import SequenceMatcher

    base = re.sub(r"[-\s]?(?:season|part|cour|s)\s*\d+$", "", title or "", flags=re.I)
    if not base:
        return None
    try:
        r = requests.get(
            "%s/singlesearch/shows?q=%s" % (TVM_API, base),
            timeout=6,
        )
        if r.status_code == 200:
            show = r.json() or {}
            nm = _norm(show.get("name") or "")
            bn = _norm(base)
            if nm and bn and (bn == nm or bn in nm or nm in bn
                              or SequenceMatcher(None, bn, nm).ratio() >= 0.55):
                return show.get("id")
        return None
    except Exception:
        return None


def _tvmaze_episodes(sid):
    try:
        r = requests.get("%s/shows/%s/episodes" % (TVM_API, sid), timeout=8)
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:
        return []


# Verified alternate-name aliases: TVmaze lists some shows under a different
# title (e.g. 'Chainsmoker Cat' -> 'Yani Neko'). Each entry is hand-verified so
# no fuzzy matching is involved and wrong-anime thumbs can't sneak in.
TVMAZE_ALIASES = {
    "chainsmoker-cat": 92274,  # Yani Neko
}


def _pick_tvmaze_season(eps_by_season, slug, named_hits, our_seasons=0):
    """Choose the TVmaze season matching this card."""
    if named_hits:
        counts = {}
        for s in named_hits:
            counts[s] = counts.get(s, 0) + 1
        return max(counts, key=counts.get)
    s = _season_suffix(slug)
    if s is not None:
        return s
    # No season suffix in the slug: fall back to the first TVmaze season only
    # for single-season cards (e.g. 'From Overshadowed to Overpowered'). For
    # multi-season cards (One Piece, Conan, long-runners) the season mapping is
    # ambiguous, so skip -- positional fills there could land on the wrong
    # season's stills.
    if our_seasons == 1 and eps_by_season:
        return min(eps_by_season)
    return None


def _backfill_one(entry, aired):
    """Fill real titles + HD thumbs for aired-but-missing episodes of a card."""
    slug = entry.get("slug") or ""
    title = entry.get("title") or slug
    year = entry.get("release") or ""
    y = None
    m = re.search(r"(\d{4})", str(year))
    if m:
        y = int(m.group(1))

    sid = _search_tvmaze(title, y)
    if not sid:
        # Hand-verified alternate-name alias (see TVMAZE_ALIASES).
        sid = TVMAZE_ALIASES.get(slug)
    if not sid:
        return 0, 0
    eps = _tvmaze_episodes(sid)
    if not eps:
        return 0, 0

    by_season = {}
    for e in eps:
        by_season.setdefault(e.get("season"), []).append(e)

    named_hits = []
    for s in entry.get("seasons") or []:
        for ep in s.get("episodes") or []:
            t = ep.get("title")
            if not t or _is_placeholder(t):
                continue
            nt = _norm(t)
            if not nt:
                continue
            for tseason, teps in by_season.items():
                for te in teps:
                    if nt == _norm(te.get("name") or ""):
                        named_hits.append(tseason)
                        break

    tseason = _pick_tvmaze_season(
        by_season, slug, named_hits, our_seasons=len(entry.get("seasons") or [])
    )
    tvm = by_season.get(tseason) or []
    if not tvm:
        return 0, 0
    by_num = {}
    for e in tvm:
        by_num[e.get("number")] = e

    titles = 0
    thumbs = 0
    for si, s in enumerate(entry.get("seasons") or []):
        for ep in s.get("episodes") or []:
            if _global_number(entry.get("seasons") or [], si, ep.get("number") or 0) > aired:
                continue
            te = by_num.get(ep.get("number"))
            if not te:
                continue
            tname = te.get("name") or ""
            if (not ep.get("title") or _is_placeholder(ep.get("title"))) and tname and not _is_placeholder(tname):
                ep["title"] = tname
                titles += 1
            img = _tvmaze_ep_image(te)
            if img:
                if not ep.get("thumb"):
                    ep["thumb"] = img
                    thumbs += 1
                elif _hd_url(ep.get("thumb")) != img:
                    # The card carries a still from a different show/season
                    # (old pipeline contamination). We only reach this code
                    # with a confident show+season match (title search + exact
                    # episode-name matches), so swap in the correct still.
                    ep["thumb"] = img
                    thumbs += 1
    return titles, thumbs


def _needs_backfill(entry, aired):
    """True when some aired episode of the card lacks a title or a thumb."""
    for si, s in enumerate(entry.get("seasons") or []):
        for ep in s.get("episodes") or []:
            if _global_number(entry.get("seasons") or [], si, ep.get("number") or 0) > aired:
                continue
            if not ep.get("title") or not ep.get("thumb"):
                return True
    return False


def tvmaze_backfill(count=0, offset=0, todo_path=None, cross_path=None):
    """Fill TVmaze titles + HD thumbs for aired episodes missing them."""
    data = load_json(DATA_FILE)

    jobs = []
    todo = load_json(todo_path) if todo_path else []
    for row in todo[offset:offset + count] if count else todo[offset:]:
        slug = row[0] if isinstance(row, (list, tuple)) else row
        entry = data.get(slug)
        if not entry or entry.get("status") != "Ongoing":
            continue
        aired = entry.get("total_episodes") or 0
        nxt = entry.get("next_episode")
        if nxt:
            aired = nxt - 1
        if aired > 0 and _needs_backfill(entry, aired):
            jobs.append((slug, aired))

    cross = load_json(cross_path) if cross_path else []
    for slug in cross:
        entry = data.get(slug)
        if not entry:
            continue
        total = 0
        for s in entry.get("seasons") or []:
            total += len(s.get("episodes") or [])
        if total > 0 and _needs_backfill(entry, total):
            jobs.append((slug, total))

    if not jobs:
        print("Nothing to backfill.")
        return
    print("TVMAZE backfill jobs:", len(jobs), flush=True)

    total_t = 0
    total_th = 0
    for i, (slug, aired) in enumerate(jobs, 1):
        t, th = _backfill_one(data[slug], aired)
        total_t += t
        total_th += th
        if i % 10 == 0 or i == len(jobs):
            save_json(DATA_FILE, data)
            print(f"  {i}/{len(jobs)} | titles={total_t} thumbs={total_th}", flush=True)
        time.sleep(0.2)

    save_json(DATA_FILE, data)
    print(f"DONE TVMAZE backfill: {total_t} titles, {total_th} thumbs")


def apply_airing():
    data = load_json(DATA_FILE)
    mal = load_json(MAL_FILE)

    by_aid = {}
    for slug, e in data.items():
        aid = e.get("anilist_id")
        if aid:
            by_aid.setdefault(aid, []).append((slug, e))

    merged = {}
    for fname in _cache_files():
        merged.update(load_json(fname) or {})

    now = int(time.time())
    stats = {
        "status_fixed": 0,
        "next_fixed": 0,
        "total_fixed": 0,
        "tbc_marked": 0,
        "tbc_cleared": 0,
        "thumbs_removed": 0,
        "titles_backfilled": 0,
        "seasons_created": 0,
    }

    for aid_s, info in merged.items():
        aid = int(aid_s)
        for slug, e in by_aid.get(aid, []):
            st = STATUS_MAP.get(info.get("status"))
            if st:
                e["status"] = st
                stats["status_fixed"] += 1

            nxt = info.get("nextAiringEpisode") or {}
            if nxt.get("airingAt"):
                e["next_episode_at"] = nxt["airingAt"]
                if nxt.get("episode"):
                    e["next_episode"] = nxt["episode"]
                stats["next_fixed"] += 1
            else:
                e.pop("next_episode_at", None)

            sd = info.get("startDate") or {}
            for key in ("year", "month", "day"):
                if sd.get(key):
                    e[f"start_{key}"] = sd[key]

            nodes = (info.get("airingSchedule") or {}).get("nodes") or []
            aired = None
            # Two independent signals, take the larger:
            #  * every scheduled episode whose airing time has already passed
            #    has aired -- robust to a stale nextAiringEpisode, so a
            #    just-aired episode flips to released as soon as its timestamp
            #    passes, even before the cache refreshes;
            #  * nextAiringEpisode.episode - 1 -- robust to partial schedules
            #    (AniList only lists the current cour's airing times for
            #    long multi-cour shows like Renegade Immortal).
            done = [nd["episode"] for nd in nodes
                    if nd.get("airingAt") and nd["airingAt"] <= now]
            if done:
                aired = max(done)
            if nxt.get("episode") and (aired is None or nxt["episode"] - 1 > aired):
                aired = nxt["episode"] - 1

            total = info.get("episodes") or 0
            if not total and nodes:
                total = max((nd.get("episode") or 0) for nd in nodes)
            if total:
                e["total_episodes"] = total
                stats["total_fixed"] += 1

            mal_titles = mal.get(slug) or {}

            if st != "Ongoing":
                # The show stopped airing (finished / cancelled): any episode
                # at or before the final count really aired, so clear the
                # stale TBC markers left behind by the airing run.
                if total and st in ("Completed", "Cancelled"):
                    seasons = e.get("seasons") or []
                    for si, s in enumerate(seasons):
                        for ep in s.get("episodes") or []:
                            gnum = _global_number(seasons, si, ep.get("number") or 0)
                            if gnum <= total and (ep.get("released") is False
                                                  or ep.get("title") == "TBC"):
                                ep.pop("released", None)
                                if (not ep.get("title")
                                        and str(ep.get("number")) in mal_titles):
                                    ep["title"] = mal_titles[str(ep["number"])]
                                stats["tbc_cleared"] += 1
                continue

            if not aired:
                continue

            seasons = e.get("seasons") or []
            if not seasons:
                if not total:
                    total = aired
                seasons = [
                    {"name": "Season 1",
                     "episodes": [{"number": n} for n in range(1, total + 1)]}
                ]
                e["seasons"] = seasons
                if not e.get("watch_order"):
                    e["watch_order"] = ["Season 1"]
                stats["seasons_created"] += 1

            for si, s in enumerate(seasons):
                for ep in s.get("episodes") or []:
                    gnum = _global_number(seasons, si, ep.get("number") or 0)
                    if gnum > aired:
                        if ep.get("released") is not False:
                            ep["released"] = False
                        ep["title"] = "TBC"
                        if ep.get("thumb"):
                            ep.pop("thumb", None)
                            stats["thumbs_removed"] += 1
                        stats["tbc_marked"] += 1
                    else:
                        ep.pop("released", None)
                        # This episode has officially aired. If a previous run
                        # left a placeholder title behind ("TBC" etc.) while it
                        # was still upcoming, drop it so it no longer renders
                        # as "To be released" and can pick up the real name.
                        if _is_placeholder(ep.get("title")):
                            ep.pop("title", None)
                        if not ep.get("title") and str(ep.get("number")) in mal_titles:
                            ep["title"] = mal_titles[str(ep["number"])]
                            stats["titles_backfilled"] += 1

    save_json(DATA_FILE, data)
    print("APPLIED:")
    for k, v in stats.items():
        print(f"  {k}: {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--fetch", type=int, default=0)
    ap.add_argument("--tvmaze", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--cache", default=None)
    ap.add_argument("--todo", default=None)
    ap.add_argument("--cross", default=None)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if args.apply:
        apply_airing()
        return
    if args.plan:
        plan_todo(args.todo or "anime_airing_todo.json")
    if args.fetch:
        fetch_window(args.fetch, offset=args.offset, cache_file=args.cache,
                     todo_path=args.todo or "anime_airing_todo.json")
    if args.tvmaze:
        tvmaze_backfill(args.tvmaze, offset=args.offset,
                        todo_path=args.todo or "anime_airing_todo.json",
                        cross_path=args.cross or "anime_ep_thumbs_crosstodo.json")
    if not (args.apply or args.plan or args.fetch or args.tvmaze):
        ap.print_help()


if __name__ == "__main__":
    sys.exit(main())
