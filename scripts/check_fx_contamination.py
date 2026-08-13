#!/usr/bin/env python3
"""Attribute suspicious shared-URL pairs to the fx51-fx88 re-match caches.

A pair is 'introduced by fx' if the shared URL value appears inside any
fx51..fx88 cache entry for one of the two slugs (the fx re-match wrote it).
Otherwise the share comes from older w/k/r/m caches (pre-existing) and is
overwhelmed/fine.
"""
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

data = json.load(open("anime_data.json", "r", encoding="utf-8"))
clean = re.compile(r"[^a-z0-9]+")


def norm(x):
    return clean.sub("", (x or "").lower())


def related(a, b):
    na, nb = norm(a), norm(b)
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = set(na[i:i + 4] for i in range(len(na) - 3)), set(nb[i:i + 4] for i in range(len(nb) - 3))
    return len(ta & tb) >= 2


# shared URL -> slugs
from collections import defaultdict
url_slugs = defaultdict(set)
for slug, e in data.items():
    for s in e.get("seasons") or []:
        for ep in s.get("episodes") or []:
            t = ep.get("thumb")
            if t:
                url_slugs[t].add(slug)

suspicious = {}
for u, sl in url_slugs.items():
    sl = sorted(sl)
    for i in range(len(sl)):
        for j in range(i + 1, len(sl)):
            a, b = sl[i], sl[j]
            if not related(a, b):
                suspicious.setdefault((a, b), u)

# Load fx51+ caches
fx = {}
for f in glob.glob("anime_ep_thumbs_fx5*.json") + glob.glob("anime_ep_thumbs_fx6*.json") + \
        glob.glob("anime_ep_thumbs_fx7*.json") + glob.glob("anime_ep_thumbs_fx8*.json"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    for k, v in d.items():
        if isinstance(v, dict):
            fx.setdefault(k, v)

new_intro = []
pre = []
for (a, b), u in sorted(suspicious.items()):
    fa = fx.get(a, {})
    fb = fx.get(b, {})
    in_a = any(v == u for v in fa.values())
    in_b = any(v == u for v in fb.values())
    if in_a or in_b:
        new_intro.append((a, b, u, "A" if in_a else "", "B" if in_b else ""))
    else:
        pre.append((a, b))

print(f"suspicious pairs total: {len(suspicious)}")
print(f"INTRODUCED BY FX RE-MATCH: {len(new_intro)}")
for a, b, u, fa_, fb_ in new_intro:
    print(f"  {a}[{fa_}] <-> {b}[{fb_}]  {u}")
print(f"pre-existing (not from fx51-88): {len(pre)}")
for a, b in pre[:15]:
    print(f"  {a} <-> {b}")
