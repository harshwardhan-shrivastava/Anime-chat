#!/usr/bin/env python3
"""Print every suspicious shared-URL pair with catalog titles, for curation."""
import json
import os
import re
from collections import defaultdict

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

print(f"TOTAL suspicious pairs: {len(suspicious)}\n")
for (a, b), u in sorted(suspicious.items()):
    ta = (data.get(a) or {}).get("title") or "?"
    tb = (data.get(b) or {}).get("title") or "?"
    print(f"PAIR: {a} | {b}")
    print(f"  T1: {ta}")
    print(f"  T2: {tb}")
    print(f"  URL: {u}")
    print()
