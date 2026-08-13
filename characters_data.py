# Character index for the "Know Your Characters" page.
#
# Loads anime_characters_index.json (built by scripts/fetch_characters.py
# phase C) and exposes a fast normalized search over every
# (anime, character) pair with their Japanese (sub) and English (dub)
# voice actors. Mirrors anime_data.py: a plain JSON file loaded at import
# time, hot-reloadable so the background collection loop can grow the
# index without a restart.
import json
import os
import re
import threading

_DIR = os.path.dirname(os.path.abspath(__file__))
_INDEX_PATH = os.path.join(_DIR, "anime_characters_index.json")

# Internal fields (_nname / _ntitle / word sets) are precomputed at load so
# every keystroke of search doesn't re-normalize 100k+ strings.
characters_index = []


def _norm(s):
    return _SEP_RE.sub(" ", s.translate(_APOS).lower()).strip()


_APOS = str.maketrans({"'": "", "\u2019": "", "\u2018": ""})
_SEP_RE = re.compile(r"[\s\-–—:;,.!?/\\()\[\]\"]+")


def _load():
    global characters_index
    try:
        with open(_INDEX_PATH, "r", encoding="utf-8") as _f:
            data = json.load(_f)
    except (OSError, ValueError):
        data = []
    for e in data:
        e["_nname"] = _norm(e.get("name") or "")
        e["_ntitle"] = _norm(e.get("title") or "")
        e["_nname_set"] = frozenset(e["_nname"].split())
        e["_ntitle_set"] = frozenset(e["_ntitle"].split())
    characters_index = data


_load()

_reload_lock = threading.Lock()


def reload_characters():
    """Reload the index file into memory. Called by the background
    collection loop so newly-fetched characters / voice actors show up
    without restarting the app."""
    global characters_index
    with _reload_lock:
        _load()


def _score(e, qn, words):
    """Relevance: exact/substring matches on the character NAME beat anime
    TITLE matches, and full-word matches beat mid-word substrings, so
    "goku" ranks Son Goku ahead of Nadeko Sengoku."""
    nname = e["_nname"]
    ntitle = e["_ntitle"]
    nname_set = e["_nname_set"]
    ntitle_set = e["_ntitle_set"]

    s = 0
    if qn in nname:
        s += 4
    if qn in ntitle:
        s += 2
    if all(w in nname_set for w in words):
        s += 3
    if all(w in ntitle_set for w in words):
        s += 2
    # Every query word appears somewhere (name or title), e.g.
    # "one piece luffy" -> luffy in the name, one+piece in the title.
    if all(w in nname_set or w in ntitle_set for w in words):
        s += 1
    # Lenient fallback for partial words across name/title.
    if s == 0 and all(w in nname or w in ntitle for w in words):
        s = 1
    return s


def search_characters(q, offset=0, limit=60):
    """Return up to `limit` (anime, character) entries matching `q`,
    most relevant first (ties keep the members-first index order).

    Matches against the character name AND the anime title (so searching
    "naruto" surfaces Naruto's cast). Same punctuation-tolerant matching
    as the main /api/search. Empty query returns the most-popular entries.
    """
    q = (q or "").strip()
    if not q:
        return characters_index[offset:offset + limit]

    qn = _norm(q)
    words = [w for w in qn.split() if w]
    if not words:
        return []

    scored = []
    for e in characters_index:
        s = _score(e, qn, words)
        if s:
            scored.append((s, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [e for _, e in scored[offset:offset + limit]]
