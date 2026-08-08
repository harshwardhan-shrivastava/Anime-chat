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

### Catalog data & adding Sub/Dub/Episodes/Arcs in parts
- The 13,994-title catalog lives in `anime_data.json`, loaded at import by
  `anime_data.py` (which is now just a tiny loader — a giant Python literal
  was OOM-killing the app in the low-memory container).
- `python3 scripts/enrich_details.py --details 5000` enriches the top 5,000
  titles (by popularity) that still lack Sub/Dub / episode lists / arcs, then
  rewrites `anime_data.json`. Re-running the same command continues with the
  *next* 5,000 — it's resumable and saves progress per API batch.
- `--upgrade` also deepens existing episode-title lists (slow for very long
  shows); `--skip-fetch` applies details from the existing AniList cache
  without hitting the API. Hand-curated entries are never overwritten.

### Real per-country streaming availability (US / Japan) from JustWatch
- `python3 scripts/enrich_streaming.py --offset N --count M --cache FILE` looks
  up each title on JustWatch (the same data Google shows in "Where to Watch"
  boxes, no API key needed) and saves its real providers for the **US** and
  **Japan**, including each service's audio (dub) and subtitle languages.
  Each run writes its own cache file (`anime_streaming_jw_*.json`), so you can
  run several offset windows in parallel; progress is saved every 25 titles so
  a killed/time-limited run just needs to be re-run to continue.
- `python3 scripts/enrich_streaming.py --apply` merges every
  `anime_streaming_jw_*.json` cache into `anime_data.json`.
- The anime detail page shows "Where to Watch": per-service 🇺🇸/🇯🇵 region
  badges, watch links, monetization (Streaming / Free / Rent / Buy) and
  Sub•Dub status derived from the service's real audio-language list.
  JustWatch rate-limits aggressively (~2k requests before a 403 block), so the
  full 14k-title catalog is meant to be enriched in chunks over time.

To let two people test it together on different devices, run the app on a
machine reachable from both (or deploy it somewhere), and have each person sign
up their own account and open the same `/community/<anime>` page.

### Per-episode thumbnails from TVmaze (no key needed)
- `scripts/enrich_ep_thumbnails.py` fills `episode["thumb"]` with a real
  16:9 thumbnail image (from TVmaze's public image CDN, `static.tvmaze.com`)
  for every episode that has one. The episode list on anime pages and the
  episode rating page show the thumbnail instead of the official poster when
  it exists.
- **No account, no API key, no subscription** — it just works. (IMDb blocks
  datacenter IPs; TheTVDB now requires a paid PIN; TMDB needs an account. TVmaze
  needs none of that.)
- Usage (same resumable pattern as the MAL grind):
  `python3 scripts/enrich_ep_thumbnails.py --plan`, then parallel
  `--fetch N --offset O --cache anime_ep_thumbs_wK.json` workers, then
  `--apply` to merge into `anime_data.json`.
- Coverage note: TVmaze has episode stills mostly for mainstream/popular
  anime; older/obscure titles often have none, and those episodes keep falling
  back to the official poster.
