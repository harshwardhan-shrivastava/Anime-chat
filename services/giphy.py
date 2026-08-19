import os
import requests

GIPHY_BASE = "https://api.giphy.com/v1/gifs"


def _api_key():
    return os.environ.get("GIPHY_API_KEY", "").strip()


def is_configured():
    return bool(_api_key())


def search(query, limit=24, offset=0):
    key = _api_key()

    if not key:
        return {"error": "GIPHY_API_KEY not configured"}

    params = {
        "api_key": key,
        "q": query,
        "limit": limit,
        "offset": offset,
        "rating": "g",
        "lang": "en",
    }

    r = requests.get(
        f"{GIPHY_BASE}/search",
        params=params,
        timeout=8
    )

    r.raise_for_status()
    return r.json()


def trending(limit=24, offset=0):
    key = _api_key()

    if not key:
        return {"error": "GIPHY_API_KEY not configured"}

    params = {
        "api_key": key,
        "limit": limit,
        "offset": offset,
        "rating": "g",
    }

    r = requests.get(
        f"{GIPHY_BASE}/trending",
        params=params,
        timeout=8
    )

    r.raise_for_status()
    return r.json()


def simplify(giphy_json):
    results = []

    for gif in giphy_json.get("data", []):

        images = gif.get("images") or {}
        original = images.get("original") or {}

        # Skip malformed entries instead of blowing up the whole response.
        if not gif.get("id") or not original.get("url"):
            continue

        preview = images.get("fixed_width_small") or original

        try:
            width = int(original.get("width") or 0)
            height = int(original.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0

        results.append({
            "id": gif["id"],
            "title": gif.get("title", ""),
            "url": original["url"],
            "preview": preview.get("url") or original["url"],
            "width": width,
            "height": height,
        })

    return {
        "results": results,
        "next": giphy_json.get("pagination", {}).get("offset", 0)
    }