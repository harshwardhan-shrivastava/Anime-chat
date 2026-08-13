#!/usr/bin/env python3
"""Strip CONFIRMED cross-franchise thumbnail contamination.

The generic shared-URL scrub (fix_wrong_thumbs.py) removes every URL that
appears on 2+ cards — but many of those shares are LEGITIMATE (our catalog
splits one franchise into several cards, or lists a show under both its JP
and EN names: 'Mirai Nikki' / 'The Future Diary', 'Quanzhi Gaoshou' / 'The
King's Avatar', ...). Blindly stripping them would delete real thumbnails.

This script only strips URLs shared between slug pairs that are KNOWN
different shows (curated from a manual review of the shared-URL pairs), then
scrubs the same URLs out of every resume cache so --apply cannot re-inject
them, and queues the affected slugs for a guarded re-match.

One-sided entries (ONE_SIDED) strip the shared URLs from ONLY the listed
(wrong) slug, leaving the legitimate franchise owner untouched — used when
one card borrowed another franchise's stills (e.g. 'Misaki Chronicle:
Divergence Eve' carrying Mirai Nikki images).

Usage:
    python3 scripts/strip_cross_thumbs.py
    python3 scripts/match_ep_thumbs.py --match N --offset M \\
        --cache anime_ep_thumbs_fx60.json --todo anime_ep_thumbs_crosstodo.json
    python3 scripts/enrich_ep_thumbnails.py --apply
"""

import glob
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "anime_data.json")

# Unordered slug pairs that are genuinely DIFFERENT shows (manual review of
# every shared-URL pair). Both sides lose the shared stills; both get queued
# for a guarded re-match.
CROSS_PAIRS = {
    frozenset(["link-click", "yu-yu-hakusho"]),
    frozenset(["link-click-season-2", "yu-yu-hakusho"]),
    frozenset(["link-click-bridon-arc", "yu-yu-hakusho"]),
    frozenset(["hit-wo-nerae-specials", "viral-hit"]),
    frozenset(["say-i-love-you", "do-you-love-your-mom-and-her-two-hit-multi-target-attacks-do-you-love-your-mom-on-the-shore"]),
    frozenset(["perfect-world", "forest-of-piano"]),
    frozenset(["r-o-d-read-or-die", "full-dive-this-ultimate-next-gen-full-dive-rpg-is-even-shittier-than-real-life"]),
    frozenset(["pes-peace-eco-smiles", "pet"]),
    frozenset(["the-8th-son-are-you-kidding-me", "sore-ike-anpanman-baikinman-to-ehon-no-lulun"]),
    frozenset(["maze", "urara-meirocho"]),
    frozenset(["mittsu-no-hanashi", "actually-i-am"]),
    frozenset(["mirai-harmony", "future-boy-conan"]),
    frozenset(["ultramarine-magmell", "gunjo-sanka-2022"]),
    frozenset(["your-forma", "i-m-your-treasure-box-you-have-found-captain-marine-in-a-treasure-chest"]),
    frozenset(["spring-and-chaos", "yuuna-and-the-haunted-hot-springs"]),
    frozenset(["an-archdemon-s-dilemma-how-to-love-your-elf-bride", "dou-po-cangqiong-yuanqi"]),
    frozenset(["a-ninja-and-an-assassin-under-one-roof", "ane-log-big-sister-moyako-s-never-ending-monologue"]),
    frozenset(["rusuban", "alya-sometimes-hides-her-feelings-in-russian"]),
    frozenset(["suu-funkan-no-yell-wo", "smoking-behind-the-supermarket-with-you"]),
    # Both boxing/golf anime cards carrying stills from a Turkish TV series
    # ("16. Bölüm") — wrong for both.
    frozenset(["hajime-no-ippo", "rising-impact-season-2"]),
    # ---- second curation pass (verified different shows) ----
    frozenset(["a-girl", "aho-girl"]),
    frozenset(["a-nobody-s-way-up-to-an-exploration-hero", "hajimari-wa-kimi-no-sora"]),
    frozenset(["air-gear", "gear-senshi-dendoh"]),
    frozenset(["akudama-drive", "i-m-kodama-kawashiri"]),
    frozenset(["ano-hi-no-kanojotachi", "waiting-in-the-summer"]),
    frozenset(["arms-alchemy", "frame-arms-girl"]),
    frozenset(["back-arrow", "back-to-you"]),
    frozenset(["baki", "saki"]),
    frozenset(["banana-fish", "katana-maidens-mini-toji"]),
    frozenset(["blue-exorcist", "blue-gale-xabungle"]),
    frozenset(["blue-exorcist-beyond-the-snow-saga", "blue-gale-xabungle"]),
    frozenset(["blue-exorcist-kyoto-saga", "blue-gale-xabungle"]),
    frozenset(["blue-exorcist-the-blue-night-saga", "blue-gale-xabungle"]),
    frozenset(["bokutachi-no-peace-river", "remake-our-life"]),
    frozenset(["children-of-the-whales", "sing-a-bit-of-harmony"]),
    frozenset(["cinderella-nine", "hachimitsu-suicide-machine"]),
    frozenset(["gal-dino", "gals-can-t-be-kind-to-otaku"]),
    frozenset(["girls-beyond-the-wasteland", "i-made-friends-with-the-second-prettiest-girl-in-my-class"]),
    frozenset(["god-eater", "god-mars"]),
    frozenset(["hirano-to-kagiura", "iria-zeiram-the-animation"]),
    frozenset(["itsuka-no-watashi-yori", "tsumasho"]),
    frozenset(["just-because", "just-disappear"]),
    frozenset(["kiss-him-not-me", "wadachi-wo-koete-yuke"]),
    frozenset(["kono-aozora-ni-yakusoku-wo-youkoso-tsugumi-ryou-e", "konosuba-god-s-blessing-on-this-wonderful-world"]),
    frozenset(["last-dance", "last-exile"]),
    frozenset(["lonely-castle-in-the-mirror", "loner-life-in-another-world"]),
    frozenset(["magi-the-kingdom-of-magic", "the-labyrinth-of-grisaia"]),
    frozenset(["manie-manie-neo-tokyo", "mechanical-marie"]),
    frozenset(["mars-of-destruction", "mars-red"]),
    frozenset(["nanatsu-no-taizai-eiyuu-tachi-wa-hashagu", "seven-mortal-sins"]),
    frozenset(["napping-princess", "solo-camping-for-two"]),
    frozenset(["narenare-cheer-for-you", "wareware-wa-uchuujin"]),
    frozenset(["new-fist-of-the-north-star", "new-saga"]),
    frozenset(["new-game", "new-getter-robo"]),
    frozenset(["oval-x-over", "val-x-love"]),
    frozenset(["paradise-of-innocence", "parasite-dolls"]),
    frozenset(["re-main", "refrain-blue"]),
    frozenset(["sakugan", "sakuhin"]),
    frozenset(["the-betrayal-knows-my-name", "the-nameko-families"]),
    frozenset(["the-qwaser-of-stigmata", "the-water-magician"]),
    frozenset(["tonari-no-yokai-san", "tongari-boushi-no-memole"]),
    frozenset(["wind-breaker", "windy-tales"]),
}

