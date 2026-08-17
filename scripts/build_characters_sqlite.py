"""Build anime_characters.sqlite from anime_characters_index.json.

The in-memory index (96k entries, ~50MB JSON -> ~325MB as Python objects)
combined with the anime catalog blew past Render's 512MB free-tier RAM.
This script bakes the same data into a read-only SQLite file that
characters_data.py queries at runtime, so the characters page only costs
a few MB of memory.

Run:  python scripts/build_characters_sqlite.py
"""
import json
import os
import sqlite3

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_DIR, "anime_characters_index.json")
DST = os.path.join(_DIR, "anime_characters.sqlite")


def main():
    with open(SRC, "r", encoding="utf-8") as f:
        data = json.load(f)

    if os.path.exists(DST):
        os.remove(DST)

    db = sqlite3.connect(DST)
    db.execute(
        "CREATE TABLE characters ("
        " id TEXT, name TEXT, image TEXT, role TEXT, desc TEXT,"
        " slug TEXT, title TEXT, members TEXT, jp TEXT, en TEXT)"
    )

    rows = [
        (
            str(e.get("id") or ""),
            e.get("name") or "",
            e.get("image") or "",
            e.get("role") or "",
            e.get("desc") or "",
            e.get("slug") or "",
            e.get("title") or "",
            str(e.get("members") or ""),
            json.dumps(e.get("jp") or [], ensure_ascii=False),
            json.dumps(e.get("en") or [], ensure_ascii=False),
        )
        for e in data
    ]
    db.executemany("INSERT INTO characters VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()

    size = os.path.getsize(DST) / 1e6
    print(f"Wrote {DST} with {len(rows)} rows ({size:.1f} MB)")


if __name__ == "__main__":
    main()
