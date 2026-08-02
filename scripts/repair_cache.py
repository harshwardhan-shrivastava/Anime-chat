#!/usr/bin/env python3
"""Repair a JSON cache file that was truncated mid-write (e.g. a sandbox
kill hit the process between open() and close()).

Finds the longest prefix that still parses as a complete JSON object (by
appending the closing brace), then rewrites the file with that object.
Anything lost is just the single partially-written page.
"""
import json
import sys


def repair(path):
    with open(path, encoding="utf-8") as f:
        s = f.read()
    n = len(s)

    def valid(cut):
        try:
            json.loads(s[:cut] + "}")
            return True
        except Exception:
            return False

    if valid(n):
        obj = json.loads(s)
        print(f"{path}: already valid ({len(s)} chars)")
        return

    # Binary search for the largest valid cut (validity is monotonic:
    # everything before the truncation point parses fine).
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if valid(mid):
            lo = mid
        else:
            hi = mid - 1

    obj = json.loads(s[:lo] + "}")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    print(f"{path}: repaired to {lo} chars ({len(obj)} top-level keys); "
          f"dropped {n - lo} chars")


if __name__ == "__main__":
    repair(sys.argv[1] if len(sys.argv) > 1 else "anime_catalog_raw.json")
