"""Up Next — Otakul's personal watch queue.

One row per (user, anime) that answers "what am I watching?" and "what did I
mean to watch?". Three states plus four reminder kinds:

    status      now       I'm watching this right now
                planned   it's on my queue
                done      finished — this is what releases an
                          ``after`` reminder that was waiting on this show

    remind      tonight   ping me this evening
                tomorrow  ping me tomorrow
                date      ping me on a specific calendar day
                hours     ping me in N hours (an exact timer, not a day)
                after     ping me once I finish <another show>

Design notes
------------
* Reminders are *lazy*: nothing needs a cron. A reminder is "due" the first
  time anything asks (a page render, the queue API, or the sweep thread
  below). ``notified_at`` makes the push fire exactly once.
* Everything is evaluated in the site's timezone (Asia/Kolkata — the same
  clock ``_episode_badge`` in app.py uses), so a reminder set for "tomorrow"
  flips over at local midnight, not UTC.
* Web Push keys are generated on first use and kept in the database, so
  there is nothing to configure. ``pywebpush`` is imported defensively —
  if the package is missing the rest of the site keeps working and the push
  endpoint reports that push is unavailable instead of erroring.

Kept as a blueprint so app.py can register it from one reachable spot.
"""
import base64
import hmac
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, g, jsonify, render_template, request
from cryptography.hazmat.primitives import serialization

from anime_data import anime_database
from database import (
    REMIND_KINDS,
    UP_NEXT_DONE,
    UP_NEXT_NOW,
    UP_NEXT_PLANNED,
    UP_NEXT_STATUSES,
    ack_up_next_reminder,
    clear_up_next,
    delete_push_subscription,
    get_done_slugs,
    get_pending_reminders,
    get_push_subscriptions,
    get_setting,
    get_up_next_map,
    mark_up_next_notified,
    save_push_subscription,
    set_setting,
    set_up_next,
    touch_push_subscription,
)

up_next_bp = Blueprint("up_next", __name__)

# The site speaks one clock: Asia/Kolkata (UTC+5:30). Cards already label
# episodes "EP 5 TODAY" against this timezone, so reminders must agree with
# it or a show and its reminder would disagree about what day it is.
SITE_TZ = timezone(timedelta(hours=5, minutes=30))

# "Tonight" only means tonight after this local hour; before that a tonight
# reminder is armed but not yet due (so it does not ping anyone at 9am).
TONIGHT_HOUR = 17
REMINDER_SWEEP_SECONDS = 300

# Shared secret for the external sweep trigger (see api_sweep). Unset means
# the endpoint stays switched off rather than open to the world.
SWEEP_TOKEN_ENV = "SWEEP_TOKEN"

# Upper bound for an 'hours' timer. Anything longer is a calendar plan, not a
# timer, so 'date' is the right tool and the picker never offers more.
MAX_REMIND_HOURS = 72

# A reminder that came due inside this window is flagged as 'fresh' for the
# site-wide dock, which draws attention to the one that just went off.
FRESH_SECONDS = 600

# How the pill on a card is rendered per state.
STATUS_PILL = {
    UP_NEXT_NOW: {"label": "NOW", "icon": "fa-play", "cls": "un-now"},
    UP_NEXT_PLANNED: {"label": "PLANNED", "icon": "fa-bookmark", "cls": "un-planned"},
    UP_NEXT_DONE: {"label": "DONE", "icon": "fa-check", "cls": "un-done"},
}

REMIND_LABEL = {
    "tonight": "Tonight",
    "tomorrow": "Tomorrow",
    "date": "Pick a day",
    "hours": "In a few hours",
    "after": "After I finish…",
}


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(SITE_TZ)


def _today():
    return _now().date()


def parse_date(value):
    """'YYYY-MM-DD' -> date, or None. Never raises on user input."""
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def parse_datetime(value):
    """ISO 8601 -> aware datetime in the site clock, or None.

    'hours' reminders store a full timestamp rather than a calendar day, so
    they need to survive the round trip through SQLite text. Never raises on
    user input; a naive value is assumed to be site-local.
    """
    try:
        dt = datetime.fromisoformat((value or "").strip())
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=SITE_TZ)


def _short_countdown(left):
    """Pill label for a running timer.

    Rounds to the nearest minute and then the nearest hour. Flooring the
    seconds was subtly wrong: a timer stored exactly one hour out renders a
    fraction of a second later, so it would read "IN 59M" instead of "IN 1H".
    """
    minutes = max(1, int(round(max(0.0, left.total_seconds()) / 60)))
    if minutes < 60:
        return f"IN {minutes}M"
    return f"IN {max(1, int(round(minutes / 60)))}H"


