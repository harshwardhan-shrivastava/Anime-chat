#!/usr/bin/env python3
"""
Enrich anime_data.json with REAL per-country streaming availability and
Sub/Dub language data from JustWatch -- the same "Where to Watch" dataset
Google surfaces in search results. No API key needed.

JustWatch's current GraphQL API (verified Aug 2026):

    popularTitles(country: US, filter: {searchQuery: "<title>"}) {
      edges { node {
        id objectType
        offers(country: US, platform: WEB)  { monetizationType standardWebURL
                                              audioLanguages subtitleLanguages
                                              package { clearName } }
        offersJP: offers(country: JP, platform: WEB) { ... same ... }
      } }
    }

Each entry gets:
  - streaming  -> unique providers with the regions they carry (US / JP) and a
                  Sub / Sub • Dub status derived from real audioLanguages
  - dub        -> audio language names available on the detected services
                  (English/Japanese + any others reported)
  - subtitles  -> subtitle language names reported by the services
  - availability -> {US: [...], JP: [...]} convenience map (unused by UI)

Usage (chunked + resumable):

    # fetch phase: each run writes its OWN cache file so multiple runs can
    # run in parallel; --offset/--count slice the catalog by member_count
    python3 scripts/enrich_streaming.py --offset 0    --count 800 --cache anime_streaming_jw_a.json
    python3 scripts/enrich_streaming.py --offset 800  --count 800 --cache anime_streaming_jw_b.json

    # apply phase: merge all anime_streaming_jw_*.json caches into the catalog
    python3 scripts/enrich_streaming.py --apply

Hand-curated entries and entries JustWatch does not know are left untouched.
"""

import argparse
import json
import os
import re
import sys
import time

import requests

GQL = "https://apis.justwatch.com/graphql"
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
CACHE_GLOB = "anime_streaming_jw_*.json"

# Language code -> display name (JustWatch reports ISO codes).
LANG_NAMES = {
    "en": "English", "ja": "Japanese", "es": "Spanish", "pt": "Portuguese",
    "fr": "French", "de": "German", "it": "Italian", "ko": "Korean",
    "zh": "Chinese", "ru": "Russian", "hi": "Hindi", "ta": "Tamil",
    "te": "Telugu", "th": "Thai", "id": "Indonesian", "vi": "Vietnamese",
    "ms": "Malay", "ar": "Arabic", "tr": "Turkish", "pl": "Polish",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
    "fi": "Finnish", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian",
    "uk": "Ukrainian", "he": "Hebrew", "el": "Greek",
}

MONETIZATION_LABEL = {
    "FLATRATE": "Streaming",
    "ADS": "Free with Ads",
    "FREE": "Free",
    "RENT": "Rent",
    "BUY": "Buy",
}

# Noise providers that are not really streaming services we want to promote.
IGNORE_PROVIDERS = {
    "Amazon DVD / Blu-ray", "GRUV", "Fandango at Home Free", "Hoopla",
    "Pluto TV Live", "The Roku Channel",
}


