#!/usr/bin/env python3
"""Resume-safe TVmaze backfill sweep.

Processes N jobs (count) starting at a global job offset, saving the catalog
after EVERY job so a run that hits a terminal timeout never loses progress.
Already-fixed shows are skipped quickly (their _needs_backfill becomes False),
so re-running at offset 0 is always safe.

A hard wall-clock guard (signal alarm) bounds each job: TVmaze DNS/connect
stalls are NOT covered by requests' read timeout, so a stuck lookup could
otherwise block a run forever. On expiry the job is skipped (marked attempted).

Usage:
    python3 scripts/backfill_sweep.py --count 6 --offset 0 --progress anime_backfill_progress.json
"""
import argparse
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from enrich_airing import (  # noqa: E402
    DATA_FILE,
    load_json,
    save_json,
    _needs_backfill,
    _backfill_one,
)

JOB_TIMEOUT = 15


class _JobTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise _JobTimeout()


def build_jobs(todo_path, attempted=None):
    data = load_json(DATA_FILE)
    todo = load_json(todo_path) or []
    jobs = []
    for row in todo:
        slug = row[0] if isinstance(row, (list, tuple)) else row
        if attempted and slug in attempted:
            continue
        entry = data.get(slug)
        if not entry or entry.get("status") != "Ongoing":
            continue
        aired = entry.get("total_episodes") or 0
        nxt = entry.get("next_episode")
        if nxt:
            aired = nxt - 1
        if aired > 0 and _needs_backfill(entry, aired):
            jobs.append((slug, aired))
    return jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--todo", default="anime_airing_todo.json")
    ap.add_argument("--progress", default="anime_backfill_progress.json")
    args = ap.parse_args()

    attempted = load_json(args.progress) if args.progress else {}
    jobs = build_jobs(args.todo, attempted)
    window = jobs[args.offset:args.offset + args.count]
    if not window:
        print(f"SWEEP: no jobs in window (offset={args.offset}, total={len(jobs)})")
        return

    data = load_json(DATA_FILE)
    titles = thumbs = skipped = 0
    signal.signal(signal.SIGALRM, _alarm_handler)
    for i, (slug, aired) in enumerate(window, 1):
        entry = data.get(slug)
        if not entry:
            continue
        try:
            signal.setitimer(signal.ITIMER_REAL, JOB_TIMEOUT)
            t, th = _backfill_one(entry, aired)
        except _JobTimeout:
            t = th = 0
            skipped += 1
            print(f"  [{args.offset + i}/{len(jobs)}] {slug}: TIMEOUT, skipping", flush=True)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
        titles += t
        thumbs += th
        attempted[slug] = True  # never re-attempt on later runs
        save_json(args.progress, attempted)  # durable progress
        save_json(DATA_FILE, data)  # durable: save after every job
        print(f"  [{args.offset + i}/{len(jobs)}] {slug}: +{t} titles, +{th} thumbs", flush=True)

    print(f"SWEEP done: window={len(window)} jobs, titles={titles}, thumbs={thumbs}, skipped={skipped}")


if __name__ == "__main__":
    main()
