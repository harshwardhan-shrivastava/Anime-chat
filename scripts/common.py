"""Helpers shared by the enrichment / maintenance scripts.

Every script used to carry its own copy of the same JSON cache loader, the
atomic saver and a "GET with retries" wrapper. They now live here so a fix
(e.g. a new rate-limit rule) applies to every script at once.
"""

import json
import os
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DATA_FILE = "anime_data.json"

# AniList airing status -> the label the site displays.
STATUS_MAP = {
    "FINISHED": "Completed",
    "RELEASING": "Ongoing",
    "NOT_YET_RELEASED": "Upcoming",
    "CANCELLED": "Cancelled",
    "HIATUS": "On Hiatus",
}

# Platforms we recognise as legitimate streaming services (the dub flag is a
# reasonable generalisation: these services carry English dubs broadly).
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


def chdir_root():
    """Run relative to the repo root, whatever directory the script was
    started from, so `anime_data.json` & friends always resolve."""
    os.chdir(ROOT)


def load_json(path, default=None):
    """Parse `path`, or return `default` when the file doesn't exist.

    A file that exists but holds broken JSON raises — a corrupted cache is a
    bug worth surfacing, not something to silently overwrite. Use `read_json`
    where a corrupted file should be treated as missing instead.
    """
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def read_json(path, default=None):
    """Lenient `load_json`: any read/parse failure yields `default`."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj, compact=False):
    """Atomic write: dump to a temp file then rename, so a process kill can
    never leave a truncated/poisoned cache behind."""
    tmp = path + ".tmp"
    separators = (",", ":") if compact else None
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=separators)
    os.replace(tmp, path)


def request_json(url, params=None, headers=None, retries=4, timeout=25):
    """GET `url` and return parsed JSON, or None once the retries run out.

    Backs off harder on 429 (rate limit) than on other failures, which is what
    every public API used here (Kitsu, Jikan, TVmaze) wants.
    """
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                time.sleep(6 + attempt * 4)
                continue
            time.sleep(2 + attempt * 2)
        except Exception:
            time.sleep(3)
    return None


def get_json(url, timeout=15, default=None):
    """Single-shot GET for endpoints that need no retry/backoff (TVmaze)."""
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            return default
        return r.json()
    except Exception:
        return default
