"""
Ota-chan — Otakul's friendly mascot AI assistant.

A single blueprint that owns:
  • GET  /otachan        — chat page
  • GET  /otachan/status — conversation history for page load
  • POST /otachan/message — send a message, get an AI reply

Ota-chan is NOT a character roleplay — it's a fixed assistant that helps
with site questions and general anime knowledge.

The API key is read from the GROQ_API_KEY (or XAI_API_KEY fallback) env var.

To enable, add two lines to app.py (after init_threads):

    from ota_chan import init_ota_chan
    init_ota_chan(app)
"""

import ast
import json
import os
import random
import re
import requests
from flask import (
    Blueprint,
    g,
    jsonify,
    render_template,
    request,
    url_for,
)

import database as site_db
from anime_data import anime_database

bp = Blueprint("otachan", __name__)

# LLM API config — uses Groq (OpenAI-compatible, fast, free tier).
LLM_API_KEY = os.environ.get("GROQ_API_KEY") or os.environ.get("XAI_API_KEY", "")
LLM_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
LLM_MODEL = os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_TIMEOUT = 45

MAX_HISTORY = 20  # cap conversation_history at last ~20 messages

# Ota-chan's fixed greeting
GREETING = "Hii, I'm Ota-chan! Ask me anything about Otakul or anime — I've got you."

# Site features reference — written from the actual codebase
SITE_FEATURES_REFERENCE = """Otakul features:
• Browse (New / Upcoming / Popular / Trending / Underrated): The main catalog pages let you sort the full 14k+ anime library by release date (New), upcoming air dates, popularity score, trending activity, or underrated hidden gems. Each card shows the anime poster, rating, and genre.
• Categories / Genres: The genre dropdown in the navbar links to genre-filtered pages (Action, Comedy, Romance, etc.). The "Airing Now" shortcut shows currently airing shows.
• Characters: A searchable database of 10k+ anime characters with images, roles, and anime affiliations. You can search by character name or browse the full list.
• Reviews: Users write 2-10 star anime reviews with text comments. Reviews earn XP — likes from higher-ranked users give more points. Dislikes require C rank (500 XP). Each review shows its author's rank tier (D/C/B/A/S/S+).
• Reply War: Under each review, users can enter a "Reply War" debate — pick a Positive or Negative stance and argue your case. The community votes on the best take, and the winner earns XP while the loser's review takes a penalty.
• Threads (Guilds): Community guilds where users can chat, create channels, share media, and participate in guild wars. Guilds have roles (owner/moderator/member) and compete for Guild XP.
• War Zone: Standalone debate battles where users post a "declaration" (a position), battlers enter their best takes, and the community votes by like-ratio. Guild wars pit two guilds against each other. Wars settle after 24-72 hours.
• XP & Ranking System: Every user earns XP through reviews, likes received, war victories, and community participation. Ranks progress: F → D → C → B → A → S → S+. Higher ranks unlock features (dislike voting at C, reply wars at C, war creation at C). XP is displayed on profiles and next to usernames.
• New to Anime: A quiz-style page that helps newcomers discover anime by answering fun preference questions, then recommends titles based on their taste.
• Your Profile: Shows your avatar, username, rank badge, XP total, review history, anime lists (watching/completed/planned/dropped), and public activity. Users can toggle their profile between public and private.
• Anime Lists: Users can track anime across four lists — Watching, Completed, Plan to Watch, and Dropped — with episode progress tracking.
• Chat (Community Pages): Each anime has its own community chat room where logged-in users can discuss in real time, with GIF support via Giphy.
• Language Toggle: The site supports English and Japanese (日本語) — toggle between them from the user menu. UI strings are translated; user content is not.
• OTP Signup: New accounts require email verification via a one-time code sent to the user's email.
• Favorites: Users can favorite anime, which shows on their profile and affects the site's popularity rankings.
"""

