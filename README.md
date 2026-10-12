# Anime-chat
Anime discussion platform built with Flask

## Setup for the new features (login, real chat, GIF search)

1. Install deps: `pip install -r requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `SECRET_KEY` — any long random string (needed for login sessions).
   - `GMAIL_TOKEN_JSON` — **the reliable way to send on Render**: paste the full
     `token.json` contents from `gmail_auth.py` (same as Project Tohoku). It
     sends via the Gmail API over HTTPS, which works everywhere. Render's
     network can't reach Gmail's SMTP servers, so plain SMTP alone will fail
     there with "[Errno 101] Network is unreachable".
   - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_FROM` — your real
     email account via Gmail SMTP (works on local dev and hosts that allow
     outbound SMTP; on Render it's only a fallback). **Leave everything blank
     to test right away** — signup still works, and the verification code is
     printed in the terminal, saved to `logs/emails.log`, and shown directly on
     the "check your email" screen.
   - `GIPHY_API_KEY` — free key from https://developers.giphy.com, needed for the
     GIF picker in the chat to search Giphy's library instead of erroring out.
3. Run `python app.py` and open http://127.0.0.1:5000

### How the pieces fit together
- `auth.py` — signup / login / logout / email verification (Flask session cookies,
  Werkzeug password hashing, itsdangerous signed tokens for the verify link).
- `services/mailer.py` — sends the verification email (or logs a dev link if SMTP
  isn't set up yet).
- `services/giphy.py` — proxies GIF search to Giphy so the API key never reaches
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

### Brand-new season anime are added automatically
- `.github/workflows/enrich.yml` runs the enrichment pipeline every 15 minutes.
  The first step, `scripts/season_topup.py`, makes sure BRAND-NEW season
  premieres get a card without anyone running `fetch_anime_catalog.py` by hand.
- It asks AniList for the previous + current + next season's
  airing/upcoming titles, inserts the ones the catalog is missing (poster,
  banner, synopsis, studio, genres, status, AniList id, and the start date so
  the card shows "EXP OCT 2026" right away) and then runs a
  bounded pass of the existing per-title enrichment for just those slugs:
  Sub/Dub + episode list, real US/JP streaming from JustWatch, and TVmaze/Kitsu
  episode thumbnails. The airing refresh in the same run then fills the
  countdown, released flags, real episode titles and HD thumbs.
- Dedupe is by AniList id first, then normalised title, so a new season of a
  show already in the catalog is never duplicated. Insertions are capped per
  run (`MAX_NEW`, `MAX_JUSTWATCH` in the module) so a backlog is filled over
  several ticks instead of hammering the APIs.
- It is cheap when there is nothing new (a few AniList season queries), and
  best-effort: a transient API failure never aborts the 15-minute run.

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

### Up Next — personal watch queue + reminder push notifications

`up_next.py` is a self-contained blueprint (registered in `app.py` the same way
as `threads.py` / `war_zone.py`) that answers "what am I watching?" and "what did
I mean to watch?".

- **Three states** in `user_up_next` (one row per user + anime): `now`
  (watching right now), `planned` (on the queue), `done` (finished). Marking a
  show `done` is what releases a reminder that was waiting on it.
- **Five reminder kinds**: `tonight`, `tomorrow`, a specific calendar `date`, a
  short `hours` timer, or `after` you finish another show you have queued. The
  timer is anything from 1 to 72 hours and is stored as an absolute `remind_at`
  (ISO 8601 in the site's clock) rather than a calendar day, so "in 3 hours"
  means three hours and not the next midnight; a running timer counts itself
  down on the card's pill ("IN 3H"). A reminder is *due* the first time anything
  asks (`resolve()` in `up_next.py`), never on a timer, and `notified_at`
  guarantees the push fires exactly once. Day boundaries use Asia/Kolkata, the
  same clock the episode countdown badges use.
- **Real Web Push.** `get_vapid()` generates a P-256 VAPID keypair on first use
  and stores it in `app_settings`, so signing needs no keys on disk and they
  survive redeploys. Each send has a 10 s timeout, and subscriptions the push
  service reports as 404/410 are deleted instead of retried forever.
- **The sweep needs traffic, because the host sleeps.** `start_reminder_thread()`
  is a daemon thread *inside* the web process. Render's free plan spins a web
  service down after 15 minutes without inbound traffic, so with nobody browsing
  there is no process, no sweep, and a due reminder never reaches a phone. The
  fix is an external tick: `POST /api/up-next/sweep` runs the same sweep over
  HTTP, and `.github/workflows/reminders.yml` calls it every 5 minutes — that
  request is also what wakes the app.
- **The sweep endpoint is token-gated.** Set `SWEEP_TOKEN` to any long random
  string on Render **and** as a repo secret of the same name; the endpoint sends
  it back in an `X-Sweep-Token` header (`?token=` also works). With the variable
  unset the endpoint answers 503, so a deploy that has not configured one never
  exposes an open push trigger. Cost note: a 5-minute tick keeps the free
  instance awake around the clock (~720 of the 750 free instance-hours a month);
  widen the cron in that workflow to trade notification latency for headroom.
- **A reminder is not consumed until a device can receive it.** The sweep only
  stamps `notified_at` once the user has a subscribed device. Stamping with zero
  subscriptions silently ate the reminder — you turned notifications on later and
  the thing you were waiting to be told about had already gone. With no device
  the row stays armed, so it fires exactly once the moment one subscribes (and
  `[up-next] reminder … is due but no device is subscribed` is logged).
- **UI**: status pill on every card (bottom-left of the poster), an Up Next
  button on anime pages, a queue rail on the homepage, a `/up-next` page with
  shelves (ready / watching now / tonight / later / waiting), all driven by
  `static/js/up_next.js` + `static/css/up_next.css`. The picker paints its rows
  before it asks the server for state, so it opens instantly and then corrects
  the tick marks when the response lands.
- **Reminders surface themselves — on every page, not just their own.** The
  pill reads READY, the show moves into the "Ready now" shelf, and a dock in the
  top-right corner shows it wherever the user happens to be. That dock is
  `templates/_up_next_alert.html`, included by `_footer.html` (and directly by
  `threads.html` / `otachan.html`, which have no footer include), so it renders
  on *every* page — a reminder that came due must not require opening the
  show's own page to discover. Two pieces:
  - a **bell that is page chrome, like the homepage's, everywhere**: a page
    leaves an empty `#unNavSlot` and the dock moves its button into that place.
    `_navbar.html` has one beside the profile pill (deliberately outside
    `.nav-right`, which phones hide inside the ☰ drawer), and the anime /
    episode / community headers and the user-history crumb bar have one too, so
    the bell is part of the header instead of a widget bolted over the corner.
    It appears as soon as a reminder is armed or due, dressed as the burger's
    twin (its size and chrome, the accent on hover) by `.un-bell.in-nav`, and it
    stays put while the box is open, because it is what opens the box. The one
    exception is **threads**, which already has its own bell and dropdown in its
    own top bar: `_up_next_alert.html` is deliberately not included there, since
    two bells in one header is exactly the inconsistency this dock exists to
    avoid. The icon is inline SVG, so a blocked icon font can never leave an
    empty square where the bell should be;
  - a **badge that counts what is *unread***, not everything that is due.
    Seeing the box is seeing the notifications, so opening it — or ✕/Escape, or
    the ✓ — marks what it showed as seen (`sessionStorage`, so it survives
    navigation within the tab) and the number clears instead of claiming
    "1 unread" forever. A quiet violet dot still means "only timers ticking", so
    the bell never claims something is ready when it is just counting down;
  - a **wide, low notification box** that hangs from the top-right, just under
    the navbar — its top is measured from the navbar's bottom edge rather than
    guessed, because the navbar wraps onto a second row between 820px and
    1150px. One row per reminder, wide and short: the show's square thumbnail,
    the show's name, a line of why ("ready — came due 2h ago", "tonight",
    "waiting for you to finish another show") and the reminder's state on the
    right (`READY`, or the running `IN 3H` pill). Every row is a link straight
    to that anime, and *Open queue* goes to `/up-next`.
  The box opens by itself only for a reminder that **just went off** (inside
  `FRESH_SECONDS`), and only on a page that does not already render the same
  thing as page furniture (the `/up-next` page's own "while you were away"
  card; the bell still works there, it just waits to be asked). A stale reminder
  popping a box open over the page is what reads as being nagged — the bell and
  its unread count already say something is waiting, and one click shows the
  whole list. No page keeps a *second*, lesser reminder box for it: the homepage
  used to show a "1 reminder is waiting" strip and then the box landed on top
  of it, which read as two notifications for one reminder — the box is the
  homepage's reminder surface now. Once it has opened by itself it stays put: it
  is the notification, not a menu, so clicking the page under it does not take
  the row away (that is what made a row "vanish" the moment the user looked at
  anything). ✕ or Escape retires it — the reminder itself stays pending on the
  queue — and every row also carries a ✓ whose tooltip is the whole point:
  "Got it, I'll watch it". It reads
  `GET /api/up-next/alerts`, which answers with what is ready plus what is
  coming up (so a timer you *set* is visible on the site, not only a missed one)
  and 401 when signed out, whereupon the dock stays quiet rather than bouncing a
  visitor to `/login` for opening a public page. The ready list runs
  **most-recently-due first**, so the row on top is the reminder that just fired,
  and it is long enough to hold everything a queue realistically carries at once
  — an oldest-first list truncated at three hid exactly the one the user was
  looking for. The one that came due in the last ten minutes is highlighted
  (`fresh`). It re-polls every 60s while the tab is visible and on
  `visibilitychange`, so a timer that runs out mid-browse pops on its own, and
  a reminder already seen this session is not announced again. When the
  device has no push subscription the box also offers **Turn on notifications**
  — a due reminder with nothing subscribed is the most common reason someone
  sees no notification at all, and the check costs no request (the browser's
  push manager answers it locally). `due_moment()` derives that age per
  kind — an `hours` timer from `remind_at`, the dated kinds from `remind_on` (at
  17:00 for `tonight`, midnight otherwise), and `after` from the sweep's
  `notified_at`, which SQLite writes in UTC and therefore has to be converted to
  the site clock or every label would be 5:30 out.
- **"Got it, I'll watch it" is a real action, not a dismiss.** The ✓ on a row
  posts to `POST /api/up-next/ack`, which calls `ack_up_next_reminder()`: the
  `remind_*` columns are cleared, so the timer goes off, the row leaves the box
  and the badge, and the sweep can never fire it again — while the row keeps its
  status and stays on the queue, so `/up-next` still lists the show for anyone
  who missed the notification. That is the difference between "stop telling me"
  and "I watched it": acknowledging never deletes a show the user still means to
  watch. The same endpoint accepts `{"slugs": [...]}` for a batch.
- Requires the `pywebpush` dependency (pinned in `requirements.txt`). If it is
  missing the feature degrades quietly — the pickers and queue still work and
  the push endpoints report `push_unavailable` instead of erroring. iOS needs the
  site added to the Home Screen before `PushManager` exists.
