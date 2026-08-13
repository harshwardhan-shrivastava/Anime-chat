#!/usr/bin/env python3
"""Fetch characters + voice actors for the whole catalog from AniList.

Why AniList (not Behind the Voice Actors): BTVA has no official API and
scraping it is fragile/against ToS. AniList GraphQL returns every
character's Japanese AND English (and other) voice actors per anime for
free, no key, and our catalog is already keyed by AniList IDs.

Pipeline (each phase is resumable via a progress file):
  A. For every anime, fetch its character list (id, name, image, role,
     short description)  -> anime_characters.json
  B. For every unique character id, fetch their voice actors per media
     (the Character-side query is the one that actually returns VAs;
     Media-side voiceActors is null on AniList) -> anime_character_va.json
  C. Join into a flat per-(anime, character) index with resolved JP/EN
     VAs -> anime_characters_index.json

Usage:
  python3 scripts/fetch_characters.py a        # phase A only
  python3 scripts/fetch_characters.py b        # phase B only
  python3 scripts/fetch_characters.py c        # rebuild the joined index
  python3 scripts/fetch_characters.py slice 170   # time-budgeted run
                                                   # (alternates A+B, then C)

Run repeatedly; it resumes where it left off. Phase A works through the
catalog most-popular-first so the character page fills with well-known
shows before obscure ones.
"""
import json
import os
import re
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANIME_DATA = os.path.join(ROOT, "anime_data.json")
CHARS = os.path.join(ROOT, "anime_characters.json")
CHAR_PROGRESS = os.path.join(ROOT, "anime_characters_progress.json")
VA = os.path.join(ROOT, "anime_character_va.json")
VA_PROGRESS = os.path.join(ROOT, "anime_character_va_progress.json")
INDEX = os.path.join(ROOT, "anime_characters_index.json")

API = "https://graphql.anilist.co"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}
BATCH = 25  # AniList allows 25 ids per query
PER_ANIME_CHARS = 12  # characters per anime (ROLE-sorted)
PER_CHAR_MEDIA = 30  # media edges per character

PHASE_A_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 25) {
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
  Page(perPage: 25) {
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
          }
        }
      }
    }
  }
}
"""


def post(query, variables):
    """POST to AniList with pacing (90 req/min limit) + 429 backoff."""
    for attempt in range(8):
        try:
            r = requests.post(API, json={"query": query, "variables": variables},
                              headers=HEADERS, timeout=30)
        except requests.RequestException:
            time.sleep(3)
            continue
        if r.status_code == 200:
            d = r.json()
            if "errors" in d:
                msg = d["errors"][0].get("message", "")
                # Rate-limit style errors retry with a long cool-down.
                if "rate" in msg.lower() or "too many" in msg.lower():
                    time.sleep(12)
                    continue
                raise RuntimeError(msg[:200])
            time.sleep(0.65)  # stay under the 90 req/min sustained limit
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


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    """Atomic write (tmp + rename) so a mid-write kill can't corrupt the file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def _anime_todo(anime, chars, progress):
    """Remaining (slug, anilist_id) pairs, most-popular-first.

    Popularity (member_count) is only a sort key: an anime with no member
    count still gets collected, just later.
    """
    done_ids = set(progress)
    todo = []
    for slug, entry in anime.items():
        aid = entry.get("anilist_id")
        if aid and aid not in done_ids and slug not in chars:
            todo.append((slug, aid, entry.get("member_count", 0) or 0))
    todo.sort(key=lambda t: t[2], reverse=True)
    return [(slug, aid) for slug, aid, _ in todo]


def _process_a_batch(batch, chars, done_ids, anime):
    """Fetch one batch of anime character lists. Returns number of anime saved."""
    ids = [aid for _, aid in batch]
    d = post(PHASE_A_QUERY, {"ids": ids})
    saved = 0
    for media in d["data"]["Page"]["media"] or []:
        if not media:
            continue
        aid = media["id"]
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
        # The media entry came back, so mark it done even if it had no
        # MAIN/SUPPORTING characters -- otherwise it would be re-fetched
        # on every single run forever.
        slug = next((s for s, a in batch if a == aid), None)
        if slug and char_list:
            chars[slug] = char_list
            saved += 1
        done_ids.add(aid)
    return saved