# slug -> [other slugs]: strip URLs shared with those others from THIS slug
# only (the wrong borrower), never from the franchise owner.
ONE_SIDED = {
    "misaki-chronicle-divergence-eve": [
        "mirai-nikki", "the-future-diary",
    ],
}


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


def main():
    data = load_json(DATA_FILE)
    if not data:
        print("no data")
        return

    # 1. Collect the URLs shared by each CROSS pair and ONE_SIDED entry.
    slug_urls = {}
    for slug, e in data.items():
        urls = set()
        for s in e.get("seasons") or []:
            for ep in s.get("episodes") or []:
                if ep.get("thumb"):
                    urls.add(ep["thumb"])
        slug_urls[slug] = urls

    bad_urls = set()      # URLs stripped from both sides of a CROSS pair
    one_sided_bad = {}    # slug -> urls stripped from that slug only
    affected = set()
    for a, b in CROSS_PAIRS:
        common = slug_urls.get(a, set()) & slug_urls.get(b, set())
        if common:
            bad_urls |= common
            affected.add(a)
            affected.add(b)
    for slug, others in ONE_SIDED.items():
        urls = set()
        for o in others:
            urls |= slug_urls.get(slug, set()) & slug_urls.get(o, set())
        if urls:
            one_sided_bad[slug] = urls
            affected.add(slug)
    print("cross-franchise URLs to strip:", len(bad_urls),
          "| one-sided URLs:", sum(len(v) for v in one_sided_bad.values()),
          "| slugs:", len(affected), flush=True)

    # 2. Strip them from the catalog.
    stripped = 0
    for slug, e in data.items():
        if not (slug in affected or slug in one_sided_bad):
            continue
        mine = bad_urls | one_sided_bad.get(slug, set())
        changed = False
        for s in e.get("seasons") or []:
            for ep in s.get("episodes") or []:
                if ep.get("thumb") in mine:
                    ep.pop("thumb", None)
                    stripped += 1
                    changed = True
        if changed:
            save_json(DATA_FILE, data)
    save_json(DATA_FILE, data)
    print("stripped episodes:", stripped, flush=True)

    # 3. Scrub the same URLs from every resume cache so --apply can't
    #    re-inject them.
    scrubbed_files = 0
    removed = 0
    for fname in sorted(glob.glob(os.path.join(ROOT, "anime_ep_thumbs*.json"))):
        base = os.path.basename(fname)
        if "todo" in base or "remain" in base or "retry" in base:
            continue
        cache = load_json(fname)
        if not isinstance(cache, dict):
            continue
        changed = False
        for slug, thumbs in cache.items():
            if not isinstance(thumbs, dict):
                continue
            if slug not in affected and slug not in one_sided_bad:
                continue
            mine = bad_urls | one_sided_bad.get(slug, set())
            for k in [k for k, v in thumbs.items() if isinstance(v, str) and v in mine]:
                thumbs.pop(k, None)
                removed += 1
                changed = True
        if changed:
            save_json(fname, cache)
            scrubbed_files += 1
    print("scrubbed cache files:", scrubbed_files, "| cache urls removed:",
          removed, flush=True)

    # 4. Queue affected slugs (that still have named eps missing thumbs) for
    #    a guarded re-match.
    todo = []
    for slug in sorted(affected):
        missing = any(
            (ep.get("title") and not ep.get("thumb"))
            for s in (data.get(slug) or {}).get("seasons") or []
            for ep in s.get("episodes") or []
        )
        if missing:
            todo.append(slug)
    todo_path = os.path.join(ROOT, "anime_ep_thumbs_crosstodo.json")
    save_json(todo_path, todo)
    print("re-match todo:", len(todo), "slugs ->",
          os.path.basename(todo_path), flush=True)


if __name__ == "__main__":
    main()