# -------------------------------------------------------------------
# Template replies (fallback when no LLM API key is set)
# -------------------------------------------------------------------
# Anime knowledge base for template replies
_ANIME_DATA = {
    # slug: {name, genre, studio, eps, year, rating, synopsis_short}
    "one-piece": {"name": "One Piece", "genre": "Action, Adventure, Fantasy", "studio": "Toei Animation", "eps": "1100+", "year": "1999", "rating": "8.7", "synopsis": "Monkey D. Luffy sets sail with his pirate crew, the Straw Hat Pirates, searching for the legendary treasure One Piece to become King of the Pirates."},
    "naruto-shippuden": {"name": "Naruto Shippūden", "genre": "Action, Adventure, Fantasy", "studio": "Pierrot", "eps": "500", "year": "2007", "rating": "8.3", "synopsis": "Naruto Uzumaki returns from training to protect his village from Akatsuki while pursuing his dream of becoming Hokage."},
    "dragon-ball-z": {"name": "Dragon Ball Z", "genre": "Action, Adventure, Fantasy", "studio": "Toei Animation", "eps": "291", "year": "1989", "rating": "8.8", "synopsis": "Goku and friends defend Earth from increasingly powerful threats including Saiyans, Namekians, and Androids."},
    "bleach": {"name": "Bleach", "genre": "Action, Adventure, Supernatural", "studio": "Pierrot", "eps": "366", "year": "2004", "rating": "8.1", "synopsis": "Ichigo Kurosaki gains Soul Reaper powers and protects humans from evil spirits while uncovering the truth behind his abilities."},
    "attack-on-titan": {"name": "Attack on Titan", "genre": "Action, Dark Fantasy, Post-Apocalyptic", "studio": "MAPPA", "eps": "87", "year": "2013", "rating": "9.0", "synopsis": "Humanity fights for survival against giant humanoid Titans behind massive walls. Eren Yeager vows to exterminate every Titan."},
    "demon-slayer": {"name": "Demon Slayer", "genre": "Action, Fantasy, Historical", "studio": "ufotable", "eps": "55+", "year": "2019", "rating": "8.5", "synopsis": "Tanjiro Kamado becomes a demon slayer to avenge his family and cure his sister Nezuko, who was turned into a demon."},
    "jujutsu-kaisen": {"name": "Jujutsu Kaisen", "genre": "Action, Supernatural", "studio": "MAPPA", "eps": "48+", "year": "2020", "rating": "8.6", "synopsis": "Yuji Itadori swallows a cursed finger and enrolls in a school of jujutsu sorcerers to fight curses."},
    "fullmetal-alchemist-brotherhood": {"name": "Fullmetal Alchemist: Brotherhood", "genre": "Action, Adventure, Fantasy", "studio": "Bones", "eps": "64", "year": "2009", "rating": "9.1", "synopsis": "Two brothers search for the Philosopher's Stone after a failed attempt to resurrect their mother costs them their bodies."},
    "death-note": {"name": "Death Note", "genre": "Mystery, Psychological, Thriller", "studio": "Madhouse", "eps": "37", "year": "2006", "rating": "8.6", "synopsis": "Light Yagami finds a supernatural notebook that lets him kill anyone whose name he writes in it, leading to a cat-and-mouse game with detective L."},
    "my-hero-academia": {"name": "My Hero Academia", "genre": "Action, Comedy, Superhero", "studio": "Bones", "eps": "138+", "year": "2016", "rating": "8.0", "synopsis": "In a world where most people have superpowers, Izuku Midoriya inherits the quirk 'One For All' and enrolls in a hero academy."},
    "hunter-x-hunter": {"name": "Hunter x Hunter", "genre": "Action, Adventure, Fantasy", "studio": "Madhouse", "eps": "148", "year": "2011", "rating": "9.1", "synopsis": "Gon Freecss searches for his father, a world-famous Hunter, by taking the Hunter Exam and exploring dangerous adventures."},
    "sword-art-online": {"name": "Sword Art Online", "genre": "Action, Adventure, Fantasy", "studio": "A-1 Pictures", "eps": "96+", "year": "2012", "rating": "7.2", "synopsis": "Players trapped in a virtual reality MMORPG must clear the game to escape. Kirito fights to survive and free everyone."},
    "spirited-away": {"name": "Spirited Away", "genre": "Adventure, Fantasy, Supernatural", "studio": "Studio Ghibli", "eps": "Movie (1 film)", "year": "2001", "rating": "8.6", "synopsis": "Chihiro becomes trapped in a spirit world and must work in a bathhouse to save her parents and find her way home."},
    "your-name": {"name": "Your Name (Kimi no Na wa)", "genre": "Romance, Supernatural, Drama", "studio": "CoMix Wave Films", "eps": "Movie (1 film)", "year": "2016", "rating": "8.4", "synopsis": "Two teenagers share a mysterious body-swapping connection and race to meet before a catastrophic event."},
    "one-punch-man": {"name": "One Punch Man", "genre": "Action, Comedy, Superhero", "studio": "Madhouse/MAPPA", "eps": "24", "year": "2015", "rating": "8.5", "synopsis": "Saitama trained so hard he can defeat any enemy with one punch. He searches for a worthy opponent while dealing with boredom."},
    "sword-art-online-alternative-gun-gale-online": {"name": "SAO Alternative: Gun Gale Online", "genre": "Action, Sci-Fi", "studio": "Studio 3Hz", "eps": "12", "year": "2018", "rating": "7.0", "synopsis": "Llenn joins a battle royale VR game and becomes a deadly pink-clad sniper."},
    "clannad": {"name": "Clannad", "genre": "Drama, Romance, Supernatural", "studio": "Kyoto Animation", "eps": "48", "year": "2007", "rating": "8.3", "synopsis": "Tomoya Okazaki finds meaning in life through his relationships with Nagisa and her friends at a small high school."},
    "steins-gate": {"name": "Steins;Gate", "genre": "Drama, Sci-Fi, Thriller", "studio": "White Fox", "eps": "24", "year": "2011", "rating": "9.1", "synopsis": "A self-proclaimed mad scientist discovers time travel and faces devastating consequences when he tries to change the past."},
    "code-geass": {"name": "Code Geass", "genre": "Action, Mecha, Military, Supernatural", "studio": "Sunrise", "eps": "50", "year": "2006", "rating": "8.7", "synopsis": "Exiled prince Lelouch gains the power of absolute obedience and uses it to lead a rebellion against the Holy Britannian Empire."},
    "mob-psycho-100": {"name": "Mob Psycho 100", "genre": "Action, Comedy, Supernatural", "studio": "Bones", "eps": "37", "year": "2016", "rating": "8.6", "synopsis": "Shigeo Kageyama, aka Mob, tries to live a normal life despite having immense psychic powers."},
    "vinland-saga": {"name": "Vinland Saga", "genre": "Action, Adventure, Historical", "studio": "MAPPA", "eps": "48+", "year": "2019", "rating": "8.8", "synopsis": "Young Thorfinn pursues the warrior who killed his father and discovers what it truly means to be strong."},
    "frieren": {"name": "Frieren: Beyond Journey's End", "genre": "Adventure, Drama, Fantasy", "studio": "Madhouse", "eps": "28", "year": "2023", "rating": "8.9", "synopsis": "An elf mage reflects on her journey with her now-deceased companions and sets out on a new quest to understand humans."},
    "solo-leveling": {"name": "Solo Leveling", "genre": "Action, Adventure, Fantasy", "studio": "A-1 Pictures", "eps": "24+", "year": "2024", "rating": "8.3", "synopsis": "The weakest hunter Sung Jinwoo gains a mysterious system that lets him level up without limits."},
    "re-zero": {"name": "Re:Zero", "genre": "Drama, Fantasy, Suspense", "studio": "White Fox", "eps": "50+", "year": "2016", "rating": "8.3", "synopsis": "Subaru Natsuki is transported to another world with the ability to return from death, using it to save those he cares about."},
    "that-time-i-got-reincarnated-as-a-slime": {"name": "That Time I Got Reincarnated as a Slime", "genre": "Action, Comedy, Fantasy", "studio": "Eight Bit", "eps": "48+", "year": "2018", "rating": "8.1", "synopsis": "Satoru Mikami is reincarnated as a slime in a fantasy world and builds a nation of monsters."},
    "mushoku-tensei-jobless-reincarnation": {"name": "Mushoku Tensei", "genre": "Adventure, Drama, Fantasy", "studio": "Studio Bind", "eps": "36+", "year": "2021", "rating": "8.5", "synopsis": "A jobless man is reincarnated in a fantasy world and tries to live his new life without regrets."},
    "kaguya-sama-love-is-war": {"name": "Kaguya-sama: Love Is War", "genre": "Comedy, Romance", "studio": "A-1 Pictures", "eps": "37", "year": "2019", "rating": "8.5", "synopsis": "Two prideful geniuses at a prestigious school are in love but refuse to confess first, setting up elaborate schemes."},
    "bocchi-the-rock": {"name": "Bocchi the Rock!", "genre": "Comedy, Music", "studio": "CloverWorks", "eps": "12", "year": "2022", "rating": "8.6", "synopsis": "Hitori Gotoh, a socially anxious guitarist, joins a band and slowly comes out of her shell through music."},
    "apothecary-diaries": {"name": "The Apothecary Diaries", "genre": "Drama, Mystery", "studio": "OLM, TOHO", "eps": "24", "year": "2024", "rating": "8.7", "synopsis": "Maomao, a pharmacy worker, uses her knowledge of medicine to solve mysteries in the imperial palace."},
    "dr-stone": {"name": "Dr. Stone", "genre": "Adventure, Comedy, Sci-Fi", "studio": "TMS Entertainment", "eps": "36+", "year": "2019", "rating": "8.3", "synopsis": "After all of humanity is petrified, genius Senku Ishigami uses science to rebuild civilization from scratch."},
    "spy-family": {"name": "SPY×FAMILY", "genre": "Action, Comedy", "studio": "Wit Studio, CloverWorks", "eps": "37", "year": "2022", "rating": "8.5", "synopsis": "A spy, an assassin, and a telepathic girl form a fake family, each hiding their true identities."},
    "chainsaw-man": {"name": "Chainsaw Man", "genre": "Action, Horror, Supernatural", "studio": "MAPPA", "eps": "12", "year": "2022", "rating": "8.4", "synopsis": "Denji merges with his chainsaw devil and becomes a Devil Hunter for a questionable organization."},
    "tokyo-ghoul": {"name": "Tokyo Ghoul", "genre": "Action, Horror, Supernatural", "studio": "Pierrot", "eps": "48", "year": "2014", "rating": "7.7", "synopsis": "Ken Kaneki becomes a half-ghoul after a transplant and must navigate between the human and ghoul worlds."},
    "fairytale": {"name": "Fairy Tail", "genre": "Action, Adventure, Comedy, Fantasy", "studio": "A-1 Pictures, Satelight", "eps": "328", "year": "2009", "rating": "7.9", "synopsis": "Natsu Dragneel and his friends in the Fairy Tail guild go on magical adventures and fight powerful enemies."},
    "black-clover": {"name": "Black Clover", "genre": "Action, Comedy, Fantasy", "studio": "Pierrot", "eps": "170", "year": "2017", "rating": "8.1", "synopsis": "Asta, born without magic in a magic world, strives to become the Wizard King through sheer determination."},
    "seven-deadly-sins": {"name": "The Seven Deadly Sins", "genre": "Action, Adventure, Fantasy", "studio": "A-1 Pictures, Deen", "eps": "96", "year": "2014", "rating": "8.0", "synopsis": "Knights of the Kingdom of Britannia set out to find the legendary Seven Deadly Sins and save the realm."},
    "no-game-no-life": {"name": "No Game No Life", "genre": "Adventure, Comedy, Fantasy, Ecchi", "studio": "Madhouse", "eps": "12", "year": "2014", "rating": "8.3", "synopsis": "Gaming genius siblings Sora and Shiro are transported to a world where everything is decided by games."},
    "the-promised-neverland": {"name": "The Promised Neverland", "genre": "Horror, Mystery, Sci-Fi", "studio": "CloverWorks", "eps": "23", "year": "2019", "rating": "8.5", "synopsis": "Children in an idyllic orphanage discover a dark secret and plot their escape."},
    "mob-psycho": {"name": "Mob Psycho 100", "genre": "Action, Comedy, Supernatural", "studio": "Bones", "eps": "37", "year": "2016", "rating": "8.6", "synopsis": "Shigeo Kageyama, aka Mob, tries to live a normal life despite having immense psychic powers."},
}

