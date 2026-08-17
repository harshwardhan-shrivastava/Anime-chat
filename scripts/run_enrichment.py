#!/usr/bin/env python3
"""Run the full episode-enrichment pipeline on a CI runner.

This is the GitHub Actions entry point (see .github/workflows/enrich.yml).
It mirrors the local dev auto-enrichment (app.py -> _full_enrich_worker)
but runs standalone on a runner with plenty of RAM, so the heavy
disk-based steps that would OOM Render's 512MB free instance can run here:

  1. plan_todo     -> rebuild the list of Ongoing/Upcoming anime
  2. fetch_window  -> refresh AniList airing data (status, episodes,
                      next airing, full airing schedule) into the cache
  3. apply_airing  -> apply statuses, released/TBC flags, totals, MAL titles
  4. tvmaze_backfill -> real TVmaze episode titles + HD thumbs for the
                      newest aired episodes
  5. hd_upgrade    -> upgrade remaining thumbs to the HD flavor

The workflow commits the changed data files back to the repo, and Render
auto-deploys the fresh catalog. The site's in-app 10-minute schedule
refresh (app.py) keeps countdowns/released flags live between deploys.
"""

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def main():
    from scripts.enrich_airing import (
        plan_todo,
        fetch_window,
        apply_airing,
        tvmaze_backfill,
        load_json,
    )

    todo_path = os.path.join(ROOT, "anime_airing_todo.json")
    cross_path = os.path.join(ROOT, "anime_ep_thumbs_crosstodo.json")
    cache_path = os.path.join(ROOT, "anime_airing_a0.json")

    t0 = time.time()

    print("[enrich] plan_todo", flush=True)
    plan_todo(todo_path)

    todo = load_json(todo_path) or []
    print(f"[enrich] fetch_window (count={len(todo)})", flush=True)
    fetch_window(len(todo), cache_file=cache_path, todo_path=todo_path)

    print("[enrich] apply_airing", flush=True)
    apply_airing()

    print("[enrich] tvmaze_backfill (all)", flush=True)
    tvmaze_backfill(0, todo_path=todo_path, cross_path=cross_path)

    print("[enrich] hd_upgrade", flush=True)
    from scripts.upgrade_thumbs_to_hd import main as hd_main
    hd_main()

    print(f"[enrich] done in {time.time() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
