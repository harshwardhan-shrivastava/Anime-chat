#!/usr/bin/env python3
"""Fetch characters + voice actors (with photos) for the whole catalog
from AniList.

Why AniList (not Behind the Voice Actors / Fandom): BTVA has no official
API and scraping it is fragile/against ToS. AniList GraphQL returns every
character's Japanese AND English (and other) voice actors per anime for
free, no key, includes the actors' official staff photos, and our catalog
is already keyed by AniList IDs.

Note: AniList's Media-side voiceActors(language:) filter is broken (it
returns the same cast for both languages), so we fetch VAs character-side
via `characters(id_in:)` and split by languageV2 ourselves.

Pipeline (each phase is resumable via a progress file):
  A. For every anime, fetch its character list (id, name, image, role,
     short description)  -> anime_characters.json
  B. For every unique character id, fetch their voice actors per media
     with staff photos -> anime_character_va.json
  C. Join into a flat per-(anime, character) index with resolved JP/EN
     VAs -> anime_characters_index.json

Usage:
  python3 scripts/fetch_characters.py a        # phase A only
  python3 scripts/fetch_characters.py b        # phase B only
  python3 scripts/fetch_characters.py c        # rebuild the joined index
  python3 scripts/fetch_characters.py slice 170   # time-budgeted run
                                                   # (alternates A+B, then C)

Run repeatedly; it resumes where it left off. Phase A works through the
catalog most-popular-first and Phase B covers the most popular characters
first, so the character page fills with well-known shows before obscure
ones.
"""
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from scripts.common import load_json, save_json as _save_json  # noqa: E402

ANIME_DATA = os.path.join(ROOT, "anime_data.json")
CHARS = os.path.join(ROOT, "anime_characters.json")
CHAR_PROGRESS = os.path.join(ROOT, "anime_characters_progress.json")
VA = os.path.join(ROOT, "anime_character_va.json")
VA_PROGRESS = os.path.join(ROOT, "anime_character_va_progress.json")
INDEX = os.path.join(ROOT, "anime_characters_index.json")

API = "https://graphql.anilist.co"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
BATCH = 50  # AniList allows 50 ids per query
PER_ANIME_CHARS = 12  # characters per anime (ROLE-sorted)
PER_CHAR_MEDIA = 30  # media edges per character
# AniList's sustained limit is 90 req/min. These heavy queries take ~10s
# each on AniList's side, so many workers are needed to get close to the
# limit; the sleep in post() + 429 backoff keep us from exceeding it.
WORKERS = 16

PHASE_A_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      characters(perPage: 12, sort: ROLE) {
        edges {
          node {
            id
            name { full }
            image { large }
            description
          }
          role
        }
      }
    }
  }
}
"""

PHASE_B_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    characters(id_in: $ids) {
      id
      media(perPage: 30) {
        edges {
          node { id }
          characterRole
          voiceActors(sort: RELEVANCE) {
            id
            name { full }
            languageV2
            image { large }
          }
        }
      }
    }
  }
}
"""


def post(query, variables, sleep_after=0.55):
    """POST to AniList with pacing + 429/5xx backoff. Thread-safe."""
    for attempt in range(8):
        try:
            r = requests.post(API, json={"query": query, "variables": variables},
                              headers=HEADERS, timeout=45)
        except requests.RequestException:
            time.sleep(2)
            continue
        if r.status_code == 200:
            d = r.json()
            if "errors" in d:
                msg = d["errors"][0].get("message", "")
                # Rate-limit style errors retry with a long cool-down.
                if "rate" in msg.lower() or "too many" in msg.lower():
                    time.sleep(10)
                    continue
                raise RuntimeError(msg[:200])
            time.sleep(sleep_after)  # stay under the 90 req/min sustained limit
            return d
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(20, 5 * (attempt + 1)))
            continue
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("persistent AniList failure")


