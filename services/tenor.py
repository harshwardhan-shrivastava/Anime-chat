"""
Thin wrapper around the Tenor v2 API so the frontend can search Tenor's
entire GIF library instead of the small hardcoded demo set.

Get a free key at https://tenor.com/gifapi/documentation (via the Google
Cloud console) and set it as TENOR_API_KEY in your environment / .env.
"""

import os
import requests

TENOR_BASE = "https://tenor.googleapis.com/v2"
CLIENT_KEY = "unanime_app"


def _api_key():
    return os.environ.get("TENOR_API_KEY", "").strip()


def is_configured():
    return bool(_api_key())


def search(query, limit=24, pos=None):
    key = _api_key()
    if not key:
        return {"error": "TENOR_API_KEY not configured"}

    params = {
        "q": query,
        "key": key,
        "client_key": CLIENT_KEY,
        "limit": limit,
        "media_filter": "gif,tinygif",
        "contentfilter": "medium",
    }
    if pos:
        params["pos"] = pos

    resp = requests.get(f"{TENOR_BASE}/search", params=params, timeout=8)
    resp.raise_for_status()
    return resp.json()


def trending(limit=24, pos=None):
    key = _api_key()
    if not key:
        return {"error": "TENOR_API_KEY not configured"}

    params = {
        "key": key,
        "client_key": CLIENT_KEY,
        "limit": limit,
        "media_filter": "gif,tinygif",
        "contentfilter": "medium",
    }
    if pos:
        params["pos"] = pos

    resp = requests.get(f"{TENOR_BASE}/featured", params=params, timeout=8)
    resp.raise_for_status()
    return resp.json()


def simplify(tenor_json):
    """Reduces Tenor's verbose payload down to just what the picker needs."""

    results = []
    for item in tenor_json.get("results", []):
        media = item.get("media_formats", {})
        gif = media.get("gif", {})
        tiny = media.get("tinygif", {})

        if not gif.get("url"):
            continue

        results.append({
            "id": item.get("id"),
            "title": item.get("content_description", ""),
            "url": gif["url"],
            "preview": tiny.get("url", gif["url"]),
            "width": gif.get("dims", [0, 0])[0],
            "height": gif.get("dims", [0, 0])[1],
        })

    return {
        "results": results,
        "next": tenor_json.get("next", ""),
    }
