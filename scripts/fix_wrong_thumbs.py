#!/usr/bin/env python3
"""Strip cross-contaminated episode thumbnails.

The first-pass matcher assigned one TVmaze show's stills to many unrelated
catalog cards (e.g. 'You and Idol Precure♪' images ended up on 'You and I Are
Polar Opposites Season 2'). Signature of the bug: the SAME image URL appears
on episodes of 2+ different anime slugs — at most one of them can be the real
owner, so every sharing card is suspect and gets its thumb stripped.

Usage:
    python3 scripts/fix_wrong_thumbs.py            # strip + write re-match todo
    python3 scripts/match_ep_thumbs.py --match N --offset M \\
        --cache anime_ep_thumbs_fx0.json --todo anime_ep_thumbs_fixtodo.json
    python3 scripts/enrich_ep_thumbnails.py --apply
"""

import glob
import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    data = load_json(DATA_FILE)
    if not data:
        print("no data"); return

    # 1. Which URLs are shared across different slugs?
    url_to_slugs = defaultdict(set)
    for slug, e in data.items():
        for s in e.get("seasons") or []:
            for ep in s.get("episodes") or []:
                t = ep.get("thumb")
                if t:
                    url_to_slugs[t].add(slug)
    bad_urls = {u for u, sl in url_to_slugs.items() if len(sl) > 1}
    print("shared (contaminated) URLs:", len(bad_urls), flush=True)

    # 2. Strip those thumbs from every episode.
    stripped_eps = 0
    affected_slugs = set()
    for slug, e in data.items():
        changed = False
        for s in e.get("seasons") or []:
            for ep in s.get("episodes") or []:
                if ep.get("thumb") in bad_urls:
                    ep.pop("thumb", None)
                    stripped_eps += 1
                    changed = True
        if changed:
            affected_slugs.add(slug)
    save_json(DATA_FILE, data)
    print("stripped thumbs:", stripped_eps, "| affected slugs:", len(affected_slugs), flush=True)

    # 3. Scrub the same URLs out of every resume cache so --apply can't
    #    re-inject them.
    scrubbed_files = 0
    scrubbed_urls = 0
    for fname in sorted(glob.glob(os.path.join(ROOT, "anime_ep_thumbs*.json"))):
        if "fixtodo" in fname or "mtodo" in fname or os.path.basename(fname) in ("anime_ep_thumbs_zzfix.json",):
            continue
        cache = load_json(fname)
        if not isinstance(cache, dict):
            continue
        changed = False
        for slug, thumbs in cache.items():
            if not isinstance(thumbs, dict):
                continue
            for k in [k for k, v in thumbs.items() if isinstance(v, str) and v in bad_urls]:
                thumbs.pop(k, None)
                scrubbed_urls += 1
                changed = True
        if changed:
            save_json(fname, cache)
            scrubbed_files += 1
    print("scrubbed cache files:", scrubbed_files, "| cache urls removed:", scrubbed_urls, flush=True)

    # 4. Emit the re-match todo (multi-episode slugs only).
    multi = [s for s in sorted(affected_slugs)
             if sum(1 for se in (data.get(s) or {}).get("seasons") or []
                    for ep in se.get("episodes") or [] if ep.get("title")) >= 2]
    todo_path = os.path.join(ROOT, "anime_ep_thumbs_fixtodo.json")
    save_json(todo_path, multi)
    print("re-match todo (multi-ep slugs):", len(multi), flush=True)


if __name__ == "__main__":
    main()
