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
LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-oss-120b")
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
def _template_reply(history):
    """Generate a helpful reply without calling the API.
    Detects common question patterns and responds as Ota-chan."""
    user_msg = ""
    for m in reversed(history):
        if m.get("role") == "user":
            user_msg = m.get("content", "").lower()
            break

    greetings = any(w in user_msg for w in ["hello", "hi", "hey", "yo", "sup", "what's up", "how are you"])
    site_help = any(w in user_msg for w in ["how does", "how do i", "what is", "how to", "explain", "work", "feature", "use"])
    anime_q = any(w in user_msg for w in ["anime", "manga", "recommend", "best", "watch", "season", "episode", "character", "who is", "who would win", "fight", "power", "strongest"])
    xp_q = any(w in user_msg for w in ["xp", "rank", "level", "tier", "experience"])
    review_q = any(w in user_msg for w in ["review", "rating", "vote", "like", "dislike"])
    chat_q = any(w in user_msg for w in ["chat", "message", "community", "guild", "thread", "war"])
    greeting_q = any(w in user_msg for w in ["who are you", "tell me about yourself", "what can you do", "your name"])
    goodbye = any(w in user_msg for w in ["bye", "goodbye", "see you", "thanks", "thank you"])

    if greeting_q:
        return "I'm Ota-chan, Otakul's mascot and AI assistant! I can help you navigate the site, answer questions about anime, or just chat about your favorite shows. What's on your mind?"

    if greetings:
        return random.choice([
            "Hey there! What's up? Ask me anything about Otakul or anime!",
            "Hii! Welcome to Otakul! Need help with anything or just want to chat about anime?",
            "Yo! I'm here to help. What do you want to know?",
        ])

    if goodbye:
        return random.choice([
            "See you later! Have fun watching anime! ✨",
            "Bye! Come back anytime you need help!",
            "Take care! Happy anime-ing! 🎌",
        ])

    if xp_q:
        return "On Otakul, you earn XP by getting likes on your reviews, winning Reply Wars, and participating in the community. Your rank goes from F → D → C → B → A → S → S+. Reaching C rank (500 XP) unlocks dislikes, Reply Wars, and War Zone creation!"

    if review_q:
        return "Reviews use a 2-10 star rating scale with text comments. Likes from higher-ranked users give more XP. Dislikes require C rank (500 XP) — so fresh accounts can only like. You can also enter Reply Wars under any review to debate a stance!"

    if chat_q:
        return "Otakul has community chats on each anime's page, plus Guild threads for your group discussions. Guilds compete in War Zone battles — declare war, enter battlers, and the community votes on the best takes!"

    if site_help:
        return "Great question! Otakul has lots of features — browse anime by genre/rating/trending, write reviews to earn XP and climb ranks, join guilds, participate in War Zones, and track your anime lists. What specifically would you like to know more about?"

    if anime_q:
        return "Ooh, anime talk! I love it. Whether you want recommendations, character deep-dives, franchise trivia, or a heated 'who would win' debate — I'm your girl. What do you want to know?"

    # Default
    responses = [
        "That's a great question! I'm still learning, but I can help with Otakul features, anime recommendations, character info, and more. Could you tell me more about what you're looking for?",
        "Interesting! I can help with site navigation, anime knowledge, reviews, guilds, and War Zones. What specifically can I help with?",
        "Hmm, let me think... I'm best at answering questions about Otakul's features and anime in general. Want to ask me something specific?",
    ]
    return random.choice(responses)


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
