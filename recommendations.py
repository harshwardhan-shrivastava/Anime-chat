"""Personalized homepage recommendation scoring."""

import random
import re
import time
import heapq


_FRANCHISE_RE = re.compile(
    r"-(?:2nd|3rd|4th|5th|s\d+|season|part|ova|movie|film|special|tv|remake|rebirth|the-movie|the-movie-.*|\d+).*$"
)

_TIME_BUCKET_SECONDS = 30 * 60
_TOP_POOL_SIZE = 48
_IGNORED_GENRES = {"anime"}


def _genres(entry):
    return {
        genre.strip()
        for genre in (entry.get("genre") or "").split("•")
        if genre.strip() and genre.strip().lower() not in _IGNORED_GENRES
    }


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _quality_score(entry):
    rating = _number(entry.get("rating"))
    popularity = _number(entry.get("member_count"))
    return rating * 3 + min(popularity / 100000.0, 10)


def _diverse_top(pool, n):
    """Return up to n picks while keeping one entry per franchise."""
    picked = []
    seen = set()
    for score, slug in pool:
        if len(picked) >= n:
            break
        base = _FRANCHISE_RE.sub("", slug)
        if base in seen:
            continue
        seen.add(base)
        picked.append(slug)
    return picked


def _signal_sources(catalog, signals):
    profile = {}
    sources = {}

    def add_genres(genres, weight, source):
        for genre in genres:
            key = genre.strip().lower()
            if not key or key in _IGNORED_GENRES:
                continue
            profile[key] = profile.get(key, 0.0) + weight
            sources.setdefault(key, []).append((weight, source))

    history = signals.get("history") or []
    for index, item in enumerate(history):
        slug = item.get("anime_slug") if isinstance(item, dict) else item
        entry = catalog.get(slug)
        if not entry:
            continue
        # Recent views are stronger, while still allowing older interests to
        # contribute to the profile.
        weight = 1.0 / (1.0 + index * 0.22)
        add_genres(_genres(entry), weight, ("history", entry.get("title") or slug))

    list_slugs = signals.get("list_slugs") or set()
    for slug in list_slugs:
        entry = catalog.get(slug)
        if not entry:
            continue
        add_genres(_genres(entry), 1.35, ("list", entry.get("title") or slug))

    quiz_genres = signals.get("top_genres") or []
    add_genres(quiz_genres, 0.35, ("quiz", None))

    total = sum(profile.values())
    if total:
        profile = {genre: weight / total for genre, weight in profile.items()}
    return profile, sources


def _reason(entry, profile, sources):
    matched = [
        (profile.get(genre.lower(), 0), genre)
        for genre in _genres(entry)
        if genre.lower() in profile
    ]
    if not matched:
        return "Popular with fans right now"

    genre = max(matched)[1]
    source = max(
        sources.get(genre.lower(), []),
        key=lambda item: item[0],
        default=(0, None),
    )[1]
    if source:
        kind, title = source
        if kind in {"history", "list"}:
            verb = "watched" if kind == "history" else "saved"
            return f"Because you {verb} {title}"
    return f"Matches your {genre} taste"


def _card(slug, entry, reason, personalized):
    return {
        "slug": slug,
        "title": entry.get("title") or slug,
        "image": entry.get("image") or "",
        "rating": entry.get("rating") or "N/A",
        "live_rating": entry.get("rating") or "N/A",
        "year": entry.get("release") or "",
        "genre": entry.get("genre") or "",
        "total_episodes": entry.get("total_episodes", 0) or 0,
        "member_count": entry.get("member_count", 0) or 0,
        "has_sub": bool(entry.get("subtitles")),
        "has_dub": any(
            str(d).strip().lower() == "english"
            for d in (entry.get("dub") or [])
        ),
        "arc_count": len(entry.get("watch_order") or []) or len(entry.get("seasons") or []),
        "badge_label": "For You" if personalized else "Popular",
        "reason": reason,
    }


def build_recommendations(catalog, signals=None, user_id=None, limit=12, now=None):
    """Build rotating cards from catalog entries and optional user signals."""
    signals = signals or {}
    viewed = {
        item.get("anime_slug") if isinstance(item, dict) else item
        for item in (signals.get("history") or [])
    }
    saved = set(signals.get("list_slugs") or set())
    excluded = viewed | saved
    profile, sources = _signal_sources(catalog, signals)
    personalized = bool(profile)

    def scored_candidates():
        for slug, entry in catalog.items():
            if (
                not entry.get("image")
                or entry.get("status") == "Upcoming"
                or slug in excluded
            ):
                continue
            genres = _genres(entry)
            overlap = sum(profile.get(genre.lower(), 0.0) for genre in genres)
            if personalized and overlap <= 0:
                continue
            score = overlap * 100 + _quality_score(entry) * 0.9
            yield score, slug

    # Keep only the recommendation pool rather than copying the whole
    # catalog into another list.
    pool = heapq.nlargest(
        _TOP_POOL_SIZE,
        scored_candidates(),
        key=lambda item: (item[0], item[1]),
    )
    if not pool:
        return {"picks": [], "personalized": personalized}
    pool = pool[:_TOP_POOL_SIZE]
    bucket = int((time.time() if now is None else now) // _TIME_BUCKET_SECONDS)
    rng = random.Random(f"{user_id or 'anonymous'}:{bucket}")
    rng.shuffle(pool)
    slugs = _diverse_top(pool, limit)

    return {
        "picks": [
            _card(
                slug,
                catalog[slug],
                _reason(catalog[slug], profile, sources)
                if personalized
                else "Popular with fans right now",
                personalized,
            )
            for slug in slugs
        ],
        "personalized": personalized,
    }
