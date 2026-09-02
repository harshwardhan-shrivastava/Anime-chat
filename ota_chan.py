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

import json
import os
import random
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


def _template_reply(history):
    """Generate a helpful reply without calling the API.
    Detects common question patterns and responds as Ota-chan."""
    user_msg = ""
    for m in reversed(history):
        if m.get("role") == "user":
            user_msg = m.get("content", "").lower()
            break

    # ── Greetings & identity ──
    if any(w in user_msg for w in ["who are you", "tell me about yourself", "what can you do", "your name", "what are you"]):
        return "I'm Ota-chan, Otakul's mascot and AI assistant! 🎌 I can help you navigate the site (XP, reviews, guilds, War Zones), answer anime questions, give recommendations, and even debate 'who would win' matchups. What do you want to know?"

    if any(w in user_msg for w in ["hello", "hi!", "hey", "yo!", "sup", "what's up", "how are you", "hii"]):
        return random.choice([
            "Hey there! 🎌 Ask me about anime, get recommendations, or learn how Otakul works!",
            "Hii! Welcome to Otakul! Want anime recs, trivia, or help with the site?",
            "Yo! I'm Ota-chan — your anime assistant. What's on your mind?",
        ])

    if any(w in user_msg for w in ["bye", "goodbye", "see you", "thanks", "thank you", "thx"]):
        return random.choice([
            "See you later! Happy anime-ing! ✨",
            "Bye! Come back anytime you need help! 🎌",
            "Take care! I'll be here when you need me! 💜",
        ])

    # ── XP & Ranks ──
    if any(w in user_msg for w in ["xp", "rank", "level", "tier", "experience"]):
        return "On Otakul, you earn XP by getting likes on your reviews, winning Reply Wars, and community participation. Ranks go F → D → C → B → A → S → S+. At C rank (500 XP) you unlock dislikes, Reply Wars, and War Zone creation! Check your profile to see your current XP."

    # ── Reviews ──
    if any(w in user_msg for w in ["review", "rating", "rate"]):
        return "Reviews use a 2-10 star rating scale with text comments. Likes from higher-ranked users give more XP. At C rank (500 XP), you can also dislike reviews. Each review page has a Reply War section where you can debate stances!"

    # ── Guild / Thread / Community ──
    if any(w in user_msg for w in ["guild", "thread"]):
        return "Guilds are communities with roles (owner/moderator/member). Each guild has channels for chat and media. Guilds can compete in Guild Wars for guild XP! Create one from the Threads page, or discover existing ones."

    if any(w in user_msg for w in ["war zone", "warzone", "war"]):
        return "War Zone is Otakul's debate arena! Post a declaration (your position), battlers enter their best takes, and the community votes. You need C rank to create wars. Guilds can also challenge each other in GvG wars! Wars settle after 24-72 hours."

    # ── New to Anime ──
    if any(w in user_msg for w in ["new to anime", "quiz", "recommend for me", "what should i watch"]):
        return "Try the 'New to Anime' quiz in the navbar! It asks you fun preference questions and recommends anime based on your taste. It's the fastest way to find something you'll love!"

    # ── Anime lists ──
    if any(w in user_msg for w in ["list", "watching", "completed", "plan to watch", "dropped"]):
        return "Otakul has anime lists: Watching, Completed, Plan to Watch, and Dropped — with episode progress tracking! Add anime from any anime page. Track your progress and it shows on your profile."

    # ── Chat / Community ──
    if any(w in user_msg for w in ["chat", "community", "message"]):
        return "Every anime has its own community chat room. Join from the anime's page and discuss in real-time with GIF support! It's a great way to talk about specific shows with other fans."

    # ── Profile ──
    if any(w in user_msg for w in ["profile", "avatar"]):
        return "Your profile shows your avatar, username, rank badge, XP, review history, anime lists, and activity. You can toggle it between public and private in Settings!"

    # ── Characters ──
    if any(w in user_msg for w in ["character", "characters", "cast"]):
        return "Otakul has a searchable database of 10,000+ anime characters with images, roles, and anime affiliations! Check the Characters page in the navbar to search by name."

    # ── 'Who would win' battles ──
    if any(w in user_msg for w in ["who would win", "vs", "fight", "who wins", "battle", "stronger", "strongest"]):
        # Find character names mentioned
        found_chars = []
        for name, data in _BATTLE_DATA.items():
            if name in user_msg:
                found_chars.append((name, data))
        if len(found_chars) >= 2:
            chars_text = " vs. ".join(c[1] for c in found_chars)
            return f"⚔️ Great matchup! {chars_text} — This is a legendary debate! Both have insane feats. If it's pure destructive power, the Dragon Ball character has universe-level feats. But hax abilities (like Gojo's Infinity or Lelouch's Geass) can bypass raw power. Who do YOU think wins? I'd love to hear your take!"
        elif len(found_chars) == 1:
            name, data = found_chars[0]
            return f"{data} They're definitely one of the strongest in their verse! Who would you want to pit them against in a fight?"
        else:
            return "Ooh, a battle debate! 🥊 Tell me which specific characters you want to compare — like 'Goku vs Naruto' or 'Gojo vs Saitama' and I'll break down the matchup!"

    # ── Recommendations ──
    if any(w in user_msg for w in ["recommend", "suggest", "should i watch", "good anime"]):
        rec_key = "default"
        if "isekai" in user_msg:
            rec_key = "isekai"
        elif "action" in user_msg:
            rec_key = "action"
        elif "romance" in user_msg or "love" in user_msg:
            rec_key = "romance"
        elif "comedy" in user_msg or "funny" in user_msg:
            rec_key = "comedy"
        elif "thriller" in user_msg or "suspense" in user_msg:
            rec_key = "thriller"
        elif "best" in user_msg or "top" in user_msg or "greatest" in user_msg:
            rec_key = "best"
        recs = _RECOMMENDATIONS[rec_key]
        chosen = random.sample(recs, min(3, len(recs)))
        picks = "\n".join(f"• {r}" for r in chosen)
        return f"🎬 Here are my picks:\n{picks}\nWant more? Tell me a specific genre or ask about any anime!"

    # ── Anime-specific questions ──
    if any(w in user_msg for w in ["anime", "manga", "show", "series", "watch", "season", "episode"]):
        return "Tell me what specific anime or genre you're interested in! I can give detailed info about shows, recommend by genre (isekai, action, romance, comedy, thriller), or debate matchups between characters."

    # ── Default ──
    return random.choice([
        "I can help with anime recommendations, character info, 'who would win' debates, and questions about how Otakul works (XP, reviews, guilds, War Zones). What are you interested in?",
        "Not sure what you mean — but I'm great at anime trivia, recommendations, site help, and battle debates! Try asking about a specific anime or character.",
        "Hmm, try asking me about a specific anime, character, or genre — or ask about Otakul features like XP, reviews, or guilds!",
    ])


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

    # Build system prompt
    system_prompt = _build_system_prompt()

    # Call LLM or use template replies
    if not LLM_API_KEY:
        reply = _template_reply(history)
    else:
        try:
            reply = _call_llm(system_prompt, history, max_tokens=500)
            if not reply:
                reply = _template_reply(history)
        except requests.exceptions.Timeout:
            reply = "Sorry, I zoned out for a second — can you say that again?"
        except Exception as exc:
            print(f"[otachan] chat API error: {exc}", flush=True)
            reply = _template_reply(history)

    # Append the reply and persist
    history.append({"role": "assistant", "content": reply})

    # Cap again before saving
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]

    site_db.set_ota_chan_chat(user["id"], json.dumps(history))

    return jsonify({
        "success": True,
        "reply": reply,
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
