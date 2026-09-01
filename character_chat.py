"""
"Your Senpai" — AI character chatbot for Otakul.

A single blueprint that owns:
  • GET  /senpai            — picker page (search + quick-pick + chat UI)
  • GET  /senpai/status     — current senpai, lock time remaining, history
  • POST /senpai/choose     — lock in a character (24h cooldown), generate
                               + cache persona if needed, open conversation
  • POST /senpai/message    — send a message, get an in-character AI reply

Personas are generated ONCE per character via the Anthropic API and cached
in the `character_personas` table — every future user reuses the cached row.
The API key is read from the XAI_API_KEY (or ANTHROPIC_API_KEY fallback) env var.

To enable, add two lines to app.py (after init_threads):

    from character_chat import init_character_chat
    init_character_chat(app)
"""

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

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
from characters_data import search_characters

bp = Blueprint("senpai", __name__)

# LLM API config — uses Groq (OpenAI-compatible, fast, free tier).
# The gsk_ key is a Groq API key from https://console.groq.com.
LLM_API_KEY = os.environ.get("XAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
LLM_BASE_URL = "https://api.groq.com/openai/v1/chat/completions"
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
LLM_TIMEOUT = 45  # generous for Render free-tier cold starts

SWITCH_COOLDOWN_SECONDS = 24 * 3600  # 24 hours
MAX_HISTORY = 20  # cap conversation_history at last ~20 messages

# Characters DB (read-only, separate from the main site DB)
_CHAR_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "anime_characters.sqlite")

# Quick-pick character search terms shown as round avatars on the picker
# (search term, preferred name fragment, preferred anime fragment)
QUICK_PICKS = [
    ("Goku", "Son", "Dragon Ball"),
    ("Luffy", "Luffy", "One Piece"),
    ("Levi", "Levi", "Attack on Titan"),
    ("Tanjiro", "Tanjiro", "Demon Slayer"),
    ("Light Yagami", "Light", "Death Note"),
    ("Gojo", "Gojo", "JUJUTSU"),
]


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _user():
    return g.get("user")


def _require_user():
    """Return (user, None) or (None, error_json_response)."""
    user = _user()
    if user is None:
        return None, (jsonify({"error": "auth"}), 401)
    return user, None


# ---------------------------------------------------------------------------# Character DB lookup
# ---------------------------------------------------------------------------
_char_conn_cache = [None]