# Character power-level knowledge for 'who would win' debates
_BATTLE_DATA = {
    "goku": "Goku (Dragon Ball Z) — Can destroy universes with Ultra Instinct. Massively faster than light, master of ki attacks, has SSJ Blue, Ultra Instinct, and God forms.",
    "ichigo": "Ichigo (Bleach) — Fullbringer, Shinigami, Hollow, and Quincy hybrid. Bankai grants massive speed and power, with Final Getsuga Tensho capable of destroying mountains.",
    "naruto": "Naruto (Naruto Shippūden) — Jinchūriki of the Nine-Tails, Sage Mode, Six Paths powers. Massive chakra reserves and can create thousands of shadow clones.",
    "sasuke": "Sasuke (Naruto Shippūden) — Rinnegan and Eternal Mangekyo Sharingan. Space-time manipulation, Amaterasu, and Susano'o.",
    "luffy": "Luffy (One Piece) — Gomu Gomu no Mi (Rubber), advanced Conqueror's Haki, Gear 5 (Nika form) — can turn reality into cartoon physics.",
    "zoro": "Zoro (One Piece) — Three-sword style master, immense physical strength, and Conqueror's Haki.",
    "saitama": "Saitama (One Punch Man) — Can defeat ANYONE with a single punch. Immense strength, speed, and durability with seemingly no upper limit.",
    "gojo": "Gojo (Jujutsu Kaisen) — Infinity barrier makes him nearly untouchable, Six Eyes + Hollow Purple are devastating offensive techniques.",
    "itadori": "Itadori (Jujutsu Kaisen) — Black Flash punches, immense physical strength, hosts Sukuna who is the King of Curses.",
    "tanjiro": "Tanjiro (Demon Slayer) — Sun Breathing style, Water Breathing mastery, enhanced senses, and powerful Hinokami Kagura.",
    "lelouch": "Lelouch (Code Geass) — Geass power of absolute obedience, tactical genius, command of the Knightmare Frame mechs.",
    "levi": "Levi (Attack on Titan) — Strongest human soldier, fastest ODM gear user, defeated multiple Titan Shifters.",
    "eren": "Eren (Attack on Titan) — Founding Titan + Attack Titan + War Hammer. Can control all Titans and see through time.",
    "gon": "Gon (Hunter x Hunter) — Nen user (Enhancement), incredible potential shown in his Adult Form which rivals Royal Guards.",
    "killua": "Killua (Hunter x Hunter) — Godspeed/Whirlwind mode makes him nearly untouchable, assassination training since childhood.",
    "alucard": "Alucard (Hellsing) — Near-immortal vampire with unlimited regeneration and hundreds of souls.",
    "guts": "Guts (Berserk) — Peak human strength, Dragonslayer sword, Berserker Armor, fights gods and demons.",
    "homura": "Homura (Madoka Magica) — Time manipulation via shield, experienced through countless timelines.",
    "tanya": "Tanya (Saga of Tanya the Evil) — Mage soldier with ruthless tactics and Type-95 Portable God.",
    "satellight": "This character doesn't have enough combat data for me to make a call!",
}

