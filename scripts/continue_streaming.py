#!/usr/bin/env python3
"""
Continue the JustWatch per-country (US/JP) streaming enrichment across every
unfinished cache band.

JustWatch rate-limits aggressively (IP ban after ~2k rapid requests). This
script polls the API until the ban lifts, then resumes every
anime_streaming_jw_*.json band from where it left off, respecting the
--budget (seconds) cap per run. Re-run it as often as you like.

Usage:
    python3 scripts/continue_streaming.py --budget 170
    python3 scripts/continue_streaming.py --budget 170 --apply   # also merge
"""

import argparse
import json
import os
import subprocess
import sys
import time

import requests

GQL = "https://apis.justwatch.com/graphql"
HEADERS = {"Content-Type": "application/json",
           "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (offset, count) per cache band, matching the original enrichment layout.
BANDS = [
    (0, 700, "anime_streaming_jw_a.json"),
    (700, 700, "anime_streaming_jw_b.json"),
    (1400, 700, "anime_streaming_jw_c.json"),
    (2100, 700, "anime_streaming_jw_d.json"),
    (2800, 700, "anime_streaming_jw_e.json"),
    (3500, 700, "anime_streaming_jw_f.json"),
    (4200, 700, "anime_streaming_jw_g.json"),
    (4900, 700, "anime_streaming_jw_h.json"),
    (5600, 700, "anime_streaming_jw_i.json"),
    (6300, 700, "anime_streaming_jw_j.json"),
    (7000, 700, "anime_streaming_jw_k.json"),
    (7700, 700, "anime_streaming_jw_l.json"),
    (8400, 700, "anime_streaming_jw_m.json"),
    (9100, 700, "anime_streaming_jw_n.json"),
    (9800, 700, "anime_streaming_jw_o.json"),
    (10500, 700, "anime_streaming_jw_p.json"),
    (11200, 700, "anime_streaming_jw_q.json"),
    (11900, 700, "anime_streaming_jw_r.json"),
    (12600, 700, "anime_streaming_jw_s.json"),
]


def blocked():
    try:
        r = requests.post(
            GQL,
            json={"query": '{ popularTitles(country: US, filter: {searchQuery: "Naruto"}) { edges { node { id } } } }'},
            headers=HEADERS, timeout=15)
        return r.status_code != 200
    except Exception:
        return True


def wait_for_unblock(budget):
    start = time.time()
    last = 0
    while time.time() - start < budget:
        if not blocked():
            print(f"UNBLOCKED after {int(time.time()-start)}s", flush=True)
            return True
        el = int(time.time() - start)
        if el - last >= 30:
            print(f"still blocked... {el}s", flush=True)
            last = el
        time.sleep(10)
    print("still blocked after budget", flush=True)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=170,
                        help="max seconds to spend polling + enriching")
    parser.add_argument("--apply", action="store_true",
                        help="merge all caches into anime_data.json afterwards")
    parser.add_argument("--bands", default="",
                        help="comma-separated cache filenames to process; "
                             "default = all defined bands")
    args = parser.parse_args()

    start = time.time()

    # Which bands need work?
    bands = [b for b in BANDS if b[2] in (args.bands.split(",") if args.bands else [b[2] for b in BANDS])]
    pending = []
    for offset, count, fname in bands:
        path = os.path.join(ROOT, fname)
        done = 0
        if os.path.exists(path):
            try:
                done = len(json.load(open(path)))
            except Exception:
                done = 0
        if done < count:
            pending.append((offset, count, fname, done))
    if not pending:
        print("All bands complete!", flush=True)
    else:
        print(f"{len(pending)} bands pending: " +
              ", ".join(f"{f}({d}/{c})" for _, c, f, d in pending), flush=True)

    # Wait for JustWatch to unblock, then enrich each pending band in turn.
    if pending:
        if not wait_for_unblock(args.budget):
            sys.exit(1)
        for offset, count, fname, done in pending:
            remaining = args.budget - (time.time() - start)
            if remaining < 20:
                print("budget nearly exhausted; continue next run", flush=True)
                break
            cmd = [sys.executable, os.path.join(ROOT, "scripts/enrich_streaming.py"),
                   "--offset", str(offset), "--count", str(count),
                   "--cache", os.path.join(ROOT, fname)]
            print(f"== enriching {fname} (offset {offset}, {count}) for ~{int(remaining)}s", flush=True)
            try:
                subprocess.run(cmd, timeout=max(20, int(remaining) - 5),
                               cwd=ROOT)
            except subprocess.TimeoutExpired:
                print(f"{fname}: time budget hit (resumable, will continue next run)", flush=True)

    if args.apply:
        print("== applying caches to catalog", flush=True)
        subprocess.run([sys.executable, os.path.join(ROOT, "scripts/enrich_streaming.py"),
                        "--apply"], cwd=ROOT)


if __name__ == "__main__":
    main()
