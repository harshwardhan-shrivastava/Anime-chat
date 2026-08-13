#!/usr/bin/env python3
"""Flag only SUSPICIOUS shared thumbnail URLs (cross-franchise contamination).

Legitimate shares: the catalog lists the same show under 2+ cards (JP/EN titles,
season-split cards: 'demon-slayer' / 'demon-slayer-kimetsu-no-yaiba',
'naruto' / 'naruto-shippuden'). Those are fine. We flag pairs where neither
slug contains the other AND the normalized titles are dissimilar."""
import json
import os
import re
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

data = json.load(open("anime_data.json", "r", encoding="utf-8"))

url_slugs = defaultdict(set)
for slug, e in data.items():
    for s in e.get("seasons") or []:
        for ep in s.get("episodes") or []:
            t = ep.get("thumb")
            if t:
                url_slugs[t].add(slug)

clean = re.compile(r"[^a-z0-9]+")


def norm(x):
    return clean.sub("", (x or "").lower())


def related(a, b):
    na, nb = norm(a), norm(b)
    if na == nb or na in nb or nb in na:
        return True
    # shared distinctive token (>= 4 chars) e.g. 'naruto' in both
    ta, tb = set(na[i:i + 4] for i in range(len(na) - 3)), set(nb[i:i + 4] for i in range(len(nb) - 3))
    return len(ta & tb) >= 2

suspicious = {}
for u, sl in url_slugs.items():
    sl = sorted(sl)
    for i in range(len(sl)):
        for j in range(i + 1, len(sl)):
            a, b = sl[i], sl[j]
            if not related(a, b):
                suspicious.setdefault((a, b), u)

print("SUSPICIOUS cross-franchise shared-URL pairs:", len(suspicious))
for (a, b), u in sorted(suspicious.items())[:40]:
    print(f"  {a} <-> {b}")
    print(f"      {u}")
