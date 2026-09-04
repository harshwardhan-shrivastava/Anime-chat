"""Kitsu thumbnail fallback for the airing backfill.

TVmaze publishes episode stills late for brand-new episodes (sometimes days
after they air), so an aired episode can sit thumb-less for a while. Kitsu's
anime-specific CDN usually has the still already. This module is imported
lazily from scripts/enrich_airing._backfill_one and fills only *missing*
thumbs on aired episodes, never overwriting existing ones.

Kitsu is a free, keyless JSON:API (https://kitsu.io/api/edge). Each Kitsu
anime entry corresponds to one season/cour, so episode numbers map 1:1 onto
a single-season catalog card.
"""

import re
import time

import requests

KITSU_API = "https://kitsu.io/api/edge"
KITSU_HEADERS = {
    "Accept": "application/vnd.api+json",
    "User-Agent": "Otakul/1.0 (episode-thumbnail enrichment)",
}

# Mirrors scripts/enrich_airing's markers.
_TRAILING_TAG_RE = re.compile(r"[-\\s]?(?:season|part|cour|s)\\s*\\d+$", re.I)
_SEASON_TAG_RE = re.compile(r"\\s*(?:season|part|cour|s)\\s*\\d+\\s*[:,-]?\\s*", re.I)


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _hits(query):
    try:
        r = requests.get(
            "%s/anime" % KITSU_API,
            params={"filter[text]": query, "page[limit]": 5},
            headers=KITSU_HEADERS,
            timeout=8,
        )
    except Exception:
        return []
    if r.status_code != 200:
        return []
    return (r.json() or {}).get("data") or []


def _find_id(title, year):
    """Best Kitsu anime id for a title (+year), or None.

    Candidate ladder mirrors the TVmaze search (full title -> season tags
    stripped -> head segment -> first words) with a '{title} {year}' variant
    so remakes/reboots resolve to the right decade (Kitsu lists 'Koukaku
    Kidoutai: THE GHOST IN THE SHELL' only under a year-qualified search).
    A candidate only wins when its start year matches the card's, unless the
    card has no year to check.
    """
    from difflib import SequenceMatcher

    title = title or ""
    candidates = [title]
    if year:
        candidates.append("%s %s" % (title, year))
    base = _TRAILING_TAG_RE.sub("", title).strip()
    if base and base != title:
        candidates.append(base)
    stripped = _SEASON_TAG_RE.sub("", title).strip()
    if stripped and stripped != base:
        candidates.append(stripped)
    if year:
        candidates.append("%s %s" % (stripped, year))
    for sep in (":", " - ", " \u2013 ", "-", "\u2013"):
        head = title.split(sep)[0].strip()
        if head and len(head) >= 4 and head not in candidates:
            candidates.append(head)
    words = re.sub(r"[^A-Za-z0-9 ]", " ", stripped).split()
    for n in (3, 2):
        if len(words) > n:
            cand = " ".join(words[:n])
            if cand not in candidates:
                candidates.append(cand)

    seen = set()
    for cand in candidates:
        key = _norm(cand)
        if not key or key in seen:
            continue
        seen.add(key)
        hits = _hits(cand)
        if not hits:
            continue
        best, best_score, best_year = None, 0.0, None
        for item in hits:
            at = item.get("attributes") or {}
            nm = _norm(at.get("canonicalTitle") or "")
            if not nm:
                continue
            score = SequenceMatcher(None, key, nm).ratio()
            cy = (at.get("startDate") or "")[:4]
            if year and cy == str(year):
                score += 0.25
            if score > best_score:
                best, best_score, best_year = item.get("id"), score, cy
        if best is None or best_score < 0.3:
            continue
        # A same-name franchise entry from another decade is a miss: keep
        # walking the ladder (the year-qualified candidate usually wins next).
        if year and best_year and str(best_year) != str(year):
            continue
        return best
    return None


def _episode_thumbs(anime_id):
    """Every {number: thumb-url} for a Kitsu anime entry (one season each)."""
    out = {}
    offset = 0
    while True:
        try:
            r = requests.get(
                "%s/anime/%s/episodes" % (KITSU_API, anime_id),
                params={"page[limit]": 20, "page[offset]": offset},
                headers=KITSU_HEADERS,
                timeout=10,
            )
        except Exception:
            break
        if r.status_code != 200:
            break
        rows = (r.json() or {}).get("data") or []
        if not rows:
            break
        for e in rows:
            at = e.get("attributes") or {}
            num = at.get("number")
            th = (at.get("thumbnail") or {}).get("original") or \
                 (at.get("thumbnail") or {}).get("medium")
            if num is not None and th:
                out[int(num)] = th
        offset += len(rows)
        if len(rows) < 20:
            break
        time.sleep(0.3)
    return out


def kitsu_backfill_one(entry, aired):
    """Fill Kitsu stills for aired episodes of a card that lack a thumbnail.

    Runs for single-season cards only (episode numbers then map 1:1 onto a
    Kitsu entry). Never overwrites an existing thumb and skips unreleased
    episodes. Returns (0, filled)."""
    from scripts.enrich_airing import _global_number

    seasons = entry.get("seasons") or []
    if len(seasons) != 1:
        return 0, 0
    slug = entry.get("slug") or ""
    title = entry.get("title") or slug
    year = entry.get("release") or ""
    y = None
    m = re.search(r"(\d{4})", str(year))
    if m:
        y = int(m.group(1))
    aid = _find_id(title, y)
    if not aid:
        return 0, 0
    thumbs = _episode_thumbs(aid)
    if not thumbs:
        return 0, 0
    filled = 0
    for si, s in enumerate(seasons):
        for ep in s.get("episodes") or []:
            gnum = _global_number(seasons, si, ep.get("number") or 0)
            if gnum > aired or ep.get("released") is False:
                continue
            url = thumbs.get(ep.get("number"))
            if url and not ep.get("thumb"):
                ep["thumb"] = url
                filled += 1
    return 0, filled