def _parse_utc_stamp(value):
    """SQLite CURRENT_TIMESTAMP -> aware datetime in the site clock.

    CURRENT_TIMESTAMP is UTC and carries no offset, so it must be tagged UTC
    *before* converting. Reading it as local time would put every "came due
    4h ago" label out by the 5:30 this site is offset from UTC.
    """
    try:
        naive = datetime.strptime((value or "").strip(), "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return naive.replace(tzinfo=timezone.utc).astimezone(SITE_TZ)


def due_moment(row):
    """When this reminder actually came due, in the site clock, or None.

    Each kind stores its timing differently, so "how long has this been
    waiting?" has to be derived per kind rather than read off one column.
    """
    kind = (row.get("remind_kind") or "").strip()
    if kind == "hours":
        return parse_datetime(row.get("remind_at"))
    on = parse_date(row.get("remind_on"))
    if on is not None and kind in ("tonight", "tomorrow", "date"):
        # 'tonight' only starts at TONIGHT_HOUR; the date-based kinds are due
        # from the start of their day.
        hour = TONIGHT_HOUR if kind == "tonight" else 0
        return datetime(on.year, on.month, on.day, hour, 0, tzinfo=SITE_TZ)
    # 'after' stores no moment of its own — it becomes due when its blocker is
    # finished. The sweep stamps notified_at within one sweep of that, so it is
    # the best available proxy rather than a guess.
    return _parse_utc_stamp(row.get("notified_at"))


def _ago_label(moment):
    """Human age of a past moment: '4h ago', '2 days ago', or '' if unknown."""
    if moment is None:
        return ""
    seconds = (_now() - moment).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours}h ago"
    days = int(hours // 24)
    return "1 day ago" if days == 1 else f"{days} days ago"


def waiting_summary(cards):
    """(count, longest wait) across due reminders — the "while you were
    away" summary. The count says something is waiting; the age is what tells
    the user it slipped past them."""
    stamps = [
        c["up_next"].get("due_since")
        for c in (cards or [])
        if (c.get("up_next") or {}).get("due")
    ]
    if not stamps:
        return 0, ""
    known = [s for s in stamps if s]
    if not known:
        return len(stamps), ""
    return len(stamps), _ago_label(datetime.fromtimestamp(min(known), SITE_TZ))


def _friendly_countdown(left):
    """Human caption for a running timer, in the pill's voice."""
    minutes = int(left.total_seconds() // 60)
    if minutes < 1:
        return "any second now"
    if minutes < 60:
        return f"in {minutes} min"
    hours = minutes / 60
    if hours < 1.5:
        return "in about an hour"
    return f"in about {int(round(hours))} hours"


def _friendly_day(target, today):
    """Short human label for a due date, in the pill's voice."""
    delta = (target - today).days
    if delta <= 0:
        return "TODAY"
    if delta == 1:
        return "TOMORROW"
    if delta < 7:
        return f"IN {delta}D"
    return target.strftime("%b %d").upper()


# ---------------------------------------------------------------------------
# Resolving a queue row into something a template can render
# ---------------------------------------------------------------------------

def resolve(row, done_slugs=None, today=None):
    """Turn a stored queue row into display state.

    Returns a dict with the pill, whether the reminder is due, and a short
    reminder caption. Pure function of the row + the user's finished set, so
    it is cheap enough to run for every card on a page.
    """
    done_slugs = done_slugs or set()
    today = today or _today()
    status = row.get("status") or UP_NEXT_PLANNED
    kind = (row.get("remind_kind") or "").strip()
    on = parse_date(row.get("remind_on"))

    due = False
    caption = ""
    countdown = None

    if status != UP_NEXT_DONE and kind:
        if kind == "after":
            blocker = (row.get("remind_after_slug") or "").strip()
            if blocker and blocker in done_slugs:
                due = True
                caption = "ready — you finished the show before it"
            else:
                caption = "waiting for you to finish another show" if blocker else ""
        elif kind == "hours":
            at = parse_datetime(row.get("remind_at"))
            if at is not None:
                left = at - _now()
                if left.total_seconds() <= 0:
                    due = True
                    caption = "your timer just ran out"
                else:
                    caption = _friendly_countdown(left)
                    countdown = _short_countdown(left)
        elif on:
            if today > on:
                due = True
                caption = "from an earlier day"
            elif today == on:
                if kind == "tonight":
                    if _now().hour >= TONIGHT_HOUR:
                        due = True
                        caption = "tonight"
                    else:
                        caption = "later tonight"
                else:
                    due = True
                    caption = "today"
            else:
                caption = _friendly_day(on, today).lower()

    pill = dict(STATUS_PILL.get(status) or STATUS_PILL[UP_NEXT_PLANNED])
    if due:
        # A due reminder outranks the plain status: this is the thing the
        # user asked to be told about.
        pill = {"label": "READY", "icon": "fa-bell", "cls": "un-ready"}
    elif kind == "after":
        # Waiting on another show — deliberately quiet, it is not calling
        # for you yet.
        pill = {"label": "WAITING", "icon": "fa-hourglass-half", "cls": "un-waiting"}
    elif status == UP_NEXT_PLANNED and countdown:
        # A running timer counts itself down on the poster instead of showing
        # the generic PLANNED label.
        pill = {"label": countdown, "icon": "fa-clock", "cls": "un-planned"}
    elif status == UP_NEXT_PLANNED and on:
        pill["label"] = _friendly_day(on, today)

    # A due reminder carries its own age, which is what lets the site say
    # "this came due 4h ago" when the user comes back to it.
    moment = due_moment(row) if due else None

    return {
        "slug": row.get("anime_slug"),
        "status": status,
        "remind_kind": kind or None,
        "remind_on": on.isoformat() if on else None,
        "remind_after_slug": (row.get("remind_after_slug") or "").strip() or None,
        # Exposed so the picker can prefill its timer stepper with whatever is
        # left of a running countdown.
        "remind_at": row.get("remind_at"),
        "due": due,
        "caption": caption,
        "pill": pill,
        "due_since": int(moment.timestamp()) if moment else None,
        "ago": _ago_label(moment),
    }


def _ensure_queue_shape(qmap, done_slugs=None, today=None):
    """Resolve a whole slug -> row map in one pass (one `today` for all)."""
    done_slugs = done_slugs if done_slugs is not None else set()
    today = today or _today()
    return {
        slug: resolve(row, done_slugs, today)
        for slug, row in (qmap or {}).items()
    }


def decorate_card(entry, qmap, done_slugs=None, today=None):
    """Attach the Up Next pill to one card dict (used by _anime_card.html).

    Mutates and returns `entry` so it can be called inline while building a
    list of card dicts. Cards outside the queue are untouched, so a page with
    no queue costs nothing.
    """
    row = (qmap or {}).get(entry.get("slug"))
    if not row:
        return entry
    entry["up_next"] = resolve(row, done_slugs, today)
    return entry


# ---------------------------------------------------------------------------
# Card shaping (matches the pick dicts _anime_card.html expects)
# ---------------------------------------------------------------------------

def _card(slug):
    e = anime_database.get(slug)
    if e is None:
        return None
    return {
        "slug": slug,
        "title": e.get("title") or slug,
        "image": e.get("image") or "",
        "rating": e.get("rating") or "N/A",
        "live_rating": e.get("rating") or "N/A",
        "year": e.get("release") or "",
        "genre": e.get("genre") or "",
        "total_episodes": e.get("total_episodes", 0) or 0,
        "member_count": e.get("member_count", 0) or 0,
        "has_sub": bool(e.get("subtitles")),
        "has_dub": any(
            str(d).strip().lower() == "english" for d in (e.get("dub") or [])
        ),
        "arc_count": len(e.get("watch_order") or []) or len(e.get("seasons") or []),
        "next_episode_at": e.get("next_episode_at"),
    }


def attach_queue_state(cards, user_id, qmap=None):
    """Merge Up Next pills into a list of card dicts, in place.

    Called by the page routes (home, browse, category) after pagination, so
    only the cards actually being rendered are resolved, and the whole queue
    is fetched in one query rather than one per card. A missing user or an
    empty queue is a no-op, so signed-out pages are unaffected.
    """
    if not user_id or not cards:
        return cards
    if qmap is None:
        qmap = get_up_next_map(user_id)
    if not qmap:
        return cards
    done = {s for s, r in qmap.items() if r.get("status") == UP_NEXT_DONE}
    today = _today()
    for card in cards:
        decorate_card(card, qmap, done, today)
    return cards


def queued_cards(user_id, limit=24, qmap=None):
    """The user's queue as card dicts, most-actionable first.

    Ordering is the whole point of the rail: due reminders, then the show
    you're watching right now, then dated reminders soonest-first, then the
    rest of the queue. Anything already marked done is left out.

    Pass `qmap` when the caller already loaded the queue (the homepage needs
    it for both the rail and the card pills) — the remote DB is hit over
    HTTP, so a redundant fetch is a real round trip.
    """
    if qmap is None:
        qmap = get_up_next_map(user_id)
    if not qmap:
        return [], {}
    done_slugs = {s for s, r in qmap.items() if r.get("status") == UP_NEXT_DONE}
    today = _today()
    resolved = _ensure_queue_shape(qmap, done_slugs, today)

    def rank(item):
        r = item[1]
        if r["status"] == UP_NEXT_DONE:
            return (9, 0)
        if r["due"]:
            return (0, 0)
        if r["status"] == UP_NEXT_NOW:
            return (1, 0)
        d = parse_date(r["remind_on"])
        if d:
            return (2, (d - today).days)
        return (3, 0)

    ordered = sorted(
        ((slug, r) for slug, r in resolved.items()
         if r["status"] != UP_NEXT_DONE),
        key=rank,
    )

    # Snooze-before/after ordering within the same bucket: most recent
    # additions first, so a big queue still surfaces fresh intent.
    order = [slug for slug, _ in ordered]
    cards = []
    for slug in order[:limit]:
        card = _card(slug)
        if card is None:
            continue
        card["up_next"] = resolved[slug]
        cards.append(card)
    return cards, resolved


def _reminder_sort_key(state):
    """Soonest-first key for a reminder that is not due yet.

    A running timer is ranked by its real moment, a dated one by the day it
    lands on (its 17:00 for `tonight`, because that is when it becomes due),
    and an `after` reminder has no moment of its own — it sorts last rather
    than pretending to be imminent.
    """
    if state.get("remind_kind") == "hours":
        at = parse_datetime(state.get("remind_at"))
        if at is not None:
            return (0, at.timestamp())
    on = parse_date(state.get("remind_on"))
    if on is not None:
        return (0, datetime(on.year, on.month, on.day, tzinfo=SITE_TZ).timestamp())
    return (1, 0)


def _alert_items(pairs, now_ts=None):
    """Trim resolved rows down to the few fields the dock renders.

    ``fresh`` marks a reminder that came due in the last ten minutes — or that
    has no moment of its own, which only happens when the show it waited on was
    finished moments ago — so the box can point at the one that just went off.
    """
    out = []
    for slug, state in pairs:
        entry = anime_database.get(slug) or {}
        due_since = state.get("due_since")
        out.append({
            "slug": slug,
            "title": entry.get("title") or slug,
            "image": entry.get("image") or "",
            "caption": state.get("caption") or "",
            "ago": state.get("ago") or "",
            # When this reminder came due, in epoch seconds (None when the
            # kind has no moment of its own). The dock keys "already shown"
            # and "too old to still be nagging" off this, so a reminder can
            # be identified rather than a show — re-reminding the same show
            # later is a new notification, not the old one coming back.
            "since": due_since,
            "pill": state.get("pill") or {},
            "kind": state.get("remind_kind"),
            "fresh": bool(
                state.get("due")
                and (due_since is None or now_ts is None
                     or now_ts - due_since <= FRESH_SECONDS)
            ),
        })
    return out


def alert_state(user_id, due_limit=8, upcoming_limit=4):
    """(due_total, due_items, upcoming_total, upcoming_items) for the dock.

    One pass over the user's rows, because the dock polls this from every page.
    ``due`` is what is calling for the user right now; ``upcoming`` is every
    other armed reminder, soonest first — that is what makes a timer *set*
    visible on the site, not just a missed one.

    Deliberately cheaper than ``queued_cards``: it resolves the rows and keeps
    only reminders, instead of building a hundred card dicts to discard them.

    The due list runs most-recent-first and is long enough to hold everything a
    user realistically parks on the queue at once (the limit only bites on a
    queue nobody visits for weeks), because the reminder someone is looking for
    is the one that just fired — an oldest-first list truncated at three hid
    exactly that one.
    """
    qmap = get_up_next_map(user_id)
    if not qmap:
        return 0, [], 0, []
    done = {s for s, r in qmap.items() if r.get("status") == UP_NEXT_DONE}
    today = _today()
    due = []
    upcoming = []
    for slug, row in qmap.items():
        state = resolve(row, done, today)
        if state["due"]:
            due.append((slug, state))
        elif state["status"] != UP_NEXT_DONE and state["remind_kind"]:
            upcoming.append((slug, state))

    now_ts = _now().timestamp()
    # Most recent first: the top row is the reminder that just went off. A due
    # reminder with no moment of its own counts as just-now rather than last —
    # the only way an 'after' reminder comes due is its blocker being finished,
    # which is a fresh event from the user's side. (A plain backlog row with no
    # reminder is not a reminder at all, so it never appears here.)
    due.sort(key=lambda pair: pair[1].get("due_since") or now_ts, reverse=True)
    upcoming.sort(key=lambda pair: _reminder_sort_key(pair[1]))
    return (
        len(due), _alert_items(due[:due_limit], now_ts),
        len(upcoming), _alert_items(upcoming[:upcoming_limit], now_ts),
    )


# ---------------------------------------------------------------------------
# VAPID keys — generated once, stored in the DB, shared by every worker
# ---------------------------------------------------------------------------

_VAPID_LOCK = threading.Lock()
_vapid_cache = {}


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def get_vapid():
    """(private_key_b64url, public_key_b64url), generating on first use.

    The private key is the raw 32-byte P-256 scalar, base64url-encoded — the
    exact string ``py_vapid.Vapid.from_string`` accepts, so signing needs no
    PEM files on disk and the key survives redeploys in the database.
    """
    if _vapid_cache:
        return _vapid_cache["priv"], _vapid_cache["pub"]

    with _VAPID_LOCK:
        if _vapid_cache:
            return _vapid_cache["priv"], _vapid_cache["pub"]

        priv = get_setting("vapid_private_key")
        pub = get_setting("vapid_public_key")
        if not priv or not pub:
            from py_vapid import Vapid

            key = Vapid()
            key.generate_keys()
            priv = _b64u(
                key.private_key.private_numbers().private_value.to_bytes(32, "big")
            )
            pub = _b64u(
                key.public_key.public_bytes(
                    serialization.Encoding.X962,
                    serialization.PublicFormat.UncompressedPoint,
                )
            )
            set_setting("vapid_private_key", priv)
            set_setting("vapid_public_key", pub)
            print("[up-next] generated a new VAPID keypair for web push",
                  flush=True)

        _vapid_cache["priv"] = priv
        _vapid_cache["pub"] = pub
        return priv, pub


def push_available():
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def send_push(user_id, payload, subs=None):
    """Push `payload` to every device this user subscribed.

    Returns (sent, failed). Dead subscriptions (the push service answers
    404/410 — the browser was uninstalled or the subscription expired) are
    deleted so they stop being retried forever.

    `subs` lets the caller hand in a subscription list it already read, so a
    sweep does not pay for the same query twice.
    """
    if subs is None:
        subs = get_push_subscriptions(user_id)
    if not subs:
        return 0, 0
    if not push_available():
        print("[up-next] pywebpush not installed — skipping push", flush=True)
        return 0, 0

    from pywebpush import webpush, WebPushException

    priv, _ = get_vapid()
    body = json.dumps(payload)
    sent = failed = 0
    for sub in subs:
        info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                info,
                body,
                vapid_private_key=priv,
                # The "sub" claim must be a mailto: or https: URL. Using the
                # site origin keeps it valid without extra configuration.
                vapid_claims={"sub": "mailto:hello@otakul.co"},
                ttl=86400,
                # pywebpush defaults to NO timeout, so one unresponsive push
                # service would wedge the reminder sweep thread forever.
                timeout=10,
            )
            touch_push_subscription(sub["endpoint"])
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                delete_push_subscription(sub["endpoint"])
            failed += 1
        except Exception:
            failed += 1
    return sent, failed


# ---------------------------------------------------------------------------
# The reminder sweep
# ---------------------------------------------------------------------------

def _reminder_body(row, entry, due_state):
    """Notification copy. Short, specific, and in the site's voice."""
    title = (entry or {}).get("title") or row["anime_slug"]
    kind = row.get("remind_kind")
    if kind == "after":
        why = "you finished the show before it"
    elif kind == "hours":
        why = "your timer's up"
    elif due_state.get("caption") == "from an earlier day":
        why = "you planned this earlier"
    elif kind == "tonight":
        why = "it's tonight"
    else:
        why = "it's queued for today"
    return {
        "title": "Up Next",
        "body": f"▶ {title} — {why}. Ready when you are.",
        "url": f"/anime/{row['anime_slug']}",
        "tag": f"up-next-{row['anime_slug']}",
        "icon": "/static/icons/icon-192.png",
        "badge": "/static/icons/icon-192.png",
    }


def due_reminders():
    """Every armed reminder that is due right now, with display state.

    Reads the small pending-reminder slice once and decides due-ness in
    Python, because ``after`` reminders depend on a different row's status.
    """
    pending = get_pending_reminders()
    if not pending:
        return []

    done_by_user = {}
    today = _today()
    out = []
    for row in pending:
        uid = row["user_id"]
        if uid not in done_by_user:
            done_by_user[uid] = get_done_slugs(uid)
        state = resolve(row, done_by_user[uid], today)
        if state["due"]:
            out.append((row, state))
    return out


def run_reminder_sweep(log=True):
    """Send the push for every due reminder. Returns the number sent.

    A reminder is only stamped as notified once the user actually has a
    device to notify. Stamping it with zero subscriptions would consume the
    reminder silently: the user turns notifications on afterwards and the
    thing they were waiting to be told about is already gone. With no
    subscription the row simply stays armed, and the next sweep after a
    device subscribes fires it exactly once.
    """
    due = due_reminders()
    if not due:
        return 0
    sent_total = 0
    for row, state in due:
        subs = get_push_subscriptions(row["user_id"])
        if not subs:
            # Nothing to push to yet — leave it armed and say so, because
            # "reminder is due but no device is subscribed" is the single
            # most common reason a user sees no notification at all.
            if log:
                print(
                    f"[up-next] reminder {row['user_id']}/{row['anime_slug']} "
                    f"({row.get('remind_kind')}) is due but no device is "
                    f"subscribed — leaving it armed",
                    flush=True,
                )
            continue
        entry = anime_database.get(row["anime_slug"]) or {}
        payload = _reminder_body(row, entry, state)
        sent, _failed = send_push(row["user_id"], payload, subs=subs)
        # Stamped once *a device was notified* — a send that fails still
        # counts, so a dead push service is not retried every five minutes.
        mark_up_next_notified(row["user_id"], row["anime_slug"])
        sent_total += sent
        if log:
            print(
                f"[up-next] reminder {row['user_id']}/{row['anime_slug']} "
                f"({row.get('remind_kind')}) -> {sent} device(s)",
                flush=True,
            )
    return sent_total


def _reminder_loop():
    """Background thread: keep pushing reminders even with no page traffic.

    Runs inside the long-lived Flask process (Render runs `python app.py`),
    so this works without any external cron. Cheap: one small SELECT and a
    handful of HTTP posts every five minutes.
    """
    # Let the app finish booting first.
    time.sleep(20)
    while True:
        try:
            run_reminder_sweep()
        except Exception as exc:
            print(f"[up-next] reminder sweep failed: {exc}", flush=True)
        time.sleep(REMINDER_SWEEP_SECONDS)


def start_reminder_thread():
    threading.Thread(target=_reminder_loop, daemon=True).start()


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _require_user_json():
    user = g.get("user")
    if user is None:
        return None, (jsonify({"success": False, "error": "login"}), 401)
    return user, None


def _clean_slug(raw):
    slug = (raw or "").strip()
    if not slug or anime_database.get(slug) is None:
        return None
    return slug


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@up_next_bp.route("/api/up-next", methods=["GET"])
def api_queue():
    """The whole queue, already resolved for display."""
    user, err = _require_user_json()
    if err:
        return err
    cards, resolved = queued_cards(user["id"])
    return jsonify({
        "success": True,
        "items": [{"slug": s, **r} for s, r in resolved.items()],
        "cards": [
            {
                "slug": c["slug"],
                "title": c["title"],
                "image": c["image"],
                "up_next": c["up_next"],
            }
            for c in cards
        ],
        "push": push_available(),
    })


@up_next_bp.route("/api/up-next/alerts", methods=["GET"])
def api_alerts():
    """What the site-wide dock renders: what is ready, and what is coming.

    The dock lives in the shared footer, so this runs from every page. Signed
    out it answers 401 and the dock stays silent: a reminder that came due
    must not require opening the show's own page to find out, but it also
    must never bounce a visitor to /login for opening a public page.
    """
    user, err = _require_user_json()
    if err:
        return err
    count, items, upcoming_count, upcoming = alert_state(user["id"])
    return jsonify({
        "success": True,
        "count": count,
        "items": items,
        "upcoming_count": upcoming_count,
        "upcoming": upcoming,
    })


@up_next_bp.route("/api/up-next/state", methods=["GET"])
def api_state():
    """State for one anime — what the anime page's button renders."""
    user, err = _require_user_json()
    if err:
        return err
    slug = _clean_slug(request.args.get("slug"))
    if slug is None:
        return jsonify({"success": False, "error": "slug"}), 400
    qmap = get_up_next_map(user["id"])
    done = {s for s, r in qmap.items() if r.get("status") == UP_NEXT_DONE}
    row = qmap.get(slug)
    return jsonify({
        "success": True,
        "slug": slug,
        "state": resolve(row, done) if row else None,
        "push": push_available(),
        "remind_kinds": list(REMIND_KINDS),
    })


@up_next_bp.route("/api/up-next/set", methods=["POST"])
def api_set():
    """Add / update the anime's queue entry.

    Body (JSON): {slug, status, remind_kind?, remind_on?, remind_after?}
    ``remind_on`` accepts 'tomorrow' as a convenience or a YYYY-MM-DD date;
    ``remind_after`` must be another slug already on the user's queue.
    """
    user, err = _require_user_json()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    slug = _clean_slug(data.get("slug"))
    if slug is None:
        return jsonify({"success": False, "error": "slug"}), 400

    status = (data.get("status") or UP_NEXT_PLANNED).strip().lower()
    if status not in UP_NEXT_STATUSES:
        return jsonify({"success": False, "error": "status"}), 400

    kind = (data.get("remind_kind") or "").strip().lower()
    if kind not in REMIND_KINDS:
        kind = None

    today = _today()
    remind_on = None
    remind_after = None
    remind_at = None

    if kind == "tonight":
        remind_on = today.isoformat()
    elif kind == "tomorrow":
        remind_on = (today + timedelta(days=1)).isoformat()
    elif kind == "date":
        raw = (data.get("remind_on") or "").strip()
        if raw.lower() == "tomorrow":
            remind_on = (today + timedelta(days=1)).isoformat()
        else:
            chosen = parse_date(raw)
            if chosen is None:
                return jsonify({"success": False, "error": "remind_on"}), 400
            # A reminder in the past would push instantly and read as a bug.
            if chosen < today:
                chosen = today
            remind_on = chosen.isoformat()
    elif kind == "hours":
        try:
            hours = int(data.get("remind_hours") or 0)
        except (TypeError, ValueError):
            hours = 0
        if hours < 1:
            return jsonify({"success": False, "error": "remind_hours"}), 400
        hours = min(hours, MAX_REMIND_HOURS)
        # An absolute moment, so the timer keeps ticking across restarts and
        # does not drift onto a day boundary the way a date would.
        remind_at = (_now() + timedelta(hours=hours)).isoformat()
    elif kind == "after":
        after = _clean_slug(data.get("remind_after"))
        if after is None or after == slug:
            return jsonify({"success": False, "error": "remind_after"}), 400
        qmap = get_up_next_map(user["id"])
        if after not in qmap:
            return jsonify({"success": False, "error": "remind_after_not_queued"}), 400
        remind_after = after

    # Marking something done must not leave a dangling reminder armed.
    if status == UP_NEXT_DONE:
        kind = None
        remind_on = None
        remind_after = None
        remind_at = None

    row = set_up_next(
        user["id"], slug, status, kind, remind_on, remind_after,
        remind_at=remind_at,
    )
    done = get_done_slugs(user["id"])
    return jsonify({
        "success": True,
        "state": resolve(row, done),
        # Chains that were waiting on this show may now be due.
        "released": [s for s in _released_after(user["id"], slug)],
    })


def _released_after(user_id, finished_slug):
    """Slugs whose ``after`` reminder just became due because we finished
    `finished_slug` — used to tell the client to refresh those cards."""
    rows = get_pending_reminders()
    changed = []
    for row in rows:
        if (row["user_id"] == user_id
                and row.get("remind_after_slug") == finished_slug):
            changed.append(row["anime_slug"])
    return changed


@up_next_bp.route("/api/up-next/ack", methods=["POST"])
def api_ack():
    """"I got the notification, I'll watch it" — the dock's ✓ button.

    Body (JSON): {slug} or {slugs: [...]}. Stops the reminder (the timer goes
    off) while leaving the show on the queue, so Up Next still lists it for
    anyone who missed the notification. Returns the slugs actually cleared.
    """
    user, err = _require_user_json()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    raw = data.get("slugs")
    if isinstance(raw, list):
        slugs = list(raw)
    else:
        slugs = [data.get("slug")]

    cleared = []
    for value in slugs:
        slug = _clean_slug(value)
        if slug is None or slug in cleared:
            continue
        if ack_up_next_reminder(user["id"], slug):
            cleared.append(slug)
    return jsonify({"success": True, "cleared": cleared})


@up_next_bp.route("/api/up-next/<anime_slug>", methods=["DELETE"])
def api_remove(anime_slug):
    user, err = _require_user_json()
    if err:
        return err
    slug = _clean_slug(anime_slug)
    if slug is None:
        return jsonify({"success": False, "error": "slug"}), 400
    removed = clear_up_next(user["id"], slug)
    return jsonify({"success": True, "removed": bool(removed)})


@up_next_bp.route("/api/up-next/options", methods=["GET"])
def api_options():
    """Candidate shows for an ``after I finish…`` reminder: the user's own
    queue (minus the anime being edited), so the picker never offers a show
    they are not already watching or planning."""
    user, err = _require_user_json()
    if err:
        return err
    exclude = (request.args.get("exclude") or "").strip()
    qmap = get_up_next_map(user["id"])
    out = []
    for slug, row in qmap.items():
        if slug == exclude or row.get("status") == UP_NEXT_DONE:
            continue
        entry = anime_database.get(slug) or {}
        out.append({"slug": slug, "title": entry.get("title") or slug})
    out.sort(key=lambda o: o["title"].lower())
    return jsonify({"success": True, "options": out, "push": push_available()})


@up_next_bp.route("/api/up-next/push/key", methods=["GET"])
def api_push_key():
    """The VAPID public key the browser needs to subscribe."""
    if not push_available():
        return jsonify({"success": False, "error": "push_unavailable"}), 503
    _priv, pub = get_vapid()
    return jsonify({"success": True, "key": pub})


@up_next_bp.route("/api/up-next/push/subscribe", methods=["POST"])
def api_push_subscribe():
    user, err = _require_user_json()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        return jsonify({"success": False, "error": "subscription"}), 400
    save_push_subscription(
        user["id"], endpoint, p256dh, auth,
        request.headers.get("User-Agent") or "",
    )
    return jsonify({"success": True})


@up_next_bp.route("/api/up-next/push/unsubscribe", methods=["POST"])
def api_push_unsubscribe():
    user, err = _require_user_json()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    endpoint = (data.get("endpoint") or "").strip()
    if endpoint:
        delete_push_subscription(endpoint)
    else:
        for sub in get_push_subscriptions(user["id"]):
            delete_push_subscription(sub["endpoint"])
    return jsonify({"success": True})


@up_next_bp.route("/api/up-next/push/status", methods=["GET"])
def api_push_status():
    """How many devices this user has push enabled on — the page shows the
    honest count instead of a vague "notifications on"."""
    user, err = _require_user_json()
    if err:
        return err
    subs = get_push_subscriptions(user["id"])
    _priv, pub = get_vapid() if push_available() else ("", "")
    return jsonify({
        "success": True,
        "push": push_available(),
        "devices": len(subs),
        "public_key": pub,
    })


@up_next_bp.route("/api/up-next/sweep", methods=["GET", "POST"])
def api_sweep():
    """Run the reminder sweep on demand, for an external scheduler.

    Render's free plan spins a web service down after 15 minutes without
    traffic, and the reminder thread lives inside that process — so while the
    user is away nothing sweeps and a due reminder never reaches their phone.
    A five-minute tick (.github/workflows/reminders.yml) wakes the app and
    calls this, which is the only reason reminders fire with nobody browsing.

    Token-gated by the SWEEP_TOKEN env var. Unset -> 503, so a deploy that
    has not configured a token never exposes an open push trigger.
    """
    expected = (os.environ.get(SWEEP_TOKEN_ENV) or "").strip()
    if not expected:
        return jsonify({"success": False, "error": "sweep_disabled"}), 503
    supplied = (
        request.headers.get("X-Sweep-Token") or request.args.get("token") or ""
    ).strip()
    if not hmac.compare_digest(supplied, expected):
        return jsonify({"success": False, "error": "unauthorized"}), 401
    return jsonify({"success": True, "sent": run_reminder_sweep(log=True)})


@up_next_bp.route("/up-next")
def page():
    """The Up Next page: tonight, tomorrow, later, and watching now.

    Shelves are computed server-side so the page is useful before any JS
    runs, and the client only takes over for setting/clearing entries.
    """
    user = g.get("user")
    if user is None:
        return render_template("up_next.html", signed_in=False)

    cards, resolved = queued_cards(user["id"], limit=120)
    today = _today()
    buckets = {"ready": [], "now": [], "tonight": [], "later": [], "waiting": []}
    for card in cards:
        state = card["up_next"]
        kind = state.get("remind_kind")
        on = parse_date(state.get("remind_on"))
        if state["due"]:
            buckets["ready"].append(card)
        elif state["status"] == UP_NEXT_NOW:
            buckets["now"].append(card)
        elif kind == "tonight" or (on and on == today):
            buckets["tonight"].append(card)
        elif kind == "after":
            buckets["waiting"].append(card)
        else:
            buckets["later"].append(card)

    ready_count, ready_oldest = waiting_summary(cards)
    return render_template(
        "up_next.html",
        signed_in=True,
        ready=buckets["ready"],
        ready_count=ready_count,
        ready_oldest=ready_oldest,
        now=buckets["now"],
        tonight=buckets["tonight"],
        later=buckets["later"],
        waiting=buckets["waiting"],
        total=len(cards),
        push_devices=len(get_push_subscriptions(user["id"])),
        push_available=push_available(),
        vapid_public_key=(get_vapid()[1] if push_available() else ""),
    )