# Generic anime recommendations by genre/mood
_RECOMMENDATIONS = {
    "isekai": [
        "Re:Zero — A masterpiece of the genre with intense psychological drama and time-loop mechanics.",
        "Mushoku Tensei — Beautifully animated, one of the best isekai worldbuilding with deep character growth.",
        "That Time I Got Reincarnated as a Slime — Fun and satisfying power-fantasy with smart politics.",
        "No Game No Life — Hilarious and visually stunning, genius siblings conquer a world through games.",
        "Overlord — Dark lord power fantasy with excellent strategy and world-building.",
    ],
    "action": [
        "Attack on Titan — Intense action with one of the best plot twists in anime history.",
        "Jujutsu Kaisen — Modern shonen with amazing fight choreography and dark themes.",
        "Demon Slayer — Breathtaking animation by ufotable with emotional storytelling.",
        "One Punch Man — Hilarious satire of shonen with incredible action sequences.",
        "Vinland Saga — Epic Viking adventure with deep character development.",
    ],
    "romance": [
        "Kaguya-sama: Love Is War — The funniest romance anime with clever psychological battles.",
        "Your Name — A visually gorgeous film about fate and connection.",
        "Clannad: After Story — Emotionally devastating and beautiful. Prepare tissues.",
        "Toradora! — Classic tsundere romance with genuine heart.",
        "Horimiya — Sweet and realistic high school romance.",
    ],
    "comedy": [
        "Bocchi the Rock! — Relatable comedy about social anxiety and rock music.",
        "Spy x Family — Heartwarming and hilarious family comedy.",
        "Mob Psycho 100 — Hilarious satire with incredible action and heart.",
        "Konosuba — Parody of isekai that's genuinely funny every episode.",
        "Daily Lives of High School Boys — Peak slice-of-life comedy.",
    ],
    "thriller": [
        "Death Note — The ultimate cat-and-mouse thriller.",
        "Steins;Gate — Time travel thriller with devastating emotional payoff.",
        "Code Geass — Political thriller with mind games and mecha.",
        "The Promised Neverland — Season 1 is a masterpiece of suspense.",
        "Monster — A deep, slow-burn psychological thriller.",
    ],
    "best": [
        "Fullmetal Alchemist: Brotherhood — Often considered the best anime ever made. Perfect pacing, world-building, and characters.",
        "Steins;Gate — Time travel done perfectly with incredible character development.",
        "Attack on Titan — A genre-defining epic with plot twists that shook the world.",
        "Hunter x Hunter — Subverts every shonen trope while being incredible.",
        "Frieren: Beyond Journey's End — A beautiful, contemplative masterpiece about time and memory.",
    ],
    "default": [
        "Here are some universally loved anime: Fullmetal Alchemist: Brotherhood, Steins;Gate, Attack on Titan, Hunter x Hunter, and Frieren.",
        "Can you tell me what genre you're into? Action, romance, comedy, thriller? I can give better picks that way!",
        "Try: Death Note for thriller fans, Kaguya-sama for romance lovers, Mob Psycho 100 for comedy, or Vinland Saga for epic adventure.",
    ],
}