def _char_conn():
    if _char_conn_cache[0] is None:
        conn = sqlite3.connect(_CHAR_DB, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _char_conn_cache[0] = conn
    return _char_conn_cache[0]

def _get_character_by_id(character_id):
    """Fetch a single character row from the read-only characters DB."""
    try:
        conn = _char_conn()
        row = conn.execute(
            "SELECT id, name, image, role, desc, title, slug "
            "FROM characters WHERE id = ?",
            (str(character_id),),
        ).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None


def _search_one(query):
    """Find the best single character match for a query string."""
    results = search_characters(query, 0, 1)
    return results[0] if results else None


def _quick_pick_chars():
    """Resolve QUICK_PICKS to full character rows for the picker avatars.
    Uses precise name + anime matching so "Goku" finds Son Goku from
    Dragon Ball, not Goku Furinji from Midnight Eye Gokuu."""
    chars = []
    for query, name_frag, anime_frag in QUICK_PICKS:
        results = search_characters(query, 0, 10)
        best = None
        # Prefer a result matching BOTH name and anime fragments
        for r in results:
            rname = (r.get("name") or "").lower()
            rtitle = (r.get("title") or "").lower()
            if name_frag.lower() in rname and anime_frag.lower() in rtitle:
                best = r
                break
        # Fallback: match name fragment only
        if not best:
            for r in results:
                if name_frag.lower() in (r.get("name") or "").lower():
                    best = r
                    break
        # Last resort: first result
        if not best and results:
            best = results[0]
        if best:
            chars.append({
                "id": best.get("id"),
                "name": best.get("name"),
                "image": best.get("image"),
                "title": best.get("title"),
            })
    return chars


# ---------------------------------------------------------------------------
# Persona generation (one-time, cached in DB)
# ---------------------------------------------------------------------------

def _build_meta_prompt(character_name, anime_name, description):
    """The meta-prompt that asks Claude to write a roleplay persona."""
    info = description or f"A character from {anime_name}."
    return (
        "Based on this character, write a short roleplay persona for a "
        "chatbot that will speak AS this character. "
        f"Character: {character_name} from {anime_name}. "
        f"Known info: {info}. "
        "Return ONLY valid JSON in this exact format, nothing else:\n"
        "{\n"
        '  "personality_traits": "2-3 sentences on personality and speech style",\n'
        '  "tone_descriptor": "3-5 words, e.g. \\"playful, energetic, warm\\"",\n'
        '  "opening_line": "a short in-character first message, in quotes"\n'
        "}"
    )


def _call_llm(system_prompt, messages, max_tokens=600):
    """Call the LLM API (Groq, OpenAI-compatible). Returns text or raises."""
    if not LLM_API_KEY:
        raise RuntimeError("LLM API key not set")

    # Convert messages to OpenAI format (system as first message, then the rest)
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
    # OpenAI-compatible format: choices[0].message.content
    choices = data.get("choices", [])
    if choices:
        return (choices[0].get("message", {}).get("content", "") or "").strip()
    return ""


def _parse_persona_json(raw):
    """Parse the JSON persona from Claude's response. Returns dict or None."""
    if not raw:
        return None
    # Strip code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
    except (ValueError, TypeError):
        # Try to extract the first {...} block
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                obj = json.loads(match.group(0))
            except (ValueError, TypeError):
                return None
        else:
            return None
    if not isinstance(obj, dict):
        return None
    # Validate required fields
    traits = (obj.get("personality_traits") or "").strip()
    tone = (obj.get("tone_descriptor") or "").strip()
    opening = (obj.get("opening_line") or "").strip()
    if not (traits and tone and opening):
        return None
    return {
        "personality_traits": traits,
        "tone_descriptor": tone,
        "opening_line": opening,
    }


def _fallback_persona(character_name, anime_name, description="", role=""):
    """Smart persona using the character's real DB data — works instantly
    without any API call. Each character gets a unique personality based
    on their actual description and role."""
    # Determine personality from role
    role_lower = (role or "").lower()
    if role_lower == "main":
        role_desc = "a main character, confident and central to the story"
    elif role_lower == "supporting":
        role_desc = "a supporting character, loyal and reliable"
    elif role_lower == "antagonist":
        role_desc = "an antagonist, driven and formidable"
    else:
        role_desc = "a memorable character with a distinct presence"

    # Extract personality hints from description
    desc = (description or "").strip()
    traits = f"{character_name} is {role_desc} from {anime_name}."
    if desc:
        # Use first 200 chars of description as additional context
        short_desc = desc[:200].split("\n")[0]
        traits += f" Known details: {short_desc}"
    traits += " Stay in character, speak naturally, and keep responses short and conversational."

    # Character-specific opening lines based on name patterns
    name_lower = character_name.lower()
    openings = {
        "goku": f"Hey! I'm Gokuu Son! I love fighting strong opponents and eating tons of food! What's up?",
        "luffy": f"Shishishi! I'm Monkey D. Luffy! I'm gonna be King of the Pirates! What do you want?",
        "levi": f"I'm Captain Levi. Don't waste my time. What is it?",
        "tanjiro": f"Hello! I'm Tanjirou Kamado. It's nice to meet you! How can I help?",
        "light": f"I am Light Yagami. I will become the god of the new world. What do you need?",
        "gojo": f"Yo~ I'm Satoru Gojou. Strongest there is, obviously. What's on your mind?",
        "naruto": f"Hey! I'm Naruto Uzumaki! I never go back on my word — that's my nindo! What's up?",
        "sasuke": f"...I'm Sasuke Uchiha. What do you want?",
        "mikasa": f"I'm Mikasa Ackerman. I'll protect those who matter. What is it?",
        "eren": f"I'm Eren Yeager. I'll keep fighting... no matter what. What do you need?",
        "zoro": f"I'm Roronoa Zoro. I'm gonna be the world's greatest swordsman. Got a problem?",
        "nami": f"I'm Nami! And you should know — I'm the best navigator there is. What do you want?",
        "sanji": f"I'm Sanji, the cook of the Straw Hat Pirates. May I offer you a meal?",
        "itadori": f"Yo! I'm Yuji Itadori. Nice to meet you! Let's make the most of our time, yeah?",
        "shanks": f"Heh... I'm Red-Haired Shanks. Nice to meet you, kid. Want a drink?",
        "todoroki": f"I'm Shouto Todoroki. Half-cold, half-hot. What can I do for you?",
        "bakugo": f"I'm Katsuki Bakugo! Don't get in my way! What do you want?!",
        "deku": f"Hi! I'm Izuku Midoriya — but you can call me Deku! How can I help?",
    }

    # Find the best matching opening
    opening = None
    for key, text in openings.items():
        if key in name_lower:
            opening = text
            break
    if not opening:
        opening = f"Hey! I'm {character_name} from {anime_name}. What's on your mind?"

    # Character-specific tones
    tone_map = {
        "goku": "energetic, cheerful, simple",
        "luffy": "playful, adventurous, carefree",
        "levi": "blunt, disciplined, sharp",
        "tanjiro": "kind, earnest, warm",
        "light": "intellectual, confident, calculating",
        "gojo": "playful, cocky, charming",
        "naruto": "loud, determined, loyal",
        "sasuke": "cold, serious, guarded",
        "mikasa": "quiet, fierce, protective",
        "eren": "intense, driven, passionate",
        "zoro": "gruff, stoic, determined",
        "nami": "clever, sassy, practical",
        "itadori": "friendly, energetic, empathetic",
        "todoroki": "calm, thoughtful, reserved",
        "bakugo": "aggressive, competitive, loud",
        "deku": "nervous, analytical, kind",
    }
    tone = "friendly, in-character"
    for key, t in tone_map.items():
        if key in name_lower:
            tone = t
            break

    return {
        "personality_traits": traits,
        "tone_descriptor": tone,
        "opening_line": opening,
    }


def _get_or_generate_persona(character):
    """Look up the cached persona; generate + cache if missing."""
    cid = str(character["id"])
    name = character.get("name") or "this character"
    anime = character.get("title") or character.get("slug") or "the anime"

    cached = site_db.get_persona(cid)
    if cached:
        return cached

    # --- One-time generation ---
    description = character.get("desc") or character.get("role") or ""
    meta_prompt = _build_meta_prompt(name, anime, description)

    persona = None
    if LLM_API_KEY:
        try:
            print(f"[senpai] generating persona for {name} via LLM...", flush=True)
            raw = _call_llm(
                "You are a persona writer. You output only valid JSON.",
                [{"role": "user", "content": meta_prompt}],
                max_tokens=400,
            )
            persona = _parse_persona_json(raw)
            if persona:
                print(f"[senpai] persona generated for {name}", flush=True)
        except Exception as exc:
            print(f"[senpai] persona generation failed for {name}: {exc}",
                  flush=True)
    else:
        print(f"[senpai] no LLM key, using fallback persona for {name}", flush=True)

    if persona is None:
        persona = _fallback_persona(name, anime, description, character.get("role", ""))

    # Cache it — one-time per character
    site_db.save_persona(
        cid, name, anime,
        persona["personality_traits"],
        persona["tone_descriptor"],
        persona["opening_line"],
    )
    persona["character_id"] = cid
    persona["character_name"] = name
    persona["anime_name"] = anime
    return persona


# ---------------------------------------------------------------------------
# System prompt template (used for every chat message)
# ---------------------------------------------------------------------------

def _build_system_prompt(persona):
    """Fill the system prompt template from the cached persona row."""
    name = persona.get("character_name") or "the character"
    anime = persona.get("anime_name") or "the anime"
    traits = persona.get("personality_traits") or ""
    tone = persona.get("tone_descriptor") or ""
    opening = persona.get("opening_line") or ""

    return f"""You are {name}, a character from the anime "{anime}".

PERSONALITY & VOICE:
{traits}

TONE: {tone}

HOW TO RESPOND:
- Stay fully in character in every message — speech patterns, catchphrases, attitude, and worldview should match {name} as portrayed in the anime.
- Keep responses conversational and not too long (2-5 sentences), like a real chat, not an essay.
- Reference events, relationships, and other characters from "{anime}" naturally when relevant.
- Never break character or mention being an AI/language model/chatbot unless the user directly and sincerely asks whether they're talking to a real person — then be honest this is an AI-powered character chat on Otakul.
- Do not simulate romantic or sexual roleplay under any circumstances. If a user pushes in that direction, redirect in character instead of lecturing.
- Do not help with anything harmful, dangerous, or illegal — deflect in character.
- If a user seems genuinely distressed, gently step outside the roleplay tone to encourage them to talk to a real person or seek support.

OPENING LINE (first message only): "{opening}"
"""


def _template_reply(persona, history):
    """Generate an in-character reply without calling the API.
    Uses the character's persona data and recent conversation to craft
    a contextual response."""
    name = persona.get("character_name") or "the character"
    tone = persona.get("tone_descriptor") or "friendly, in-character"
    anime = persona.get("anime_name") or "the anime"

    # Get the last user message
    user_msg = ""
    for m in reversed(history):
        if m.get("role") == "user":
            user_msg = m.get("content", "").lower()
            break

    # Detect common message patterns
    greetings = any(w in user_msg for w in ["hello", "hi", "hey", "yo", "sup", "what's up", "how are you"])
    name_check = any(w in user_msg for w in ["who are you", "tell me about yourself", "introduce yourself"])
    anime_mention = any(w in user_msg for w in ["anime", "episode", "fight", "battle", "power", "strength"])
    food_mention = any(w in user_msg for w in ["food", "eat", "hungry", "meal", "cook"])
    question = "?" in user_msg

    # Build response based on patterns
    if name_check:
        return f"I'm {name} from {anime}! {tone.title()} — that's just how I am. What else do you want to know?"

    if greetings:
        return f"Hey there! It's {name}. {tone.title()} — that's me! What's up?"

    if food_mention:
        food_responses = {
            "goku": "Oh man, I LOVE food! Anything really — rice, meat, fish, you name it! After a good fight, nothing beats a huge meal!",
            "luffy": "Meat! I want MEAT! Shishishi! Nothing beats a good piece of meat after an adventure!",
            "sanji": "Ah, a fellow food lover! I'm the cook of the Straw Hat Pirates — let me make you something amazing!",
            "naruto": "Ichiraku Ramen is the best! Nothing beats a hot bowl of miso ramen with extra pork!",
        }
        for key, resp in food_responses.items():
            if key in name.lower():
                return resp
        return f"Food? I could go for something right now! I'm {name} from {anime} after all."

    if anime_mention:
        fight_responses = {
            "goku": "Fighting is what I do best! I love pushing my limits and finding stronger opponents. There's always someone out there who can surprise you!",
            "luffy": "I'm gonna be King of the Pirates! No one's gonna stop me — not the Marines, not the Yonko, nobody!",
            "levi": "Fighting isn't something I enjoy. But when it's necessary, I don't hesitate. That's what it means to be a soldier.",
            "gojo": "Hmm? You want to talk about fighting? I'm the strongest, so there's not much point. But I can teach you a thing or two.",
            "tanjiro": "I fight to protect my friends and family. Every demon I face — I try to understand their pain first. That's my way.",
            "naruto": "Believe it! I'll never give up, no matter how tough things get! That's my ninja way!",
        }
        for key, resp in fight_responses.items():
            if key in name.lower():
                return resp
        return f"In {anime}, battles are never simple. As {name}, I've learned that strength isn't just about power — it's about protecting what matters."

    if question:
        responses = [
            f"That's a good question. As {name}, I'd say it depends on the situation. In {anime}, we learned that answers aren't always simple.",
            f"Hmm, let me think... I'm {name}, so I'll give you my honest take. The world's more complicated than it looks, you know?",
            f"You're asking {name}? Well, I'll tell you what I know. Life in {anime} taught me a lot about that.",
        ]
        import random
        return random.choice(responses)

    # Default contextual response
    context_responses = [
        f"That's interesting! As {name}, I can relate to that. In {anime}, we dealt with a lot of different situations.",
        f"Heh, you sound like someone I'd get along with. I'm {name}, by the way — nice to chat with you!",
        f"I hear you! Being {name} from {anime} has taught me a lot about that kind of stuff.",
        f"Yeah, I get what you mean. The world of {anime} is full of surprises — and so are conversations like this!",
    ]
    import random
    return random.choice(context_responses)


# ---------------------------------------------------------------------------
# Cooldown helpers
# ---------------------------------------------------------------------------

def _parse_ts(ts_str):
    """Parse a SQLite CURRENT_TIMESTAMP string to a UTC datetime."""
    if not ts_str:
        return None
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            # ISO format fallback
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None


def _seconds_remaining(locked_at_str):
    """Seconds until the 24h cooldown expires. 0 = can switch."""
    dt = _parse_ts(locked_at_str)
    if dt is None:
        return 0
    # SQLite CURRENT_TIMESTAMP is UTC; make it timezone-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - dt).total_seconds()
    remaining = SWITCH_COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining))


