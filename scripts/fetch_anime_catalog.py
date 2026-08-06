#!/usr/bin/env python3
"""
Fetch thousands of anime from the free AniList GraphQL API and regenerate
anime_data.py.

How to use (chunked, so it survives short-lived shells):

    1) Fetch raw pages in chunks (each run resumes from the cache):
         python3 scripts/fetch_anime_catalog.py --fetch 1 90
         python3 scripts/fetch_anime_catalog.py --fetch 91 180
         python3 scripts/fetch_anime_catalog.py --fetch 181 300

    2) Build anime_data.py from the cached pages:
         python3 scripts/fetch_anime_catalog.py --build

The existing hand-curated entries (Demon Slayer, One Piece, ...) are
preserved EXACTLY, except their poster/banner images are upgraded to the
official AniList/MyAnimeList CDN images via a title search. New titles are
appended. Raw API results are cached in anime_catalog_raw.json.

No API key needed. AniList rate limit is 90 requests/min.
"""

import argparse
import json
import os
import re
import random
import sys
import time

import requests

API_URL = "https://graphql.anilist.co"
RAW_CACHE = "anime_catalog_raw.json"
OFFICIAL_CACHE = "anime_official_images.json"
STREAMING_CACHE = "anime_streaming.json"
SCHEDULE_CACHE = "anime_schedule.json"
# anime_data.py is now a thin loader over this JSON file (a giant Python
# literal was OOM-killing the app in low-memory containers).
OUT_FILE = "anime_data.json"

TARGET_NEW = 15000     # add this many NEW entries beyond the existing ones
MAX_RAW = 45000        # stop fetching once we have this many raw candidates
PAGE_SIZE = 50
SLEEP = 1.0            # seconds between API calls (rate limit is 90/min)

QUERY = """
query ($page: Int, $perPage: Int, $sort: [MediaSort]) {
  Page(page: $page, perPage: $perPage) {
    media(sort: $sort, type: ANIME, isAdult: false) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      bannerImage
      description
      averageScore
      episodes
      duration
      status
      seasonYear
      genres
      studios(isMain: true) { nodes { name } }
      source
      format
      favourites
    }
  }
}
"""

# AniList caps plain Page() paging at 5000 entries total ("Page depth
# exceeds maximum allowed"). To walk the ENTIRE database we paginate by
# ID windows instead: each window is 100 pages x 50 titles (5000 entries,
# under the cap), then we advance id_greater_than past the last ID seen.
ID_QUERY = """
query ($page: Int, $perPage: Int, $after: Int) {
  Page(page: $page, perPage: $perPage) {
    media(id_greater_than: $after, sort: ID_ASC, type: ANIME, isAdult: false) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      bannerImage
      description
      averageScore
      episodes
      duration
      status
      seasonYear
      genres
      studios(isMain: true) { nodes { name } }
      source
      format
      favourites
    }
  }
}
"""

SEASON_QUERY = """
query ($page: Int, $perPage: Int, $season: MediaSeason, $year: Int) {
  Page(page: $page, perPage: $perPage) {
    media(season: $season, seasonYear: $year, sort: POPULARITY_DESC,
          type: ANIME, isAdult: false) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      bannerImage
      description
      averageScore
      episodes
      duration
      status
      seasonYear
      genres
      studios(isMain: true) { nodes { name } }
      source
      format
      favourites
    }
  }
}
"""

SEARCH_QUERY = """
query ($q: String) {
  Page(page: 1, perPage: 1) {
    media(search: $q, type: ANIME, isAdult: false) {
      id
      title { romaji english }
      coverImage { extraLarge large }
      bannerImage
    }
  }
}
"""

