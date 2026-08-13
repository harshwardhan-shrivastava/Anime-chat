"""Audit every show's image/banner URL for dead links and fix them.

For each show with a dead URL, try (in order):
  1. AniList API by anilist_id (fresh cover + banner)
  2. TVmaze poster by title search

Usage:
    python3 scripts/audit_show_images.py --limit 300   # check first 300 (sorted)
    python3 scripts/audit_show_images.py --shuffle     # random sample
    python3 scripts/audit_show_images.py --fix         # apply fixes (needs --check first or runs check inline)
"""
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

DATA_FILE = "anime_data.json"
WORKERS = 24
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
ANILIST_URL = "https://graphql.anilist.co"
TVM_API = "https://api.tvmaze.com"


def load():
    with open(DATA_FILE) as f:
        return json.load(f)


def save(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False)


def url_ok(url):
    if not url:
        return False
    if not url.startswith(("http://", "https://")):
        return False
    try:
        with requests.get(url, timeout=6, headers={"User-Agent": UA}, stream=True) as r:
            if r.status_code != 200:
                return False
            for _ in r.iter_content(1024):
                return True  # got at least one chunk -> content is served
            return True
    except Exception:
        return False


def anilist_fetch(aid):
    """Return (cover, banner) fresh from AniList for a given id, or (None, None)."""
    if not aid:
        return None, None
    q = """
    query ($id: Int) {
      Media(id: $id, type: ANIME) {
        coverImage { extraLarge large medium }
        bannerImage
      }
    }
    """
    try:
        r = requests.post(
            ANILIST_URL,
            json={"query": q, "variables": {"id": aid}},
            headers={"User-Agent": UA, "Content-Type": "application/json"},
            timeout=10,
        )
        if r.status_code == 200:
            m = (r.json() or {}).get("data", {}).get("Media", {})
            cov = (m.get("coverImage") or {}).get("extraLarge") or (m.get("coverImage") or {}).get("large")
            return cov, m.get("bannerImage")
    except Exception:
        pass
    return None, None


def tvmaze_fetch(title):
    """Return a poster URL from TVmaze by title search, or None."""
    if not title:
        return None
    try:
        r = requests.get("%s/singlesearch/shows?q=%s" % (TVM_API, title), timeout=8, headers={"User-Agent": UA})
        if r.status_code == 200:
            img = (r.json() or {}).get("image") or {}
            return img.get("original") or img.get("medium")
    except Exception:
        pass
    return None


def main():
    args = sys.argv[1:]
    limit = 0
    shuffle = "--shuffle" in args
    do_fix = "--fix" in args
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])

    data = load()

    # Check both image and banner.
    targets = []
    for slug, s in data.items():
        img = s.get("image") or ""
        ban = s.get("banner") or ""
        if img or ban:
            targets.append((slug, img, ban, s.get("anilist_id"), s.get("title") or slug))

    if shuffle:
        import random
        random.shuffle(targets)
    else:
        # Big/known shows first (cheap sanity): sort by title length asc? Just keep order.
        pass
    if limit:
        targets = targets[:limit]

    print(f"auditing {len(targets)} shows (fix={do_fix})", flush=True)

    dead = []

    def check(slug, img, ban, aid, title):
        img_bad = not url_ok(img) if img else False
        ban_bad = not url_ok(ban) if ban else False
        return slug, img, ban, aid, title, img_bad, ban_bad

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(check, *t) for t in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            slug, img, ban, aid, title, img_bad, ban_bad = fut.result()
            if img_bad or ban_bad:
                dead.append((slug, title, img, ban, aid, img_bad, ban_bad))
                print(f"  DEAD {slug}: img_bad={img_bad} ban_bad={ban_bad}", flush=True)
            if i % 100 == 0:
                print(f"  checked {i}/{len(targets)} dead_so_far={len(dead)}", flush=True)

    print(f"DEAD URLS: {len(dead)}", flush=True)

    fixed = 0
    if do_fix:
        for slug, title, img, ban, aid, img_bad, ban_bad in dead:
            entry = data.get(slug)
            if not entry:
                continue
            new_img, new_ban = anilist_fetch(aid)
            if img_bad:
                if new_img and new_img != img:
                    entry["image"] = new_img
                elif new_img:
                    entry["image"] = new_img
                else:
                    tv = tvmaze_fetch(title)
                    if tv:
                        entry["image"] = tv
            if ban_bad and new_ban and new_ban != ban:
                entry["banner"] = new_ban
            fixed += 1
        save(data)
        print(f"FIXED {fixed} shows", flush=True)


if __name__ == "__main__":
    main()
