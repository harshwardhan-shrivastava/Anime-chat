# Character index for the "Know Your Characters" page.
#
# Originally loaded anime_characters_index.json (~50MB JSON, ~325MB as
# Python objects with precomputed search sets) at import time. Combined
# with the anime catalog, that pushed the app past Render's 512MB free-
# tier RAM and OOM-killed the deploy. The data is now baked into a
# read-only SQLite file (anime_characters.sqlite, built by
# scripts/build_characters_sqlite.py) and queried at runtime, so the
# characters page costs only a few MB of memory. Search keeps the same
# relevance ranking, just over SQL-filtered candidates instead of all
# 96k rows.
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH = os.path.join(_DIR, "anime_characters.sqlite")
_JSON_PATH = os.path.join(_DIR, "anime_characters_index.json")

_conn = None
_conn_ready = False
_conn_lock = threading.Lock()

_APOS = str.maketrans({"'": "", "\u2019": "", "\u2018": ""})
_SEP_RE = re.compile(r"[\s\-–—:;,.!?/\\()\[\]\"']+")


def _norm(s):
    return _SEP_RE.sub(" ", s.translate(_APOS).lower()).strip()


def _get_conn():
    global _conn, _conn_ready
    if not _conn_ready:
        with _conn_lock:
            if not _conn_ready:
                _conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
                _conn.row_factory = sqlite3.Row
                _conn_ready = True
    return _conn


def _reset_conn():
    global _conn, _conn_ready
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None
    _conn_ready = False


def _query(where_sql, args):
    try:
        cur = _get_conn().execute(f"SELECT * FROM characters {where_sql}", args)
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        print(f"[characters_data] sqlite error: {exc}", flush=True)
        return []


def _escape_like(word):
    return word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _public(row):
    """Convert a DB row to the shape the frontend expects: jp/en voice
    actors are stored as JSON strings, but templates/JS use lists."""
    row = dict(row)
    for key in ("jp", "en"):
        val = row.get(key)
        if isinstance(val, str):
            try:
                row[key] = json.loads(val)
            except (ValueError, TypeError):
                row[key] = []
    return row


def search_characters(q, offset=0, limit=60):
    """Return up to `limit` (anime, character) entries matching `q`,
    most relevant first. Same matching rules as before: character NAME
    matches beat anime TITLE matches, full-word beats mid-word. An empty
    query returns the first (popular-ordered) entries.

    Results are deduped by character NAME so searching "Goku" doesn't
    flood the grid with the same character across 60 anime. Each unique
    character shows once, preferring MAIN roles and the most popular
    anime, with an `appearances` count of how many anime they're in."""
    ensure_fresh()

    q = (q or "").strip()
    if not q:
        return [_public(r) for r in _query("ORDER BY rowid LIMIT ? OFFSET ?", (limit, offset))]

    qn = _norm(q)
    words = [w for w in qn.split() if w]
    if not words:
        return []

    # Prefilter with case-insensitive LIKE so we only score candidates.
    clauses = []
    args = []
    for w in words:
        esc = _escape_like(w)
        clauses.append("(name LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')")
        args += [f"%{esc}%", f"%{esc}%"]
    candidates = _query("WHERE " + " OR ".join(clauses), args)

    # Score every candidate, then group by normalized character name so
    # each character appears at most once in the results.
    by_name = {}
    name_counts = {}
    for e in candidates:
        nm = _norm(e.get("name") or "")
        name_counts[nm] = name_counts.get(nm, 0) + 1
        s = _score(e, qn, words)
        if not s:
            continue
        # Prefer MAIN over SUPPORTING, then higher member count.
        role_bonus = 2 if e.get("role") == "MAIN" else 0
        try:
            members = int(e.get("members") or 0)
        except (TypeError, ValueError):
            members = 0
        key_tuple = (s, role_bonus, members)
        prev = by_name.get(nm)
        if prev is None or key_tuple > prev[0]:
            by_name[nm] = (key_tuple, e)

    scored = [(t[0], t[1]) for t in by_name.values()]
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _, e in scored[offset:offset + limit]:
        pub = _public(e)
        pub["appearances"] = name_counts.get(_norm(e.get("name") or ""), 1)
        out.append(pub)
    return out


def _score(e, qn, words):
    name = _norm(e.get("name") or "")
    title = _norm(e.get("title") or "")
    name_set = frozenset(name.split())
    title_set = frozenset(title.split())

    s = 0
    if qn in name:
        s += 4
    if qn in title:
        s += 2
    if all(w in name_set for w in words):
        s += 3
    if all(w in title_set for w in words):
        s += 2
    if all(w in name_set or w in title_set for w in words):
        s += 1
    if s == 0 and all(w in name or w in title for w in words):
        s = 1
    return s


def index_stats():
    """Return (total_entries, distinct_anime_covered, entries_with_va)."""
    try:
        conn = _get_conn()
        total = conn.execute("SELECT COUNT(*) FROM characters").fetchone()[0]
        covered = conn.execute("SELECT COUNT(DISTINCT slug) FROM characters").fetchone()[0]
        with_va = conn.execute(
            "SELECT COUNT(*) FROM characters"
            " WHERE jp NOT IN ('', '[]') OR en NOT IN ('', '[]')"
        ).fetchone()[0]
        return total, covered, with_va
    except sqlite3.Error as exc:
        print(f"[characters_data] stats error: {exc}", flush=True)
        return 0, 0, 0


_JSON_MTIME = os.path.getmtime(_JSON_PATH) if os.path.exists(_JSON_PATH) else 0
_last_check = [0.0]


def _rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 9999


def _rebuild_from_json():
    """Re-bake the SQLite file when the collection loop wrote new
    characters. Runs the build in a child process so the JSON (which is
    ~325MB in RAM) never lands in this process. Skipped when memory is
    tight (e.g. Render's free tier with the catalog already resident)."""
    if _rss_mb() > 300:
        print("[characters_data] skipping index rebuild: memory tight", flush=True)
        return
    script = os.path.join(_DIR, "scripts", "build_characters_sqlite.py")
    try:
        subprocess.run([sys.executable, script], check=True, timeout=600)
        _reset_conn()
    except Exception as exc:
        print(f"[characters_data] index rebuild failed: {exc}", flush=True)


def reload_characters():
    """Reload the index if the JSON changed on disk. Called by the
    background collection loop so newly-fetched characters / voice actors
    show up without restarting the app."""
    global _JSON_MTIME
    new_mtime = os.path.getmtime(_JSON_PATH) if os.path.exists(_JSON_PATH) else 0
    if new_mtime != _JSON_MTIME:
        _rebuild_from_json()
        _JSON_MTIME = new_mtime


def ensure_fresh():
    """Cheap freshness check: one stat call, reload at most every 30s."""
    now = time.time()
    if now - _last_check[0] < 30:
        return
    _last_check[0] = now
    new_mtime = os.path.getmtime(_JSON_PATH) if os.path.exists(_JSON_PATH) else 0
    if new_mtime != _JSON_MTIME:
        reload_characters()
