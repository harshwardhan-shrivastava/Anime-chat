#!/usr/bin/env python3
"""Post-apply verification: coverage, HD quality, contamination, priority shows."""
import json
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

data = json.load(open("anime_data.json", "r", encoding="utf-8"))
print("catalog entries:", len(data))

# --- Global stats ---
total_eps = 0
total_thumbs = 0
total_hd = 0
total_nonhd = 0
total_tbc = 0
slugs_with_thumbs = 0
for slug, e in data.items():
    has = False
    for s in e.get("seasons") or []:
        for ep in s.get("episodes") or []:
            total_eps += 1
            t = ep.get("thumb")
            if t:
                total_thumbs += 1
                has = True
                if "original_untouched" in t or "kitsu" in t:
                    total_hd += 1
                else:
                    total_nonhd += 1
            if ep.get("released") is False:
                total_tbc += 1
    if has:
        slugs_with_thumbs += 1
print(f"episodes: {total_eps} | with thumbs: {total_thumbs} "
      f"({100*total_thumbs/max(total_eps,1):.1f}%) | TBC: {total_tbc}")
print(f"HD thumbs: {total_hd} | non-HD: {total_nonhd}")
print(f"slugs with >=1 thumb: {slugs_with_thumbs}")

# --- Contamination check: URL shared by 2+ slugs ---
url_slugs = defaultdict(set)
for slug, e in data.items():
    for s in e.get("seasons") or []:
        for ep in s.get("episodes") or []:
            t = ep.get("thumb")
            if t:
                url_slugs[t].add(slug)
shared = {u: sl for u, sl in url_slugs.items() if len(sl) > 1}
print(f"SHARED URLs across 2+ slugs: {len(shared)}")
for u, sl in list(shared.items())[:5]:
    print("   shared:", sorted(sl))

# --- Priority shows ---
print("\n=== PRIORITY SHOWS ===")
for key in ["you-and-i-are-polar-opposites",
            "you-and-i-are-polar-opposites-season-2",
            "the-100-girlfriends-who-really-really-really-really-really-love-you-season-3",
            "that-time-i-got-reincarnated-as-a-slime-season-3",
            "that-time-i-got-reincarnated-as-a-slime-season-4"]:
    e = data.get(key)
    if not e:
        print(f"{key}: MISSING")
        continue
    rows = []
    for si, s in enumerate(e.get("seasons") or [], 1):
        for ep in s.get("episodes") or []:
            t = ep.get("thumb") or ""
            hd = "HD" if ("original_untouched" in t or "kitsu" in t) else ("th" if t else "--")
            rows.append(f"  S{si}Ep{ep.get('number')}:{hd} {ep.get('title') or ''}")
    print(f"\n{key} (status={e.get('status')}, {len(rows)} eps)")
    print("\n".join(rows[:20]))

# --- Big shows the user will notice ---
print("\n=== BIG SHOWS COVERAGE ===")
for key in ["attack-on-titan-season-2", "attack-on-titan-season-3",
            "attack-on-titan-season-3-part-2", "bakemonogatari", "wotakoi-love-is-hard-for-otaku",
            "welcome-to-demon-school-iruma-kun-season-2", "world-trigger-2nd-season",
            "ace-of-diamond-second-season", "arifureta-from-commonplace-to-world-s-strongest-season-2",
            "star-detective-precure", "black-butler-emerald-witch-arc", "baki-hanma"]:
    e = data.get(key)
    if not e:
        print(f"{key}: MISSING")
        continue
    with_thumbs = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [] if ep.get("thumb"))
    named = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [] if ep.get("title"))
    total = sum(1 for s in e.get("seasons") or [] for ep in s.get("episodes") or [])
    print(f"{key}: {with_thumbs}/{total} thumbs, {named} named")
