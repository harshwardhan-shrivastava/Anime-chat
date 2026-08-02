#!/usr/bin/env python3
"""
Fetch ~1000+ anime from the free AniList GraphQL API and regenerate
anime_data.py.

How to use:
    python3 scripts/fetch_anime_catalog.py

The existing hand-curated entries (Demon Slayer, One Piece, ...) are
preserved EXACTLY -- only new titles are added.  Raw API results are
cached in anime_catalog_raw.json so a re-run resumes instead of
re-fetching, and the script can be run again later to refresh or grow
the catalog without any manual work.

No API key needed. AniList rate limit is 90 requests/min -- we use ~26.
"""

import json
import os
import re
import sys
import time

import requests

API_URL = "https://graphql.anilist.co"
RAW_CACHE = "anime_catalog_raw.json"
OUT_FILE = "anime_data.py"

TARGET_NEW = 1000   # add this many NEW entries beyond the existing ones
PAGES = 50          # 50 pages x 50 = 2500 raw candidates, deduped down

QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(sort: POPULARITY_DESC, type: ANIME, isAdult: false) {
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
    """Lowercased alphanumeric-only form, for title dedup."""
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


def fetch_page(page):
    resp = requests.post(
        API_URL,
        json={"query": QUERY, "variables": {"page": page, "perPage": 50}},
        timeout=25,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"AniList error on page {page}: {data['errors']}")
    return data.get("data", {}).get("Page", {}).get("media", [])


def load_raw_cache():
    if os.path.exists(RAW_CACHE):
        with open(RAW_CACHE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_raw_cache(cache):
    with open(RAW_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


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
        "quote": quote,
        "quote_author": quote_author,
        "dub": [],
        "subtitles": [],
        "watch_order": [],
        "streaming": [],
        "seasons": [],
        "recommendations": [],
    }


def main():
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import anime_data  # noqa: E402

    # Only the hand-curated originals are "existing" -- they are the only
    # entries with seasons/dub/streaming data. Auto-generated entries (from
    # earlier runs) have empty lists, so re-running this script never
    # compounds on its own output.
    existing = {
        k: v
        for k, v in anime_data.anime_database.items()
        if v.get("seasons") or v.get("dub") or v.get("streaming")
    }
    existing_titles = {norm(e.get("title", "")) for e in existing.values()}
    existing_slugs = set(existing.keys())
    for e in existing.values():
        if e.get("slug"):
            existing_slugs.add(e["slug"])

    cache = load_raw_cache()
    fetched_pages = set(cache.keys())
    print(f"Existing entries: {len(existing)} | cached pages: {len(fetched_pages)}")

    for page in range(1, PAGES + 1):
        if str(page) in fetched_pages:
            continue
        for attempt in range(4):
            try:
                media = fetch_page(page)
                cache[str(page)] = media
                save_raw_cache(cache)
                print(f"  page {page}: {len(media)} titles")
                break
            except Exception as exc:
                print(f"  page {page} attempt {attempt + 1} failed: {exc}")
                time.sleep(3 + attempt * 3)
        else:
            print(f"  page {page}: giving up after retries")
        time.sleep(0.6)

    # Build the pool of raw candidates in popularity order.
    raw_items = []
    seen_ids = set()
    for page in range(1, PAGES + 1):
        for m in cache.get(str(page), []):
            if m.get("id") in seen_ids:
                continue
            seen_ids.add(m.get("id"))
            raw_items.append(m)

    print(f"Raw candidates: {len(raw_items)}")

    # Dedupe against existing catalog + within the new pool.
    new_entries = []
    used_norms = set(existing_titles)
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
        # Skip exact/similar duplicates of titles we already have.
        skip = False
        for used in used_norms:
            if len(used) >= 4 and len(t_norm) >= 4 and (used in t_norm or t_norm in used):
                skip = True
                break
        if skip or slug in used_slugs:
            continue
        entry = build_entry(m)
        used_norms.add(t_norm)
        used_slugs.add(slug)
        new_entries.append(entry)

    print(f"New entries to add: {len(new_entries)}")

    # Fill "You May Also Like" recommendations from the merged pool.
    all_entries = list(existing.values()) + new_entries
    pool = [e for e in all_entries if e.get("image")]
    import random

    for entry in new_entries:
        picks = [e for e in pool if e["slug"] != entry["slug"]]
        random.shuffle(picks)
        entry["recommendations"] = [
            {"slug": e["slug"], "title": e["title"], "image": e["image"]}
            for e in picks[:6]
        ]

    merged = dict(existing)
    merged.update({e["slug"]: e for e in new_entries})

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(
            "# Auto-generated anime database -- %d titles (script: scripts/fetch_anime_catalog.py).\n"
            "anime_database = " % len(merged)
        )
        json.dump(merged, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"WROTE {OUT_FILE} with {len(merged)} total entries.")


if __name__ == "__main__":
    main()