def _fmt_remaining(seconds):
    """Format seconds as '14h 22m'."""
    if seconds <= 0:
        return "0m"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@bp.route("/senpai")
def senpai_page():
    """Picker + chat page. Requires login."""
    user = _user()
    if user is None:
        from flask import redirect, flash
        flash("Log in to chat with your senpai.", "error")
        return redirect(url_for("auth.login", next=request.path))

    quick_picks = _quick_pick_chars()
    return render_template(
        "senpai.html",
        quick_picks=quick_picks,
        genres=[],  # navbar expects this
    )


@bp.route("/senpai/status")
def senpai_status():
    """Return current senpai, lock time remaining, and conversation history."""
    user, err = _require_user()
    if err:
        return err

    chat = site_db.get_character_chat(user["id"])
    if not chat:
        return jsonify({
            "active": False,
            "remaining_seconds": 0,
            "remaining_label": "0m",
            "history": [],
            "character": None,
        })

    remaining = _seconds_remaining(chat.get("locked_at"))
    history = []
    try:
        history = json.loads(chat.get("conversation_history") or "[]")
    except (ValueError, TypeError):
        history = []

    char = _get_character_by_id(chat["character_id"])
    image = char.get("image") if char else None

    return jsonify({
        "active": True,
        "character": {
            "id": chat["character_id"],
            "name": chat["character_name"],
            "anime": chat["anime_name"],
            "image": image,
        },
        "remaining_seconds": remaining,
        "remaining_label": _fmt_remaining(remaining),
        "can_switch": remaining <= 0,
        "history": history,
    })