def _norm(text):
    """Lowercase, squeeze whitespace, strip punctuation noise."""
    text = (text or "").lower()
    text = re.sub(r"[^a-z0-9\s'\-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _lev(a, b):
    """Edit distance — small helper for spelling-tolerant matching."""
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _words(text):
    return [w for w in _norm(text).replace("-", " ").split() if len(w) > 1]


# Genre spellings the bot tolerates (common typos included).
_GENRE_TOKENS = {
    "isekai": ["isekai", "iskekai", "iskeai", "isekay", "isckai", "isokai", "isekai'd"],
    "action": ["action", "actoin", "akshun"],
    "romance": ["romance", "romantic", "romcom", "romance"],
    "comedy": ["comedy", "comedies", "funny", "comedic"],
    "thriller": ["thriller", "thriler", "suspense", "psychological"],
    "horror": ["horror", "scary", "creepy"],
    "fantasy": ["fantasy", "fantacy", "magic"],
    "scifi": ["sci fi", "scifi", "science fiction", "mecha", "cyberpunk"],
    "drama": ["drama", "tearjerker"],
    "mystery": ["mystery", "detective"],
    "sports": ["sports", "sport"],
    "sliceoflife": ["slice of life", "sliceoflife", "sol"],
    "adventure": ["adventure", "adventures"],
    "music": ["music", "band", "idol"],
    "supernatural": ["supernatural", "shonen", "shounen", "battle shonen"],
}
_GENRE_ALIAS = {}
for canon, toks in _GENRE_TOKENS.items():
    for t in toks:
        _GENRE_ALIAS[t] = canon
_CANONICAL = sorted(set(_GENRE_ALIAS.values()))

# Catalog genre strings per canonical token. The catalog tags genres with
# "\u2022" separators (e.g. "Action \u2022 Sci-Fi \u2022 Drama") \u2014 map the few names that
# don't match their catalog spelling 1:1.
_GENRE_CATALOG = {
    "scifi": ["sci-fi", "sci fi", "mecha"],
    "sliceoflife": ["slice of life"],
}

_GENRE_DISPLAY = {
    "isekai": "Isekai",
    "scifi": "Sci-Fi",
    "sliceoflife": "Slice of Life",
}


# ---- Franchise dedupe + catalog pools ---------------------------------
_SEASON_NOISE = re.compile(
    r"\b(?:season|cour|part|series|arc|s(?:eason)?)\s*[0-9ivx]+\b|"
    r"\b(?:movie|film|ova|ona|special|recap|2nd|3rd)\b|"
    r"\b(?:ii|iii|iv|v|vi|vii|viii)\b",
    re.IGNORECASE,
)


def _base_title(title):
    """Reduce 'Mushoku Tensei -\u2026 Season 2' to franchise base 'mushoku tensei'."""
    t = (title or "").lower()
    t = re.split(r"\s+-\s+", t)[0]  # drop "\u2026 - Subtitle" tail (Re:ZERO -Starting\u2026)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = _SEASON_NOISE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


_BASE_CACHE = {}


def _cached_base(entry):
    slug = entry.get("slug")
    if slug and slug in _BASE_CACHE:
        return _BASE_CACHE[slug]
    base = _base_title(entry.get("title") or "")
    if slug:
        _BASE_CACHE[slug] = base
    return base


def _fmt_members(count):
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def _has_dub(entry):
    """Catalog `dub` is a stringified list, e.g. "['English','Japanese']"."""
    dub = entry.get("dub")
    if not dub:
        return False
    if isinstance(dub, list):
        return any("english" in str(x).lower() for x in dub)
    if isinstance(dub, str):
        low = dub.lower().strip()
        if low.startswith("["):
            try:
                return _has_dub({"dub": ast.literal_eval(dub)})
            except (ValueError, SyntaxError):
                pass
        return "english" in low or low in ("yes", "true", "1", "dubbed")
    return bool(dub)


# The catalog's genre tags never say "isekai", so the pool is scanned once
# from titles/slugs that carry an unmistakable isekai marker.
_ISEKAI_MARKERS = [
    "reincarnat", "tensei", "isekai", "another world", "in another",
    "summon", "no game no life", "sword art online", "log horizon",
    "overlord", "konosuba", "shield hero", "tanya the evil",
    "devil is a part-timer", "bookworm", "restaurant to another world",
    "death march", "arifureta", "cautious hero", "by the grace of the gods",
    "eminence in shadow", "tsukimichi", "spider, so what", "8th son",
    "spirit chronicles", "campfire cooking", "skeleton knight",
    "black summoner", "saving 80,000 gold", "handyman saitou",
    "reborn to master the blade", "drifters", "gate: jieitai",
    "re:zero", "knights & magic", "isekai ojisan", "so i'm a spider",
    "saint's magic power", "reborn as a vending machine",
    "my instant death ability",
]


def _scan_isekai_pool():
    """One entry per franchise for every clearly-isekai title in the catalog."""
    best = {}
    for entry in anime_database.values():
        probe = f"{(entry.get('title') or '')} {(entry.get('slug') or '')}".lower()
        if not any(m in probe for m in _ISEKAI_MARKERS):
            continue
        base = _cached_base(entry)
        if base and (base not in best or (entry.get("member_count") or 0) > (best[base].get("member_count") or 0)):
            best[base] = entry
    return list(best.values())


_ISEKAI_POOL = _scan_isekai_pool()


def _match_genres(msg):
    """Return canonical genres mentioned, tolerant of typos."""
    norm = _norm(msg)
    found = set()
    # exact alias hits (multi-word aliases included)
    for alias, canon in _GENRE_ALIAS.items():
        if alias in norm:
            found.add(canon)
    if found:
        return sorted(found)
    # fuzzy: each word within edit distance 2 of a canonical token
    for w in _words(msg):
        for canon in _CANONICAL:
            if _lev(w, canon) <= 2:
                found.add(canon)
    return sorted(found)


def _wants_dub(msg):
    norm = _norm(msg)
    if any(w in norm for w in ["dub", "dubbed", "english dub", "dubed", "with dub"]):
        return True
    return False


def _wants_sub(msg):
    norm = _norm(msg)
    if any(w in norm for w in ["sub", "subbed", "subtitle", "subtitled"]):
        return not _wants_dub(msg)
    return False


_REC_WORDS = [
    "recommend", "recommendation", "reccomend", "recomend", "recs", "suggest",
    "suggestions", "any good", "good anime", "what to watch", "what should i watch",
    "what anime", "similar to", "like ", "more like", "picks", "give me some",
    "anime to watch", "watch next",
]
_ASK_WORDS = [
    "recommend me some", "can you recommend", "please recommend", "pls recommend",
    "rec me", "suggest me", "any recommendations",
]


def _is_recommend_ask(msg):
    """True when the user is clearly asking for recommendations."""
    norm = _norm(msg)
    for w in _ASK_WORDS:
        if w in norm:
            return True
    # misspelled trigger words within edit distance 1
    for w in _words(msg):
        if _lev(w, "recommend") <= 2 or _lev(w, "suggest") <= 2:
            return True
    for w in _REC_WORDS:
        if w in norm:
            return True
    # bare genre + "anime" also reads as a rec request ("isekai anime with dub")
    genres = _match_genres(msg)
    if genres and ("anime" in norm or "show" in norm or "something" in norm):
        return True
    if genres and len(_words(norm)) <= 6:
        return True
    return False


_STOP = {
    "the", "and", "for", "with", "you", "your", "that", "this", "what", "about",
    "anime", "show", "series", "some", "give", "me", "good", "great", "best", "top",
    "favorite", "very", "really", "want", "need", "looking", "any", "please", "nice",
    "like", "similar", "recs", "reccomend", "recommend", "recommendation", "suggest",
}


def _find_title(msg):
    """Find a known anime title mentioned in the message (best effort)."""
    target = _norm(msg)
    if not target:
        return None
    words = [w for w in _words(target) if w not in _STOP and len(w) > 2]
    if not words:
        return None
    total_len = sum(len(w) for w in words)
    need = max(5, int(total_len * 0.6))
    best = None
    best_score = 0
    for slug, e in anime_database.items():
        title = _norm(e.get("title") or "")
        if not title:
            continue
        score = 0
        for w in words:
            if re.search(rf"\b{re.escape(w)}\b", title):
                score += len(w)
        if score > best_score:
            best_score = score
            best = e
        if best_score >= need:
            break
    if best_score >= need:
        return best
    return None


def _card_for(entry):
    """Build a compact card payload the front-end renders from the catalog."""
    rating = entry.get("rating")
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        rating = 0
    eps = entry.get("total_episodes")
    return {
        "slug": entry.get("slug") or "",
        "title": entry.get("title") or "",
        "image": entry.get("image") or "",
        "rating": round(rating, 1) if rating else None,
        "eps": eps if isinstance(eps, int) else (str(eps) if eps else None),
        "genre": (entry.get("genre") or "").split("\u2022")[0].strip(),
        "members": _fmt_members(entry.get("member_count")),
        "year": entry.get("start_year") or (entry.get("release") or "").split("-")[0],
        "dub": _has_dub(entry),
    }


def _pick_from_catalog(genres, want_dub=False, limit=5):
    """Pick up to `limit` catalog entries matching the requested genres.

    Season/sequel duplicates collapse to one entry per franchise so a
    recommendation list reads like a human's picks, not a database dump.
    Isekai has no genre tag in the catalog, so it is served from a pool of
    clearly-isekai titles scanned at startup; anything else matches the
    catalog's real genre tags.
    """
    genres = [g for g in (genres or [])]
    if "isekai" in genres:
        pool = _ISEKAI_POOL
        terms = [g for g in genres if g != "isekai"]
    else:
        pool = list(anime_database.values())
        terms = genres

    picked = []
    seen = set()
    for entry in pool:
        if terms:
            genre = (entry.get("genre") or "").lower()
            ok = True
            for g in terms:
                needles = _GENRE_CATALOG.get(g) or [g]
                if not any(n in genre for n in needles):
                    ok = False
                    break
            if not ok:
                continue
        if want_dub and not _has_dub(entry):
            continue
        base = _cached_base(entry)
        if base in seen:
            continue
        seen.add(base)
        picked.append(entry)
        if len(picked) >= 80:
            break

    if not picked:
        return []

    def _rank(entry):
        try:
            rating = float(entry.get("rating") or 0)
        except (TypeError, ValueError):
            rating = 0
        return ((entry.get("member_count") or 0), rating)

    picked.sort(key=_rank, reverse=True)
    return [_card_for(e) for e in picked[:limit]]


def _recommend_reply(msg):
    """Full recommendation flow → (text, cards). Always offline-capable."""
    genres = _match_genres(msg)
    want_dub = _wants_dub(msg)
    want_sub = _wants_sub(msg)

    # "recommend something like <Title>" → treat the title's genres as the ask
    title = _find_title(msg)
    if not genres and title:
        genres = _match_genres(title.get("genre") or "")

    if not genres:
        scope = "a mix of crowd favorites"
        follow_up = (" Tell me a genre — isekai, action, romance, comedy, thriller — "
                     "and I'll tailor the next batch!")
    else:
        scope = ", ".join(_GENRE_DISPLAY.get(g, g.title()) for g in genres)
        follow_up = ""

    cards = _pick_from_catalog(genres, want_dub=want_dub, limit=5)

    if want_dub:
        scope += " with an English dub"
    elif want_sub:
        scope += " with subs"

    if not cards and want_dub:
        # Rare: nothing matched with an English dub — relax the audio filter
        # instead of abandoning the genre the user asked for.
        cards = _pick_from_catalog(genres, limit=5)
        if cards:
            text = ("Couldn't fill a list of English-dubbed shows for that one, so here are "
                    "the top picks in that lane — most of these got dubs too. Tap a card to "
                    "check its dub languages on the anime page!")
            return text, cards

    if not cards:
        # No exact match — fall back to the site's genuinely popular titles.
        cards = _pick_from_catalog([], limit=5)
        text = ("I couldn't find a strong match for that combo in the catalog — "
                "here are some universally loved picks instead. Try another genre like "
                "isekai, romance, or thriller, and I'll pull fresh cards!")
        return text, cards

    lead = cards[0]
    label = "show" if len(cards) == 1 else "shows"
    text = (f"Here are {len(cards)} {label} matching {scope} — straight from the Otakul "
            f"catalog. Starting strong with {lead['title']}: tap any card to open its page "
            f"and jump into the community chat!{follow_up}")
    return text, cards


def _anime_fact_reply(entry):
    """Answer data questions about a specific show using OUR catalog."""
    title = entry.get("title") or "This show"
    genre = entry.get("genre") or "?"
    eps = entry.get("total_episodes") or "?"
    status = entry.get("status") or ""
    year = (entry.get("release") or "").split("-")[0] or entry.get("start_year") or "?"
    studio = entry.get("studio") or "?"
    try:
        rating = float(entry.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    members = _fmt_members(entry.get("member_count"))
    syn = (entry.get("synopsis") or "").strip()
    if len(syn) > 220:
        syn = syn[:220].rsplit(" ", 1)[0] + "\u2026"
    dub_note = " \u00b7 English dub available" if _has_dub(entry) else ""
    rating_txt = f"\u2b50 {rating:.1f}/5" if rating else "no community rating yet"
    text = f"\U0001f3ac {title} \u2014 {status or 'Anime'}, {year}{dub_note}.\n"
    text += f"Genre: {genre} | Studio: {studio} | Episodes: {eps}\n"
    text += f"{rating_txt} \u00b7 {members} members on Otakul\n"
    if syn:
        text += f"\n{syn}"
    text += "\n\nWant me to suggest similar shows, or open its page for reviews and chat?"
    return text


def _site_topic_reply(msg):
    """Site / XP / guilds / features answers + battle debates + fallback."""
    # Battles ("who would win")
    if any(w in msg for w in ["who would win", "who wins", "vs", "fight", "battle", "stronger", "strongest"]):
        found = []
        for name in ["goku", "ichigo", "naruto", "sasuke", "luffy", "zoro", "saitama", "gojo",
                     "itadori", "tanjiro", "lelouch", "levi", "eren", "gon", "killua", "guts", "tanya"]:
            if name in _norm(msg):
                found.append(name)
        if len(found) >= 2:
            return (f"⚔️ Fun matchup! Both are absolute monsters in their verses — raw power vs. "
                    f"hax decides it. Tell me which two you want and I'll break down who takes it "
                    f"and why, or debate it in a War Zone on Otakul!")
        return ("Ooh, a battle debate! 🥊 Tell me exactly which two characters — like "
                "'Goku vs Naruto' — and I'll give you my verdict. You can also start a real "
                "Reply War under a review and let the community vote!")
    # XP / rank
    if any(w in msg for w in ["xp", "rank", "level", "tier", "experience", "unlock"]):
        return ("On Otakul you earn XP from likes on your reviews, Reply Wars and community "
                "participation. Ranks go F → D → C → B → A → S → S+. At C rank (500 XP) you "
                "unlock dislikes, Reply Wars and War Zone creation — check your profile for "
                "your current XP!")
    # reviews
    if any(w in msg for w in ["review", "rating", "rate", "stars"]):
        return ("Reviews on Otakul use a 10-star verdict scale — 1–4 RED (negative), 5 grey, "
                "6–10 GREEN. Reviews earn their own Review XP and you earn profile XP from "
                "likes. Dislikes need C rank (500 XP). Tap any anime page to rate it!")
    # guilds/threads
    if any(w in msg for w in ["guild", "thread"]):
        return ("Guilds (on the Threads page) are communities with roles — owner, moderator, "
                "member — plus channels for chat and media, and guild wars for guild XP. Create "
                "one from Threads or join an existing community!")
    # wars
    if "war" in msg or "warzone" in msg or "war zone" in msg:
        return ("War Zone is Otakul's debate arena — post a declaration, battlers enter their "
                "best takes, and the community votes by like-ratio. Wars settle after 24–72 "
                "hours. C rank unlocks creating wars, and guilds can challenge each other in "
                "GvG wars!")
    # quiz
    if any(w in msg for w in ["new to anime", "quiz", "starter", "beginner"]):
        return ("The 'New to Anime' quiz in the navbar is the fastest way to get started — it "
                "asks your preferences and recommends anime built around your taste. I can also "
                "hand you recommendation cards right here — just ask!")
    # characters
    if any(w in msg for w in ["character", "characters", "cast", "voice actor"]):
        return ("Otakul has a searchable Characters page with 10k+ characters — names, roles "
                "and the anime they appear in. Ask me 'who would win' matchups too and I'll "
                "debate them!")
    # lists
    if any(w in msg for w in ["list", "watching", "completed", "plan to watch", "dropped"]):
        return ("You can track anime with four lists — Watching, Completed, Plan to Watch and "
                "Dropped — with per-episode progress. Add shows from any anime page and your "
                "profile keeps it all organized.")
    # how do i / where do i (site help default)
    if any(w in msg for w in ["how do i", "where do i", "how to", "how can i", "sign up", "login", "log in"]):
        return ("Happy to help! The navbar is your map: Browse for the catalog, Reviews for "
                "the feed, Threads for guilds, and your profile pill (top right) for settings "
                "and lists. Tell me what you're trying to do and I'll point you to the exact page!")
    # fallback
    return ""


def _smart_reply(history):
    """Route a user message to the right brain. Returns (text, cards).

    • Recommendation asks  → real anime CARDS from the Otakul catalog (max 5),
      always handled locally so they never depend on the LLM being online.
    • "Tell me about X"    → facts pulled from OUR catalog (title, studio,
      episodes, rating, synopsis, members) when the show is recognized.
    • Site / system Qs     → answered from Otakul's actual feature set.
    • Everything else      → the LLM when a key is set, else a friendly reply.
    """
    user_msg = ""
    for m in reversed(history):
        if m.get("role") == "user":
            user_msg = (m.get("content") or "").strip()
            break
    norm = _norm(user_msg)

    # Greetings / identity / small talk
    if any(w in norm for w in ["who are you", "what can you do", "your name", "what are you"]):
        return ("I'm Ota-chan, Otakul's own AI assistant! 🎌 I live on Otakul's catalog — I "
                "can hand you anime recommendation cards (up to 5 at a time), answer questions "
                "about shows with real data, explain how the site works (XP, reviews, guilds, "
                "War Zones), and debate 'who would win' matchups."), []
    if any(w in norm for w in ["hello", " hi", " hey", " sup", "yo", "what's up", "how are you", "hii", "good morning", "good evening"]):
        return random.choice([
            "Hey there! 🎌 Ask me for anime recs and I'll send over cards straight from the catalog — or ask about Otakul itself!",
            "Hii! Want anime recommendations, show details, or help with the site? I'm all ears!",
            "Yo! I'm Ota-chan — ask me for anime picks and I'll show you actual cards you can tap.",
        ]), []
    if any(w in norm for w in ["bye", "goodbye", "see you", "thank you", " thanks", "thx"]):
        return random.choice([
            "See you later! Happy anime-ing! ✨",
            "Bye! Come back whenever you need a recommendation! 🎌",
            "Take care! I'll be here when you want more picks 💜",
        ]), []

    # --- Anime title recognized → talk about THAT show with real data ---
    entry = _find_title(user_msg)
    if entry and any(w in norm for w in ["tell me about", "about ", "how many episodes", "episodes does",
                                         "when did", "who made", "studio", "rating", "synopsis", "is it good",
                                         "what is ", "review of", "similar to", "like "] or len(_words(norm)) <= 3):
        text = _anime_fact_reply(entry)
        # "similar/like X" also returns recommendation cards
        if "similar" in norm or "like " in norm or "recommend" in norm or "more like" in norm:
            genres = _match_genres(entry.get("genre") or "")
            cards = _pick_from_catalog(genres, limit=5)
            if cards:
                text += "\n\nThese are closest to it in the catalog:"
                return text, cards
        return text, []

    # --- Recommendation asks → cards ---
    if _is_recommend_ask(user_msg):
        return _recommend_reply(user_msg)

    # --- Site features ---
    site_ans = _site_topic_reply(norm)
    if site_ans:
        return site_ans, []

    # --- Anime-worded asks without a clear intent ---
    if any(w in norm for w in ["anime", "manga", "show", "series", "season", "episode"]):
        return ("Happy to dig into any show we have! Ask me things like 'recommend isekai anime "
                "with a dub', 'tell me about Steins;Gate', or 'similar to Attack on Titan' — I'll "
                "answer from the Otakul catalog and send cards you can tap right into."), []

    # --- LLM free-for-all when a key exists ---
    if LLM_API_KEY:
        try:
            reply = _call_llm(_build_system_prompt(), history, max_tokens=500)
            if reply:
                return reply, []
        except requests.exceptions.Timeout:
            return "Sorry, I zoned out for a second — can you say that again?", []
        except Exception as exc:
            print(f"[otachan] chat API error: {exc}", flush=True)

    return random.choice([
        "I can hand you recommendation cards (try 'recommend isekai anime with a dub'), answer "
        "show questions from our catalog, or explain Otakul — XP, reviews, guilds, War Zones.",
        "Not sure I caught that — but ask me for anime picks ('recommend me some romance'), "
        "details on any show we have, or how Otakul works!",
        "Try asking for recommendations, a specific anime ('tell me about Frieren'), or site help!",
    ]), []

# -------------------------------------------------------------------
# LLM call
# -------------------------------------------------------------------
def _call_llm(system_prompt, messages, max_tokens=600):
    """Call the Groq API (OpenAI-compatible). Returns text or raises."""
    if not LLM_API_KEY:
        raise RuntimeError("LLM API key not set")

    full_messages = [{"role": "system", "content": system_prompt}]
    for m in messages:
        full_messages.append({"role": m["role"], "content": m["content"]})

    resp = requests.post(
        LLM_BASE_URL,
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "max_tokens": max_tokens,
            "messages": full_messages,
        },
        timeout=LLM_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    choices = data.get("choices", [])
    if choices:
        return (choices[0].get("message", {}).get("content", "") or "").strip()
    return ""


def _build_system_prompt():
    """Build Ota-chan's system prompt."""
    return f"""You are Ota-chan, the friendly mascot and assistant for Otakul, an anime discussion and discovery site. You help users in two ways:

1. SITE HELP — Answer questions about how Otakul works, using this reference:
{SITE_FEATURES_REFERENCE}
   If you don't know something about the site specifically, say so honestly and suggest where on the site they might look, rather than guessing.

2. ANIME KNOWLEDGE — Answer ANY general question about anime: characters, shows, plot details, franchise trivia, recommendations, and fun hypothetical debates (e.g., "who would win in a fight"). Be enthusiastic and give a fun, opinionated take for debate questions; be accurate and clear for factual questions. Don't limit yourself to a fixed list of shows or characters — answer whatever the user asks about.

PERSONALITY: Warm, upbeat, a little playful — like a knowledgeable friend who's excited to help, not a corporate support bot. Keep responses conversational and not too long (2-5 sentences typically) unless the question genuinely needs more detail.

Never claim to BE any existing anime character — you are Ota-chan, Otakul's own original mascot. You can talk ABOUT anime characters freely, just don't roleplay as them.

If a user asks for anything harmful, dangerous, or illegal, decline warmly and redirect rather than lecturing.

If a user seems genuinely distressed, gently step out of the casual tone to encourage them to talk to a real person or seek support.
"""


# -------------------------------------------------------------------
# Auth helpers
# -------------------------------------------------------------------
def _user():
    return g.get("user")

def _require_user():
    user = _user()
    if user is None:
        return None, (jsonify({"error": "auth"}), 401)
    return user, None


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@bp.route("/otachan")
def otachan_page():
    """Chat page. Requires login."""
    user = _user()
    if user is None:
        from flask import redirect, flash
        flash("Log in to chat with Ota-chan.", "error")
        return redirect(url_for("auth.login", next=request.path))

    return render_template(
        "otachan.html",
        genres=[],  # navbar expects this
    )


@bp.route("/otachan/status")
def otachan_status():
    """Return conversation history for page load."""
    user, err = _require_user()
    if err:
        return err

    chat = site_db.get_ota_chan_chat(user["id"])
    if not chat:
        return jsonify({
            "has_history": False,
            "history": [],
            "greeting": GREETING,
        })

    history = []
    try:
        history = json.loads(chat.get("conversation_history") or "[]")
    except (ValueError, TypeError):
        history = []

    return jsonify({
        "has_history": bool(history),
        "history": history,
        "greeting": GREETING,
    })


@bp.route("/otachan/message", methods=["POST"])
def otachan_message():
    """Send a message and get Ota-chan's reply."""
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Type a message first."}), 400
    if len(message) > 2000:
        return jsonify({"error": "Message too long (2000 char max)."}), 400

    # Load existing conversation
    chat = site_db.get_ota_chan_chat(user["id"])
    try:
        history = json.loads(chat.get("conversation_history") or "[]") if chat else []
    except (ValueError, TypeError):
        history = []

    # Append the new user message
    history.append({"role": "user", "content": message})

    # Cap at MAX_HISTORY messages
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]

    # Ota-chan's smart reply — recommendation asks always return real
    # anime cards from the catalog (max 5), answered locally so they
    # never depend on the LLM being reachable.
    reply, cards = _smart_reply(history)

    # Append the reply (with any recommendation cards) and persist
    assistant_entry = {"role": "assistant", "content": reply}
    if cards:
        assistant_entry["cards"] = cards
    history.append(assistant_entry)

    # Cap again before saving
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]

    site_db.set_ota_chan_chat(user["id"], json.dumps(history))

    return jsonify({
        "success": True,
        "reply": reply,
        "cards": cards,
        "history": history,
    })


# -------------------------------------------------------------------
# Registration hook
# -------------------------------------------------------------------

def init_ota_chan(app):
    """Call from app.py. Creates the ota_chan table and registers the
    blueprint. Idempotent."""
    site_db.create_tables()
    app.register_blueprint(bp)
