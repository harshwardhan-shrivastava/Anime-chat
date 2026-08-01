# Anime-chat
Anime discussion platform built with Flask

## Setup for the new features (login, real chat, GIF search)

1. Install deps: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `SECRET_KEY` — any long random string (needed for login sessions).
   - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_FROM` — your real
     email account, so verification emails actually get sent. **Leave these blank
     to test right away** — signup still works, and the verification link is
     printed in the terminal, saved to `logs/emails.log`, and shown directly on
     the "check your email" screen.
   - `TENOR_API_KEY` — free key from https://tenor.com/gifapi/documentation, needed
     for the GIF picker to search Tenor's full library instead of erroring out.
3. Run `python app.py` and open http://127.0.0.1:5000

### How the pieces fit together
- `auth.py` — signup / login / logout / email verification (Flask session cookies,
  Werkzeug password hashing, itsdangerous signed tokens for the verify link).
- `services/mailer.py` — sends the verification email (or logs a dev link if SMTP
  isn't set up yet).
- `services/tenor.py` — proxies GIF search to Tenor so the API key never reaches
  the browser.
- `chat.py` — the real community chat backend: messages are saved to SQLite per
  anime community and per user, so two different logged-in accounts (e.g. you and
  your brother) both see the same conversation. The page polls for new messages
  every 2.5s and for the online member list every 8s.
- New tables in `animechat.db`: `users`, `chat_messages`, `chat_presence`.

To let two people test it together on different devices, run the app on a
machine reachable from both (or deploy it somewhere), and have each person sign
up their own account and open the same `/community/<anime>` page.
