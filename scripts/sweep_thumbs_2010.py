"""Sweep the catalog for 2010+ shows with missing episode thumbs and fill
them from TVmaze (titles + HD stills).

Usage:
    python3 scripts/sweep_thumbs_2010.py                 # all shows
    python3 scripts/sweep_thumbs_2010.py --offset 500     # resume at index
    python3 scripts/sweep_thumbs_2010.py --limit 300      # first 300 only

The sweep is idempotent: already-thumbed episodes are skipped, so running it
again just fills whatever is still missing.
"""
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")

from scripts.enrich_airing import DATA_FILE, _backfill_one, load_json, save_json

WORKERS = 8
SAVE_EVERY = 100


def aired_count(entry):
    """Number of episodes that should have aired for this card."""
    nxt = entry.get("next_episode")
    if nxt:
        return max(0, nxt - 1)
    total = entry.get("total_episodes") or 0
    if total:
        return total
    return sum(len(s.get("episodes") or []) for s in entry.get("seasons") or [])


def missing(entry, aired):
    eps = 0
    miss = 0
    for si, s in enumerate(entry.get("seasons") or []):
        for ep in s.get("episodes") or []:
            eps += 1
            if (ep.get("number") or 1) > aired:
                continue
            if not ep.get("thumb"):
                miss += 1
    return eps, miss


def main():
    offset = 0
    limit = 0
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--offset" and i + 1 < len(args):
            offset = int(args[i + 1])
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    data = load_json(DATA_FILE)

    targets = []
    for slug, entry in data.items():
        rel = entry.get("release") or ""
        m = re.search(r"(\d{4})", str(rel))
        if not m or int(m.group(1)) < 2010:
            continue
        aired = aired_count(entry)
        if aired <= 0:
            continue
        _, miss = missing(entry, aired)
        if miss > 0:
            targets.append((slug, aired, miss))

    if "--shuffle" in args:
        random.shuffle(targets)
    else:
        targets.sort(key=lambda t: -t[2])  # biggest gaps first
    print(f"2010+ shows missing aired thumbs: {len(targets)}", flush=True)

    if limit:
        targets = targets[offset:offset + limit]
    elif offset:
        targets = targets[offset:]
    print(f"sweeping {len(targets)} shows (offset={offset})", flush=True)

    total_t = 0
    total_th = 0
    done = 0

    def work(slug, aired):
        try:
            return slug, _backfill_one(data[slug], aired)
        except Exception as exc:
            return slug, (0, 0)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(work, slug, aired) for slug, aired, _ in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            slug, (t, th) = fut.result()
            total_t += t
            total_th += th
            if t or th:
                done += 1
            if i % SAVE_EVERY == 0 or i == len(targets):
                save_json(DATA_FILE, data)
                print(f"  {i}/{len(targets)} | filled={done} titles={total_t} thumbs={total_th}", flush=True)

    save_json(DATA_FILE, data)
    print(f"DONE sweep: {done} shows touched, {total_t} titles, {total_th} thumbs")


if __name__ == "__main__":
    main()