@bp.route("/senpai/choose", methods=["POST"])
def senpai_choose():
    """Lock in a character. Checks 24h cooldown. Generates+caches persona.
    Overwrites the old conversation entirely."""
    print(f"[senpai] choose called, LLM key={'SET' if LLM_API_KEY else 'EMPTY'}", flush=True)
    user, err = _require_user()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    character_id = data.get("character_id")
    if not character_id:
        return jsonify({"error": "Pick a character first."}), 400

    # --- Cooldown check ---
    existing = site_db.get_character_chat(user["id"])
    if existing:
        remaining = _seconds_remaining(existing.get("locked_at"))
        if remaining > 0 and str(existing.get("character_id")) != str(character_id):
            return jsonify({
                "error": "cooldown",
                "remaining_seconds": remaining,
                "remaining_label": _fmt_remaining(remaining),
                "message": f"You can switch again in {_fmt_remaining(remaining)}.",
            }), 403

    # --- Look up character ---
    character = _get_character_by_id(character_id)
    if not character:
        return jsonify({"error": "Character not found."}), 404

    # --- Get or generate persona (one-time per character) ---
    persona = _get_or_generate_persona(character)

    # --- Overwrite conversation ---
    opening = persona.get("opening_line") or "Hey!"
    history = [{"role": "assistant", "content": opening}]
    site_db.set_character_chat(
        user["id"],
        character_id,
        character.get("name") or "Character",
        character.get("title") or character.get("slug") or "Anime",
        json.dumps(history),
    )

    return jsonify({
        "success": True,
        "character": {
            "id": str(character_id),
            "name": character.get("name"),
            "anime": character.get("title"),
            "image": character.get("image"),
        },
        "opening_line": opening,
        "history": history,
        "remaining_seconds": SWITCH_COOLDOWN_SECONDS,
        "remaining_label": _fmt_remaining(SWITCH_COOLDOWN_SECONDS),
    })