def load_json(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def gql(query, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.post(GQL, json={"query": query}, headers=HEADERS,
                                 timeout=20)
            if resp.status_code == 429:
                time.sleep(1 + attempt)
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    continue
                raise RuntimeError(data["errors"])
            return data.get("data") or {}
        except requests.RequestException as exc:
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            raise exc
    return {}


# JustWatch's parser rejects multiline queries; keep it on ONE line.
SEARCH_QUERY = ('{ popularTitles(country: US, filter: {searchQuery: %s}) { edges { node { '
                'id objectType '
                'offers(country: US, platform: WEB) { monetizationType standardWebURL audioLanguages subtitleLanguages package { clearName } } '
                'offersJP: offers(country: JP, platform: WEB) { monetizationType standardWebURL audioLanguages subtitleLanguages package { clearName } } '
                '} } } }')


def clean_query(title):
    """Shorten titles JustWatch may not index verbatim (season markers etc.)."""
    t = title.strip()
    # strip trailing " Season N" / " Part N" / " (TV)" / " (OVA)" style markers
    t = re.sub(r"\s*\(?(?:Season|Part|Cour|S)\s*\d+\s*\)?$", "", t, flags=re.I)
    t = re.sub(r"\s*\((?:TV|OVA|ONA|Movie|Special|Film)\)\s*$", "", t, flags=re.I)
    t = re.sub(r"^(?:The|A|An)\s+", "", t)
    return t.strip()


def search_title(title, want_show):
    """Find JustWatch node for a title. Returns dict or None."""
    queries = [title.strip(), clean_query(title)]
    seen = set()
    for q in queries:
        if not q or q.lower() in seen:
            continue
        seen.add(q.lower())
        data = gql(SEARCH_QUERY % json.dumps(q))
        edges = (data.get("popularTitles") or {}).get("edges") or []
        if not edges:
            continue
        # prefer exact object type (SHOW for TV anime, MOVIE for movies)
        for edge in edges:
            node = edge.get("node") or {}
            obj_type = (node.get("objectType") or "").upper()
            if want_show and obj_type == "SHOW":
                return node
            if not want_show and obj_type == "MOVIE":
                return node
        # fall back to the first result regardless of type
        return edges[0].get("node") or None
    return None


def node_to_entry(node):
    """Convert a JustWatch node into streaming/dub/subtitles data."""
    offers = (node.get("offers") or []) + (node.get("offersJP") or [])
    if not offers:
        return None

    providers = {}       # clearName -> {regions, status, url, audio, subs}
    audio_codes, sub_codes = set(), set()

    def region_for(country_field):
        return "US" if country_field == "offers" else "JP"

    for country_field in ("offers", "offersJP"):
        region = region_for(country_field)
        for off in node.get(country_field) or []:
            pkg = (off.get("package") or {}).get("clearName") or "Unknown"
            if pkg in IGNORE_PROVIDERS:
                continue
            monet = off.get("monetizationType") or ""
            if monet not in ("FLATRATE", "ADS", "FREE"):
                # only surface streaming / free / ad-supported, skip rent/buy
                continue
            audio = off.get("audioLanguages") or []
            subs = off.get("subtitleLanguages") or []
            audio_codes.update(audio)
            sub_codes.update(subs)
            p = providers.setdefault(pkg, {
                "name": pkg,
                "status": "Sub",
                "regions": [],
                "monetization": MONETIZATION_LABEL.get(monet, "Streaming"),
                "url": off.get("standardWebURL") or "",
                "audio": [], "subtitles": [],
            })
            if region not in p["regions"]:
                p["regions"].append(region)
            # dub status: English audio available anywhere
            if "en" in audio:
                p["status"] = "Sub • Dub"
            p["audio"] = sorted(set(p["audio"]) | set(audio))
            p["subtitles"] = sorted(set(p["subtitles"]) | set(subs))

    if not providers:
        return None

    def lang_names(codes):
        return [LANG_NAMES.get(c, c.upper()) for c in sorted(codes)]

    streaming = list(providers.values())
    # order: streaming services first, then free/ad-supported
    streaming.sort(key=lambda s: (s["monetization"] not in ("Streaming",), s["name"].lower()))
    for s in streaming:
        s["regions"].sort()

    return {
        "streaming": streaming,
        "dub": lang_names(audio_codes),
        "subtitles": lang_names(sub_codes),
        "availability": {
            "US": sorted({s["name"] for s in streaming if "US" in s["regions"]}),
            "JP": sorted({s["name"] for s in streaming if "JP" in s["regions"]}),
        },
    }


def apply_cache():
    """Merge all anime_streaming_jw_*.json caches into anime_data.json."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import anime_data  # noqa: E402

    data = anime_data.anime_database
    matched = 0
    total = 0
    files = sorted(f for f in os.listdir(".") if f.startswith("anime_streaming_jw_") and f.endswith(".json"))
    for fname in files:
        cache = load_json(fname) or {}
        for slug, info in cache.items():
            total += 1
            entry = data.get(slug)
            if not entry:
                continue
            if info is None:
                continue
            entry["streaming"] = info["streaming"]
            entry["dub"] = info["dub"]
            entry["subtitles"] = info["subtitles"]
            entry["availability"] = info.get("availability", {})
            matched += 1

    with open("anime_data.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))

    print(f"Applied JustWatch data to {matched}/{total} cached titles. "
          f"Total entries: {len(data)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0,
                        help="skip the first N titles (by member count)")
    parser.add_argument("--count", type=int, default=500,
                        help="how many titles to process in this run")
    parser.add_argument("--cache", default="anime_streaming_jw_a.json",
                        help="cache file to write (unique per parallel run)")
    parser.add_argument("--apply", action="store_true",
                        help="merge all caches into anime_data.json and exit")
    args = parser.parse_args()

    if args.apply:
        apply_cache()
        return

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import anime_data  # noqa: E402

    data = anime_data.anime_database
    entries = sorted(data.items(), key=lambda kv: (kv[1].get("member_count") or 0), reverse=True)
    window = entries[args.offset:args.offset + args.count]
    print(f"Catalog: {len(data)} | window: {args.offset}..{args.offset + len(window)} "
          f"({len(window)} titles)", flush=True)

    cache = load_json(args.cache) or {}
    done = 0
    errors_in_row = 0
    for idx, (slug, entry) in enumerate(window, 1):
        if slug in cache:
            done += 1
            continue
        title = entry.get("title") or slug
        want_show = "Movie" not in (entry.get("type") or "")
        try:
            node = search_title(title, want_show)
            errors_in_row = 0
        except Exception as exc:
            errors_in_row += 1
            print(f"  [{idx}] {title}: ERROR {str(exc)[:70]}", flush=True)
            # JustWatch bans the IP after a burst: stop early and save so the
            # next run can poll for the unblock instead of burning its budget
            # on doomed requests.
            if errors_in_row >= 3:
                print("  3+ consecutive errors (rate limited?) - saving and stopping",
                      flush=True)
                save_json(args.cache, cache)
                return
            time.sleep(2)
            continue
        if node is None:
            cache[slug] = None
            print(f"  [{idx}] {title}: not found", flush=True)
        else:
            info = node_to_entry(node)
            cache[slug] = info
            if info:
                n = len(info["streaming"])
                us = sum(1 for s in info["streaming"] if "US" in s["regions"])
                jp = sum(1 for s in info["streaming"] if "JP" in s["regions"])
                print(f"  [{idx}] {title}: {n} providers (US {us} / JP {jp}) "
                      f"| dub: {'+'.join(info['dub'][:3]) or 'none'}", flush=True)
            else:
                print(f"  [{idx}] {title}: no streamable offers", flush=True)
        done += 1
        if done % 5 == 0:
            save_json(args.cache, cache)
        time.sleep(1.2)

    save_json(args.cache, cache)
    found = sum(1 for v in cache.values() if v)
    print(f"DONE: {found}/{len(cache)} titles in {args.cache} have data")


if __name__ == "__main__":
    main()