STREAMING_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      title { romaji english }
      streamingEpisodes {
        title
        url
        site
      }
    }
  }
}
"""

SCHEDULE_QUERY = """
query ($ids: [Int]) {
  Page(page: 1, perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      status
      startDate { year month day }
      nextAiringEpisode {
        episode
        airingAt
        timeUntilAiring
      }
    }
  }
}
"""

# Platforms we recognise as legitimate streaming services (sub/dub flags are
# a reasonable generalisation: these services carry English dubs broadly).
STREAM_SITES = {
    "crunchyroll": ("Crunchyroll", True),
    "netflix": ("Netflix", True),
    "hulu": ("Hulu", True),
    "hidive": ("HIDIVE", True),
    "funimation": ("Funimation", True),
    "amazon": ("Amazon Prime Video", True),
    "primevideo": ("Amazon Prime Video", True),
    "disneyplus": ("Disney+", True),
    "disney+": ("Disney+", True),
    "youtube": ("YouTube", False),
    "bilibili": ("Bilibili", False),
}

TYPE_MAP = {
    "TV": "TV Anime",
    "MOVIE": "Movie",
    "OVA": "OVA",
    "ONA": "ONA",
    "SPECIAL": "Special",
    "TV_SHORT": "TV Short",
    "MUSIC": "Music",
}

STATUS_MAP = {
    "FINISHED": "Completed",
    "RELEASING": "Ongoing",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}

SOURCE_MAP = {
    "MANGA": "Manga",
    "LIGHT_NOVEL": "Light Novel",
    "NOVEL": "Novel",
    "WEB_MANGA": "Web Manga",
    "ORIGINAL": "Original",
    "GAME": "Game",
    "VISUAL_NOVEL": "Visual Novel",
    "MUSIC": "Music",
    "OTHER": "Other",
    "COMIC": "Comic",
    "PICTURE_BOOK": "Picture Book",
}


def slugify(title):
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")


def norm(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def clean_html(text):
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&#039;|&apos;", "'", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1000]


def first_sentence(text):
    if not text:
        return ""
    m = re.search(r"([^.!?]+[.!?])", text)
    sent = (m.group(1) if m else text).strip()
    return sent[:120]


def fetch_page(page, sort):
    resp = requests.post(
        API_URL,
        json={
            "query": QUERY,
            "variables": {"page": page, "perPage": PAGE_SIZE, "sort": [sort]},
        },
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error on page {page} ({sort}): {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def fetch_schedule_batch(ids):
    """Fetch startDate + nextAiringEpisode for a batch of media ids (max ~50)."""
    resp = requests.post(
        API_URL,
        json={"query": SCHEDULE_QUERY, "variables": {"ids": ids}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList schedule error: {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def fetch_streaming_batch(ids):
    """Fetch streamingEpisodes for a batch of media ids (max ~50)."""
    resp = requests.post(
        API_URL,
        json={"query": STREAMING_QUERY, "variables": {"ids": ids}},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList streaming error: {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def platform_info(site, url):
    """Map a streaming episode to (platform_name, has_dub) using the site
    field or the URL host. Returns (None, False) for unknown hosts."""
    host = (site or url or "").lower()
    for key, value in STREAM_SITES.items():
        if key in host:
            return value
    return (None, False)


def fetch_id_window_page(page, after):
    """Fetch one page of anime with ids greater than `after` (ID_ASC)."""
    resp = requests.post(
        API_URL,
        json={
            "query": ID_QUERY,
            "variables": {"page": page, "perPage": PAGE_SIZE, "after": after},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error (id> {after} p{page}): {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def fetch_season_page(page, year, season):
    """Fetch one page of anime from a specific season/year."""
    resp = requests.post(
        API_URL,
        json={
            "query": SEASON_QUERY,
            "variables": {"page": page, "perPage": PAGE_SIZE,
                          "season": season, "year": year},
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error ({season} {year} p{page}): {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def search_title(title):
    """Returns the top AniList media match for a title (for poster upgrades)."""
    resp = requests.post(
        API_URL,
        json={"query": SEARCH_QUERY, "variables": {"q": title}},
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        return None
    media = data.get("data", {}).get("Page", {}).get("media", [])
    return media[0] if media else None


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path, obj):
    # Atomic write: dump to a temp file then rename, so a process kill can
    # never leave a truncated/poisoned cache behind.
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def build_entry(m):
    title = (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or "Unknown"
    slug = slugify(title)
    synopsis = clean_html(m.get("description"))
    genres = m.get("genres") or []
    studios = ((m.get("studios") or {}).get("nodes") or [])
    studio = studios[0].get("name", "") if studios else "Unknown Studio"
    score = m.get("averageScore")
    cover = (m.get("coverImage") or {})
    image = cover.get("large") or cover.get("extraLarge") or ""
    banner = m.get("bannerImage") or image
    tagline = first_sentence(synopsis)
    if not tagline:
        tagline = f"Join the {title} community and share the hype."
    quote = first_sentence(synopsis)
    quote_author = title

    return {
        "title": title,
        "slug": slug,
        "image": image,
        "banner": banner,
        "type": TYPE_MAP.get(m.get("format"), "TV Anime"),
        "tagline": tagline,
        "rating": f"{score / 20:.1f}" if score else "N/A",
        "review_count": 0,
        "synopsis": synopsis or "A fan-favorite anime. Join the community and discuss it!",
        "status": STATUS_MAP.get(m.get("status"), "Completed"),
        "studio": studio,
        "release": str(m.get("seasonYear") or ""),
        "genre": " \u2022 ".join(genres) if genres else "Anime",
        "source": SOURCE_MAP.get(m.get("source"), m.get("source") or "Original"),
        "duration": f"{m.get('duration') or 24} min",
        "total_episodes": m.get("episodes") or 0,
        "member_count": m.get("favourites") or 0,
        "message_count": 0,
        "favorite_count": m.get("favourites") or 0,
        "anilist_id": m.get("id"),
        "quote": quote,
        "quote_author": quote_author,
        "dub": [],
        "subtitles": [],
        "watch_order": [],
        "streaming": [],
        "seasons": [],
        "recommendations": [],
    }


def upgrade_original_images(existing):
    """Replace the poster/banner of hand-curated entries with official CDN
    images, found by searching AniList for each title. Results are cached so
    re-runs are free. Hand-crafted data is left untouched."""
    cache = load_json(OFFICIAL_CACHE)
    changed = 0
    for slug, entry in existing.items():
        # Entries whose image is already a full URL came from AniList, so
        # their poster is already official CDN -- nothing to upgrade. Only
        # hand-crafted entries with local filenames need a title search.
        if (entry.get("image") or "").startswith(("http://", "https://")):
            continue
        title = entry.get("title", slug)
        if slug in cache:
            hit = cache[slug]
        else:
            hit = None
            for attempt in range(3):
                try:
                    hit = search_title(title)
                    break
                except Exception as exc:
                    print(f"  search failed for {title}: {exc}", flush=True)
                    time.sleep(10 + attempt * 10)
            # Only cache successful hits -- failed lookups are retried on
            # the next build once the rate limit cools down.
            if hit is not None:
                cache[slug] = hit
            time.sleep(SLEEP)
        if not hit:
            continue
        cover = (hit.get("coverImage") or {})
        image = cover.get("large") or cover.get("extraLarge") or entry.get("image", "")
        banner = hit.get("bannerImage") or image
        if image.startswith("http"):
            entry["image"] = image
            entry["banner"] = banner
            changed += 1
    save_json(OFFICIAL_CACHE, cache)
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch", nargs=2, type=int, metavar=("START", "END"),
                        help="fetch raw pages START..END into the cache")
    parser.add_argument("--sort", default="POPULARITY_DESC",
                        choices=["POPULARITY_DESC", "SCORE_DESC", "TRENDING_DESC",
                                 "FAVOURITES_DESC", "ID_DESC", "ID_ASC"],
                        help="sort order for the --fetch window")
    parser.add_argument("--build", action="store_true",
                        help="build anime_data.py from the cached pages")
    parser.add_argument("--streaming", nargs="?", type=int, const=500,
                        metavar="N",
                        help="fetch legal streaming platforms for the top N titles "
                             "(resumable via the cache)")
    parser.add_argument("--streaming-all", action="store_true",
                        help="fetch legal streaming platforms for EVERY title in the "
                             "catalog (walks all anilist_ids, resumable via the cache)")
    parser.add_argument("--schedule", action="store_true",
                        help="fetch airing schedule (next episode + start date) for "
                             "all Ongoing/Upcoming titles (resumable via the cache)")
    parser.add_argument("--seasons", nargs=2, type=int, metavar=("START_YEAR", "END_YEAR"),
                        help="fetch EVERY anime season year-by-year (resumable via the "
                             "cache); re-run until it prints DONE")
    parser.add_argument("--idfetch", nargs=3, type=int, metavar=("AFTER", "START", "END"),
                        help="fetch pages START..END of the id_greater_than=AFTER window "
                             "(ID_ASC, 100-page windows of 5000 titles stay under the "
                             "AniList page-depth cap); resumable via the cache")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import anime_data  # noqa: E402

    # The ENTIRE current catalog is the base to preserve; only the hand-
    # curated originals (seasons/dub/streaming) get official-poster upgrades.
    current = dict(anime_data.anime_database)
    hand_curated = {
        k: v
        for k, v in current.items()
        if v.get("seasons") or v.get("dub") or v.get("streaming")
    }

    if args.fetch:
        start, end = args.fetch
        sort = args.sort
        cache = load_json(RAW_CACHE)
        for page in range(start, end + 1):
            key = f"{sort}:{page}"
            if key in cache:
                continue
            for attempt in range(3):
                try:
                    media = fetch_page(page, sort)
                    cache[key] = media
                    save_json(RAW_CACHE, cache)
                    print(f"  {sort} page {page}: {len(media)} titles", flush=True)
                    break
                except Exception as exc:
                    print(f"  {sort} page {page} attempt {attempt + 1} failed: {exc}", flush=True)
                    time.sleep(15 + attempt * 15)
            else:
                print(f"  {sort} page {page}: giving up after retries", flush=True)
            time.sleep(SLEEP)
        print(f"Fetched pages {start}-{end} ({sort}). Cache now has {len(cache)} pages.", flush=True)
        return

    if args.streaming_all:
        # Walk the ENTIRE catalog: every title with an anilist_id, not just
        # the top popularity pages. Resumable -- batches save per run.
        ids = []
        seen = set()
        for entry in anime_data.anime_database.values():
            aid = entry.get("anilist_id")
            if aid and aid not in seen:
                seen.add(aid)
                ids.append(aid)
        print(f"Streaming-all: {len(ids)} unique catalog ids", flush=True)

        stream_cache = load_json(STREAMING_CACHE)
        pending = [i for i in ids if str(i) not in stream_cache]
        print(f"  {len(ids) - len(pending)} already cached, "
              f"{len(pending)} pending", flush=True)

        for i in range(0, len(pending), 50):
            batch = pending[i:i + 50]
            for attempt in range(3):
                try:
                    media = fetch_streaming_batch(batch)
                    for m in media:
                        stream_cache[str(m["id"])] = (m.get("streamingEpisodes") or [])[:5]
                    save_json(STREAMING_CACHE, stream_cache)
                    print(f"  batch {i // 50 + 1}: {len(media)} titles enriched",
                          flush=True)
                    break
                except Exception as exc:
                    print(f"  batch {i // 50 + 1} attempt {attempt + 1} failed: {exc}",
                          flush=True)
                    time.sleep(15 + attempt * 15)
            time.sleep(SLEEP)
        print(f"Streaming-all done. Cache covers {len(stream_cache)} titles.",
              flush=True)
        return

    if args.streaming:
        top_n = args.streaming
        cache = load_json(RAW_CACHE)
        # Top titles = popularity pages in order (already cached). Early runs
        # stored them under plain numeric keys ("1".."100"); later runs use
        # "POPULARITY_DESC:1..100".
        ids = []
        seen = set()
        for page in range(1, 200):
            items = cache.get(str(page), []) or cache.get(f"POPULARITY_DESC:{page}", [])
            for m in items:
                mid = m.get("id")
                if mid and mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
            if len(ids) >= top_n:
                break
        ids = ids[:top_n]

        stream_cache = load_json(STREAMING_CACHE)
        pending = [i for i in ids if str(i) not in stream_cache]
        print(f"Streaming enrichment: {len(ids)} top titles, "
              f"{len(pending)} pending ({len(ids) - len(pending)} already cached)",
              flush=True)

        for i in range(0, len(pending), 50):
            batch = pending[i:i + 50]
            for attempt in range(3):
                try:
                    media = fetch_streaming_batch(batch)
                    for m in media:
                        # Cap episodes per title: we only need the platforms.
                        stream_cache[str(m["id"])] = (m.get("streamingEpisodes") or [])[:5]
                    save_json(STREAMING_CACHE, stream_cache)
                    print(f"  batch {i // 50 + 1}: {len(media)} titles enriched",
                          flush=True)
                    break
                except Exception as exc:
                    print(f"  batch {i // 50 + 1} attempt {attempt + 1} failed: {exc}",
                          flush=True)
                    time.sleep(15 + attempt * 15)
            time.sleep(SLEEP)
        print(f"Streaming enrichment done. Cache covers {len(stream_cache)} titles.",
              flush=True)
        return

    if args.schedule:
        # Ongoing/Upcoming titles from the current catalog on disk.
        ids = []
        seen = set()
        for entry in anime_data.anime_database.values():
            if entry.get("status") not in ("Ongoing", "Upcoming"):
                continue
            aid = entry.get("anilist_id")
            if aid and aid not in seen:
                seen.add(aid)
                ids.append(aid)
        print(f"Schedule enrichment: {len(ids)} Ongoing/Upcoming titles", flush=True)

        cache = load_json(SCHEDULE_CACHE)
        pending = [i for i in ids if str(i) not in cache]
        print(f"  {len(pending)} pending ({len(ids) - len(pending)} already cached)",
              flush=True)

        for i in range(0, len(pending), 50):
            batch = pending[i:i + 50]
            for attempt in range(3):
                try:
                    media = fetch_schedule_batch(batch)
                    for m in media:
                        cache[str(m["id"])] = {
                            "status": m.get("status"),
                            "startDate": m.get("startDate"),
                            "nextAiringEpisode": m.get("nextAiringEpisode"),
                        }
                    save_json(SCHEDULE_CACHE, cache)
                    print(f"  batch {i // 50 + 1}: {len(media)} titles", flush=True)
                    break
                except Exception as exc:
                    print(f"  batch {i // 50 + 1} attempt {attempt + 1} failed: {exc}",
                          flush=True)
                    time.sleep(15 + attempt * 15)
            time.sleep(SLEEP)
        print(f"Schedule enrichment done. Cache covers {len(cache)} titles.",
              flush=True)
        return

    if args.idfetch:
        after, start, end = args.idfetch
        cache = load_json(RAW_CACHE)
        for page in range(start, end + 1):
            key = f"ID:{after}:{page}"
            if key in cache:
                continue
            for attempt in range(3):
                try:
                    media = fetch_id_window_page(page, after)
                    cache[key] = media
                    save_json(RAW_CACHE, cache)
                    print(f"  id>{after} page {page}: {len(media)} titles", flush=True)
                    break
                except Exception as exc:
                    print(f"  id>{after} page {page} attempt {attempt + 1} failed: {exc}",
                          flush=True)
                    time.sleep(15 + attempt * 15)
            else:
                print(f"  id>{after} page {page}: giving up after retries", flush=True)
            time.sleep(SLEEP)
        print(f"Fetched id>{after} pages {start}-{end}. Cache now has {len(cache)} keys.",
              flush=True)
        return

    if args.seasons:
        start_year, end_year = args.seasons
        seasons = ["WINTER", "SPRING", "SUMMER", "FALL"]
        cache = load_json(RAW_CACHE)
        for year in range(start_year, end_year + 1):
            for season in seasons:
                done_key = f"SEASON_DONE:{year}:{season}"
                if cache.get(done_key):
                    continue
                for page in range(1, 16):
                    key = f"SEASON:{year}:{season}:{page}"
                    if key in cache:
                        if len(cache[key]) < PAGE_SIZE:
                            break  # season already fully fetched
                        continue
                    for attempt in range(3):
                        try:
                            media = fetch_season_page(page, year, season)
                            cache[key] = media
                            save_json(RAW_CACHE, cache)
                            print(f"  {season} {year} p{page}: {len(media)}",
                                  flush=True)
                            break
                        except Exception as exc:
                            print(f"  {season} {year} p{page} attempt {attempt + 1} "
                                  f"failed: {exc}", flush=True)
                            time.sleep(15 + attempt * 15)
                    else:
                        print(f"  {season} {year} p{page}: giving up", flush=True)
                    time.sleep(SLEEP)
                    if len(cache.get(key, [])) < PAGE_SIZE:
                        break
                cache[done_key] = True
                save_json(RAW_CACHE, cache)
        print(f"Seasons {start_year}-{end_year} DONE. Cache has {len(cache)} keys.",
              flush=True)
        return

    if args.build:
        existing_titles = {norm(e.get("title", "")) for e in current.values()}
        existing_slugs = set(current.keys())
        for e in current.values():
            if e.get("slug"):
                existing_slugs.add(e["slug"])

        cache = load_json(RAW_CACHE)
        raw_items = []
        seen_ids = set()
        for key in cache:
            items = cache[key]
            if not isinstance(items, list):
                continue  # marker keys like SEASON_DONE:...
            for m in items:
                if m.get("id") in seen_ids:
                    continue
                seen_ids.add(m.get("id"))
                raw_items.append(m)

        print(f"Existing entries: {len(current)} | raw candidates: {len(raw_items)}")

        # Official posters for the hand-curated originals.
        print("Upgrading original posters to official CDN images...")
        changed = upgrade_original_images(hand_curated)
        print(f"  upgraded {changed}/{len(hand_curated)} originals")

        # Dedupe new titles against existing + each other: exact-title match
        # only (bucketed by 3-char prefix so this stays O(N)). Franchise
        # entries ("One Piece" vs "One Piece Film: Red") are distinct
        # titles with their own community pages, so containment matches are
        # intentionally NOT used -- they would wrongly drop them.
        prefix_buckets = {}
        for used in existing_titles:
            prefix_buckets.setdefault(used[:3], set()).add(used)
        new_entries = []
        used_slugs = set(existing_slugs)
        for m in raw_items:
            if len(new_entries) >= TARGET_NEW:
                break
            title = (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or ""
            if not title:
                continue
            slug = slugify(title)
            t_norm = norm(title)
            if not t_norm or len(t_norm) < 2:
                continue
            bucket = prefix_buckets.setdefault(t_norm[:3], set())
            if t_norm in bucket or slug in used_slugs:
                continue
            entry = build_entry(m)
            bucket.add(t_norm)
            used_slugs.add(slug)
            new_entries.append(entry)

        print(f"New entries to add: {len(new_entries)}")

        # Efficient recommendations: shuffle the pool once, walk it per entry.
        all_entries = list(current.values()) + new_entries
        pool = [e for e in all_entries if e.get("image")]
        random.seed(42)
        random.shuffle(pool)
        idx = 0
        for entry in new_entries:
            picks = []
            while len(picks) < 6 and len(pool) > 0:
                cand = pool[idx % len(pool)]
                idx += 1
                if cand["slug"] != entry["slug"]:
                    picks.append(cand)
            entry["recommendations"] = [
                {"slug": e["slug"], "title": e["title"], "image": e["image"]}
                for e in picks
            ]

        merged = dict(current)
        merged.update({e["slug"]: e for e in new_entries})

        # Attach AniList ids to the hand-curated originals (from the
        # official-image search cache) so streaming data can be matched.
        official = load_json(OFFICIAL_CACHE)
        for slug, entry in hand_curated.items():
            hit = official.get(slug)
            if isinstance(hit, dict) and hit.get("id"):
                entry["anilist_id"] = hit["id"]

        # Merge airing-schedule info (next episode airing + start date) for
        # Ongoing/Upcoming titles so New/Upcoming pages show live info.
        sched_cache = load_json(SCHEDULE_CACHE)
        sched_merged = 0
        for slug, entry in merged.items():
            aid = entry.get("anilist_id")
            info = sched_cache.get(str(aid)) if aid else None
            if not info:
                continue
            nxt = info.get("nextAiringEpisode") or {}
            if nxt.get("episode"):
                entry["next_episode"] = nxt["episode"]
            if nxt.get("airingAt"):
                entry["next_episode_at"] = nxt["airingAt"]
            sd = info.get("startDate") or {}
            if sd.get("year"):
                entry["start_year"] = sd.get("year")
            if sd.get("month"):
                entry["start_month"] = sd.get("month")
            if sd.get("day"):
                entry["start_day"] = sd.get("day")
            sched_merged += 1
        print(f"  entries with airing schedule: {sched_merged}", flush=True)

        # Merge legal streaming platforms (name + Sub/Dub + region note) for
        # any entry with a cached enrichment. Hand-curated streaming stays.
        stream_cache = load_json(STREAMING_CACHE)
        for slug, entry in merged.items():
            if entry.get("streaming"):
                continue
            aid = entry.get("anilist_id")
            if not aid or str(aid) not in stream_cache:
                continue
            services = {}
            for ep in stream_cache[str(aid)]:
                name, has_dub = platform_info(ep.get("site"), ep.get("url"))
                if not name or name in services:
                    continue
                services[name] = {
                    "name": name,
                    "status": "Sub • Dub" if has_dub else "Sub",
                    "regions": ["Availability varies by region"],
                }
            if services:
                entry["streaming"] = list(services.values())
        enriched = sum(1 for e in merged.values() if e.get("streaming"))
        print(f"  entries with streaming platforms: {enriched}", flush=True)

        with open(OUT_FILE, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, separators=(",", ":"))

        print(f"WROTE {OUT_FILE} with {len(merged)} total entries.")


if __name__ == "__main__":
    main()
