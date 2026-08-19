"""Card payload builder shared by the pages that render `_anime_card.html`.

The homepage picks, the profile history and the user lists all need the same
dict shape; only the corner badge differs, so it is a parameter.
"""

from anime_data import anime_database
from database import get_anime_stats


def build_card(slug, badge_label):
    """Build a card dict for `slug`, or None when the slug is unknown.

    `live_rating` prefers our own members' votes and only falls back to the
    catalog rating while nobody has voted yet.
    """
    entry = anime_database.get(slug)
    if entry is None:
        return None
    stats = get_anime_stats(slug)
    live_rating = stats["average"] if stats["votes"] > 0 else entry.get("rating", "N/A")
    return {
        "slug": slug,
        "title": entry.get("title") or slug,
        "image": entry.get("image") or "",
        "rating": entry.get("rating") or "N/A",
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
        "live_rating": live_rating,
        "badge_label": badge_label,
    }