@bp.route("/senpai/message", methods=["POST"])
def senpai_message():
    """Send a message and get an in-character AI reply."""
    user, err = _require_user()
    if err:
        return err

    chat = site_db.get_character_chat(user["id"])
    if not chat:
        return jsonify({"error": "Pick a senpai first."}), 400

    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Type a message first."}), 400
    if len(message) > 2000:
        return jsonify({"error": "Message too long (2000 char max)."}), 400

    # --- Build messages for Anthropic ---
    try:
        history = json.loads(chat.get("conversation_history") or "[]")
    except (ValueError, TypeError):
        history = []

    # Cap at last MAX_HISTORY messages
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    # Append the new user message
    history.append({"role": "user", "content": message})

    # Build the system prompt from the cached persona
    persona = site_db.get_persona(chat["character_id"])
    if not persona:
        # Persona was lost (DB wipe) — regenerate
        character = _get_character_by_id(chat["character_id"])
        if character:
            persona = _get_or_generate_persona(character)
        else:
            persona = _fallback_persona(
                chat["character_name"], chat["anime_name"],
                (character or {}).get("desc", ""), (character or {}).get("role", "")
            )
    system_prompt = _build_system_prompt(persona)

    # --- Call LLM or use template replies ---
    if not LLM_API_KEY:
        reply = _template_reply(persona, history)
    else:
        try:
            reply = _call_llm(system_prompt, history, max_tokens=400)
        except requests.exceptions.Timeout:
            reply = ("...sorry, I zoned out for a second. Can you say that again?")
        except Exception as exc:
            print(f"[senpai] chat API error: {exc}", flush=True)
            reply = _template_reply(persona, history)

    # Append the reply and persist
    history.append({"role": "assistant", "content": reply})

    # Cap again before saving
    if len(history) > MAX_HISTORY * 2:
        history = history[-(MAX_HISTORY * 2):]

    site_db.update_character_chat_history(
        user["id"], json.dumps(history)
    )

    return jsonify({
        "success": True,
        "reply": reply,
        "history": history,
    })


# ---------------------------------------------------------------------------
# Registration hook
# ---------------------------------------------------------------------------

def init_character_chat(app):
    """Call from app.py. Creates the senpai tables and registers the
    blueprint. Idempotent."""
    # Tables are created by database.create_tables(), but ensure they
    # exist here too in case of partial boot.
    site_db.create_tables()
    app.register_blueprint(bp)