def _process_b_batch(ids, va, progress, catalog_media):
    """Fetch one batch of character voice-actor maps. Returns chars saved."""
    d = post(PHASE_B_QUERY, {"ids": ids})
    saved = 0
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
            for va_actor in e.get("voiceActors") or []:
                name = (va_actor.get("name") or {}).get("full")
                lang = va_actor.get("languageV2")
                if not name:
                    continue
                if lang == "Japanese" and name not in jp:
                    jp.append(name)
                elif lang == "English" and name not in en:
                    en.append(name)
            if jp or en:
                media_map[str(mid)] = {"jp": jp, "en": en}
        if media_map:
            va[str(cid)] = media_map
            saved += 1
        progress.append(cid)
    return saved


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
        try:
            _process_a_batch(batch, chars, done_ids, anime)
        except RuntimeError as exc:
            print(f"[A] batch failed, will retry next run: {exc}", flush=True)
            break
        if (i // BATCH) % 4 == 0 or i + BATCH >= len(todo):
            save_json(CHARS, chars)
            save_json(CHAR_PROGRESS, sorted(done_ids))
            print(f"[A] saved {len(chars)} anime after batch {i // BATCH + 1}", flush=True)
    save_json(CHARS, chars)
    save_json(CHAR_PROGRESS, sorted(done_ids))
    print(f"[A] DONE: {len(chars)} anime with characters", flush=True)


def _char_todo(chars, progress):
    """Unique character ids not yet fetched in phase B."""
    char_ids = set()
    for slug, lst in chars.items():
        for c in lst:
            char_ids.add(c["id"])
    return sorted(char_ids - set(progress))


def _catalog_media_ids():
    anime = load_json(ANIME_DATA, {})
    return {int(e.get("anilist_id")) for e in anime.values() if e.get("anilist_id")}


def phase_b():
    """Voice actors per character per media."""
    chars = load_json(CHARS, {})
    va = load_json(VA, {})
    progress = load_json(VA_PROGRESS, [])

    todo = _char_todo(chars, progress)
    print(f"[B] {len(todo)} unique characters remaining", flush=True)
    catalog_media = _catalog_media_ids()

    for i in range(0, len(todo), BATCH):
        ids = todo[i:i + BATCH]
        try:
            _process_b_batch(ids, va, progress, catalog_media)
        except RuntimeError as exc:
            print(f"[B] batch failed, will retry next run: {exc}", flush=True)
            break
        if (i // BATCH) % 4 == 0 or i + BATCH >= len(todo):
            save_json(VA, va)
            save_json(VA_PROGRESS, progress)
            print(f"[B] saved {len(va)} chars after batch {i // BATCH + 1}", flush=True)
    save_json(VA, va)
    save_json(VA_PROGRESS, progress)
    print(f"[B] DONE: {len(va)} characters with VAs", flush=True)


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
                "jp": entry_va.get("jp", []),
                "en": entry_va.get("en", []),
            })
    # Most-popular anime first so the site's initial grid is the good stuff.
    index.sort(key=lambda e: e["members"], reverse=True)
    save_json(INDEX, index)
    print(f"[C] DONE: {len(index)} (anime, character) entries", flush=True)


def run_slice(budget_seconds):
    """Alternate phase A + phase B batches until the time budget runs out,
    then rebuild the index. Used by the app's background loop so the
    collection grows in small slices without hammering AniList."""
    deadline = time.time() + max(10, budget_seconds)

    anime = load_json(ANIME_DATA, {})
    chars = load_json(CHARS, {})
    progress = load_json(CHAR_PROGRESS, [])
    va = load_json(VA, {})
    va_progress = load_json(VA_PROGRESS, [])

    a_todo = _anime_todo(anime, chars, progress)
    b_todo = _char_todo(chars, va_progress)
    print(f"[slice] A remaining: {len(a_todo)} | B remaining: {len(b_todo)}", flush=True)

    done_ids = set(progress)
    a_i, b_i = 0, 0
    saved_a = saved_b = 0
    catalog_media = _catalog_media_ids()

    # Alternate one A batch and one B batch so characters AND their voice
    # actors grow together (B has plenty of material already from phase A).
    while time.time() < deadline:
        did_work = False
        if a_i < len(a_todo):
            batch = a_todo[a_i:a_i + BATCH]
            a_i += BATCH
            try:
                saved_a += _process_a_batch(batch, chars, done_ids, anime)
                did_work = True
            except RuntimeError as exc:
                print(f"[slice] A batch failed (continuing): {exc}", flush=True)
        if time.time() < deadline and b_i < len(b_todo):
            ids = b_todo[b_i:b_i + BATCH]
            b_i += BATCH
            try:
                saved_b += _process_b_batch(ids, va, va_progress, catalog_media)
                did_work = True
            except RuntimeError as exc:
                print(f"[slice] B batch failed (continuing): {exc}", flush=True)
        if not did_work:
            break
        # Persist after every pair so a killed slice never loses work.
        save_json(CHARS, chars)
        save_json(CHAR_PROGRESS, sorted(done_ids))
        save_json(VA, va)
        save_json(VA_PROGRESS, va_progress)

    remaining_a = len(a_todo) - a_i
    remaining_b = len(b_todo) - b_i
    print(f"[slice] done: +{saved_a} anime chars, +{saved_b} VA maps "
          f"(A left: {remaining_a}, B left: {remaining_b})", flush=True)

    # Rebuild the joined index whenever anything changed so the site
    # always reflects the latest collection state, not just completion.
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
    elif phase == "slice":
        budget = int(args[1]) if len(args) > 1 else 150
        run_slice(budget)
    else:
        print("usage: python3 scripts/fetch_characters.py [a|b|c|slice N]")