def _clean_desc(desc):
    if not desc:
        return ""
    # Strip HTML tags and AniList's __Bold__ markers, collapse whitespace.
    text = re.sub(r"<[^>]+>", " ", desc)
    text = re.sub(r"__([^_]*)__", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 240:
        text = text[:237].rsplit(" ", 1)[0] + "…"
    return text


def save_json(path, data):
    """Character caches are huge, so they're written without whitespace."""
    _save_json(path, data, compact=True)


def _anime_todo(anime, chars, progress):
    """Remaining (slug, anilist_id) pairs, most-popular-first."""
    done_ids = set(progress)
    todo = []
    for slug, entry in anime.items():
        aid = entry.get("anilist_id")
        if aid and aid not in done_ids and slug not in chars:
            todo.append((slug, aid, entry.get("member_count", 0) or 0))
    todo.sort(key=lambda t: t[2], reverse=True)
    return [(slug, aid) for slug, aid, _ in todo]


def _char_popularity(chars, anime):
    """char id -> summed member_count across catalog anime they appear in."""
    pop = {}
    for slug, lst in chars.items():
        members = (anime.get(slug) or {}).get("member_count", 0) or 0
        for c in lst:
            pop[c["id"]] = pop.get(c["id"], 0) + members
    return pop


def _char_todo(chars, progress, pop=None):
    """Remaining unique character ids. When `pop` is given, most popular
    characters (by summed catalog popularity) come first."""
    char_ids = set()
    for lst in chars.values():
        for c in lst:
            char_ids.add(c["id"])
    todo = list(char_ids - set(progress))
    if pop:
        todo.sort(key=lambda cid: pop.get(cid, 0), reverse=True)
    else:
        todo.sort()
    return todo


def _catalog_media_ids():
    anime = load_json(ANIME_DATA, {})
    return {int(e.get("anilist_id")) for e in anime.values() if e.get("anilist_id")}


# ---------------------------------------------------------------------------
# Batch fetchers (called from the thread pool; no shared state mutated here)
# ---------------------------------------------------------------------------

def _fetch_a_batch(batch):
    """Fetch one batch of anime character lists.

    Returns [(aid, slug, char_list_or_None), ...] -- every aid in the batch
    is returned so callers can mark it done even when no characters exist.
    """
    ids = [aid for _, aid in batch]
    d = post(PHASE_A_QUERY, {"ids": ids})
    out = []
    for media in d["data"]["Page"]["media"] or []:
        if not media:
            continue
        aid = media["id"]
        slug = next((s for s, a in batch if a == aid), None)
        edges = media["characters"]["edges"] or []
        char_list = []
        for e in edges:
            node = e.get("node")
            if not node:
                continue
            role = e.get("role")
            if role not in ("MAIN", "SUPPORTING"):
                continue
            char_list.append({
                "id": node["id"],
                "name": (node.get("name") or {}).get("full") or "",
                "image": ((node.get("image") or {}).get("large") or "") or "",
                "role": role,
                "desc": _clean_desc(node.get("description")),
            })
        out.append((aid, slug, char_list if char_list else None))
    return out


def _va_entry(va_actor):
    return {
        "name": (va_actor.get("name") or {}).get("full") or "",
        "image": ((va_actor.get("image") or {}).get("large") or "") or "",
    }


def _fetch_b_batch(ids, catalog_media):
    """Fetch one batch of character voice-actor maps.

    Returns [(cid, media_map), ...] where media_map[str(media_id)] =
    {"jp": [{"name","image"},...], "en": [...]}.
    """
    d = post(PHASE_B_QUERY, {"ids": ids})
    out = []
    for ch in d["data"]["Page"]["characters"] or []:
        if not ch:
            continue
        cid = ch["id"]
        edges = ch["media"]["edges"] or []
        media_map = {}
        for e in edges:
            mid = (e.get("node") or {}).get("id")
            if not mid or mid not in catalog_media:
                continue
            jp, en = [], []
            jp_names, en_names = set(), set()
            for va_actor in e.get("voiceActors") or []:
                name = (va_actor.get("name") or {}).get("full")
                lang = va_actor.get("languageV2")
                if not name:
                    continue
                if lang == "Japanese" and name not in jp_names:
                    jp_names.add(name)
                    jp.append(_va_entry(va_actor))
                elif lang == "English" and name not in en_names:
                    en_names.add(name)
                    en.append(_va_entry(va_actor))
            if jp or en:
                media_map[str(mid)] = {"jp": jp, "en": en}
        if media_map:
            out.append((cid, media_map))
    return out


def _run(fn, items, workers=WORKERS):
    """Run fn over every item in a thread pool; returns per-item results
    in order, or None for a failed item."""
    results = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, item): i for i, item in enumerate(items)}
        for fut in as_completed(futures):
            idx = futures[fut]
            try:
                results[idx] = fut.result()
            except RuntimeError as exc:
                print(f"  batch failed (will retry later): {exc}", flush=True)
    return results


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def phase_a():
    """Per-anime character lists (most popular first)."""
    anime = load_json(ANIME_DATA, {})
    chars = load_json(CHARS, {})
    progress = load_json(CHAR_PROGRESS, [])

    todo = _anime_todo(anime, chars, progress)
    print(f"[A] {len(todo)} anime remaining", flush=True)
    done_ids = set(progress)

    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        for row in _run(_fetch_a_batch, [batch])[0] or []:
            aid, slug, char_list = row
            done_ids.add(aid)
            if slug and char_list:
                chars[slug] = char_list
        if (i // BATCH) % 3 == 0 or i + BATCH >= len(todo):
            save_json(CHARS, chars)
            save_json(CHAR_PROGRESS, sorted(done_ids))
            print(f"[A] saved {len(chars)} anime after batch {i // BATCH + 1}", flush=True)
    save_json(CHARS, chars)
    save_json(CHAR_PROGRESS, sorted(done_ids))
    print(f"[A] DONE: {len(chars)} anime with characters", flush=True)


def phase_b():
    """Voice actors (with photos) per character per media."""
    chars = load_json(CHARS, {})
    va = load_json(VA, {})
    progress = load_json(VA_PROGRESS, [])
    anime = load_json(ANIME_DATA, {})

    pop = _char_popularity(chars, anime)
    todo = _char_todo(chars, progress, pop)
    print(f"[B] {len(todo)} unique characters remaining", flush=True)
    catalog_media = _catalog_media_ids()

    for i in range(0, len(todo), BATCH):
        ids = todo[i:i + BATCH]
        results = _run(lambda ids_: _fetch_b_batch(ids_, catalog_media), [ids])[0] or []
        for cid, media_map in results:
            va[str(cid)] = media_map
            progress.append(cid)
        if (i // BATCH) % 3 == 0 or i + BATCH >= len(todo):
            save_json(VA, va)
            save_json(VA_PROGRESS, progress)
            print(f"[B] saved {len(va)} chars after batch {i // BATCH + 1}", flush=True)
    save_json(VA, va)
    save_json(VA_PROGRESS, progress)
    print(f"[B] DONE: {len(va)} characters with VAs", flush=True)


def _norm_va_list(lst):
    """Normalize VA lists written by older pipeline versions (plain name
    strings) into the current [{name, image}, ...] shape."""
    out = []
    for item in lst or []:
        if isinstance(item, str):
            out.append({"name": item, "image": ""})
        elif isinstance(item, dict) and item.get("name"):
            out.append({"name": item["name"], "image": item.get("image") or ""})
    return out


def phase_c():
    """Join into a flat index: one entry per (anime, character)."""
    chars = load_json(CHARS, {})
    va = load_json(VA, {})
    anime = load_json(ANIME_DATA, {})

    index = []
    for slug, lst in chars.items():
        entry = anime.get(slug) or {}
        title = entry.get("title") or slug
        members = entry.get("member_count", 0) or 0
        for c in lst:
            media_map = va.get(str(c["id"])) or {}
            # Prefer VAs for this exact anime, else first available.
            aid = str(entry.get("anilist_id"))
            entry_va = media_map.get(aid) or next(iter(media_map.values()), None) or {}
            index.append({
                "id": c["id"],
                "name": c["name"],
                "image": c["image"],
                "role": c["role"],
                "desc": c["desc"],
                "slug": slug,
                "title": title,
                "members": members,
                "jp": _norm_va_list(entry_va.get("jp")),
                "en": _norm_va_list(entry_va.get("en")),
            })
    # Most-popular anime first so the site's initial grid is the good stuff.
    index.sort(key=lambda e: e["members"], reverse=True)
    save_json(INDEX, index)
    print(f"[C] DONE: {len(index)} (anime, character) entries", flush=True)


POP_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      popularity
    }
  }
}
"""


def phase_p():
    """Backfill member_count (AniList popularity) for catalog entries that
    are missing it. Many top shows (One Piece, Naruto, Haikyuu...) have 0,
    which breaks every popularity ordering: the VA fetch queue, the
    character page's "Most Popular Casts" grid, and the Popular browse
    view. Runs once because the patched values stop being missing.
    """
    anime = load_json(ANIME_DATA, {})
    todo = [
        (slug, entry["anilist_id"])
        for slug, entry in anime.items()
        if not (entry.get("member_count") or 0) and entry.get("anilist_id")
    ]
    print(f"[P] {len(todo)} anime missing member_count", flush=True)
    if not todo:
        return
    patched = 0
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        d = post(POP_QUERY, {"ids": [aid for _, aid in batch]}, sleep_after=0.4)
        for media in d["data"]["Page"]["media"] or []:
            if not media or not media.get("popularity"):
                continue
            slug = next((s for s, a in batch if a == media["id"]), None)
            if slug and slug in anime:
                anime[slug]["member_count"] = media["popularity"]
                patched += 1
        if (i // BATCH) % 2 == 0 or i + BATCH >= len(todo):
            save_json(ANIME_DATA, anime)
            print(f"[P] patched {patched} after batch {i // BATCH + 1}", flush=True)
    save_json(ANIME_DATA, anime)
    print(f"[P] DONE: {patched} anime member_count backfilled", flush=True)


def run_slice(budget_seconds):
    """Alternate phase A + phase B batches (in parallel) until the time
    budget runs out, then rebuild the index. Used by the app's background
    loop so the collection grows in small slices without hammering AniList."""
    deadline = time.time() + max(10, budget_seconds)

    anime = load_json(ANIME_DATA, {})
    # Popularity drives the character/VA priority order, so backfill it
    # first if the catalog is missing it for top shows.
    missing = sum(
        1 for e in anime.values()
        if not (e.get("member_count") or 0) and e.get("anilist_id")
    )
    if missing:
        phase_p()
        anime = load_json(ANIME_DATA, {})
    chars = load_json(CHARS, {})
    a_progress = load_json(CHAR_PROGRESS, [])
    va = load_json(VA, {})
    b_progress = load_json(VA_PROGRESS, [])
    catalog_media = _catalog_media_ids()
    pop = _char_popularity(chars, anime)

    a_todo = _anime_todo(anime, chars, a_progress)
    b_todo = _char_todo(chars, b_progress, pop)
    print(f"[slice] A remaining: {len(a_todo)} | B remaining: {len(b_todo)}", flush=True)

    done_ids = set(a_progress)
    a_i, b_i = 0, 0
    saved_a = saved_b = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while time.time() < deadline:
            work = []
            if a_i < len(a_todo) and len(work) < 2:
                work.append(("a", a_todo[a_i:a_i + BATCH]))
                a_i += BATCH
            if b_i < len(b_todo) and len(work) < 2:
                work.append(("b", b_todo[b_i:b_i + BATCH]))
                b_i += BATCH
            if not work:
                break

            futures = {}
            for kind, payload in work:
                if kind == "a":
                    futures[pool.submit(_fetch_a_batch, payload)] = kind
                else:
                    futures[pool.submit(_fetch_b_batch, payload, catalog_media)] = kind

            for fut in as_completed(futures):
                kind = futures[fut]
                try:
                    rows = fut.result()
                except RuntimeError as exc:
                    print(f"[slice] {kind} batch failed (continuing): {exc}", flush=True)
                    continue
                if kind == "a":
                    for aid, slug, char_list in rows or []:
                        done_ids.add(aid)
                        if slug and char_list:
                            chars[slug] = char_list
                            saved_a += 1
                else:
                    for cid, media_map in rows or []:
                        va[str(cid)] = media_map
                        b_progress.append(cid)
                        saved_b += 1

            # Persist after every round so a killed slice never loses work.
            save_json(CHARS, chars)
            save_json(CHAR_PROGRESS, sorted(done_ids))
            save_json(VA, va)
            save_json(VA_PROGRESS, b_progress)

    remaining_a = len(a_todo) - a_i
    remaining_b = len(b_todo) - b_i
    print(f"[slice] done: +{saved_a} anime chars, +{saved_b} VA maps "
          f"(A left: {remaining_a}, B left: {remaining_b})", flush=True)

    # Rebuild the joined index whenever anything changed so the site always
    # reflects the latest collection state, not just completion.
    if saved_a or saved_b:
        phase_c()
    return not (remaining_a <= 0 and remaining_b <= 0)


if __name__ == "__main__":
    args = sys.argv[1:]
    phase = args[0] if args else "a"
    if phase == "a":
        phase_a()
    elif phase == "b":
        phase_b()
    elif phase == "c":
        phase_c()
    elif phase == "p":
        phase_p()
    elif phase == "slice":
        budget = int(args[1]) if len(args) > 1 else 150
        run_slice(budget)
    else:
        print("usage: python3 scripts/fetch_characters.py [a|b|c|slice N]")
