import os
import sqlite3
import random
import threading
import time
from datetime import datetime, timezone
from dev_accounts import is_dev_username

DATABASE = "animechat.db"
AVATAR_COLORS = ["#00c16a", "#3b82f6", "#f59e0b", "#ec4899", "#9333ea", "#06b6d4", "#ef4444"]

# ==========================================================
# Turso (remote SQLite) support
#
# By default the app stores everything in a local animechat.db file. On
# hosting that recreates the app folder (free hosts, container recycling,
# redeploys), that file gets wiped and every account + history + list is
# lost. To keep data permanently, create a free Turso database
# (https://turso.tech) and set:
#
#     TURSO_DATABASE_URL   e.g. libsql://your-db-org.turso.io
#     TURSO_AUTH_TOKEN     the database token
#
# The app then stores everything in the remote database instead and all
# existing SQL keeps working unchanged. If Turso is unreachable, the app
# falls back to the local file so the site keeps working.
# ==========================================================
TURSO_DATABASE_URL = os.environ.get("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
try:
    import turso_serverless
except ImportError:
    turso_serverless = None
TURSO_ENABLED = bool(
    turso_serverless is not None
    and TURSO_DATABASE_URL
    and TURSO_AUTH_TOKEN
)
TURSO_BROKEN = False

if TURSO_ENABLED:
    print("OTAKUL DB: using Turso (persistent) database")
    # The bundled driver posts every statement over a brand-new urllib
    # connection (fresh TCP + TLS handshake per call), which turns each
    # small query into a full round trip. Patch its transport to reuse a
    # pooled keep-alive connection so chat polls, list saves and page
    # loads stay fast on the deployed site.
    import json as _json
    import requests as _requests
    from turso_serverless.session import Session as _TursoSession
    from turso_serverless.protocol import ProtocolError as _TursoProtocolError

    _turso_http = _requests.Session()

    def _turso_post_keepalive(self, path, body):
        url = f"{self._base_url}{path}"
        data = _json.dumps(body, allow_nan=False).encode("utf-8")
        try:
            resp = _turso_http.post(url, data=data, headers=self._headers(), timeout=30)
        except _requests.exceptions.RequestException as e:
            self._reset_stream()
            raise _TursoProtocolError(f"request to {url} failed: {e!r}") from None
        if resp.status_code != 200:
            self._reset_stream()
            message = None
            try:
                parsed = resp.json()
                if isinstance(parsed, dict):
                    for key in ("error", "message"):
                        if isinstance(parsed.get(key), str):
                            message = parsed[key]
                            break
            except ValueError:
                pass
            if message is not None:
                raise _TursoProtocolError(f"HTTP status {resp.status_code}: {message}") from None
            raise _TursoProtocolError(f"HTTP status {resp.status_code}") from None
        return resp.content

    _TursoSession._post = _turso_post_keepalive
else:
    print("OTAKUL DB: using local file animechat.db (accounts/history/lists can be lost if the app folder is recreated)")


def _mark_turso_broken(error):
    global TURSO_BROKEN
    if TURSO_ENABLED and not TURSO_BROKEN:
        TURSO_BROKEN = True
        print("CRITICAL: TURSO DATABASE UNREACHABLE - %r" % (error,))
        print("CRITICAL: Falling back to local animechat.db. DATA WILL BE LOST ON REDEPLOY.")
        print("CRITICAL: Check TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in the hosting environment.")


class RowView:
    """sqlite3.Row-style row: supports row["name"] and row[index]."""

    def __init__(self, keys, values):
        self._keys = tuple(keys)
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return self._values[self._keys.index(key)]

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __repr__(self):
        return "<RowView %r>" % (dict(zip(self._keys, self._values)),)


class CompatCursor:
    """Wraps a sqlite3-style cursor (turso's Cursor) and normalizes rows so
    row["column"] works exactly like sqlite3.Row."""

    def __init__(self, cursor):
        self._cursor = cursor

    @property
    def lastrowid(self):
        return getattr(self._cursor, "lastrowid", None)

    @property
    def rowcount(self):
        return getattr(self._cursor, "rowcount", -1)

    @property
    def description(self):
        return getattr(self._cursor, "description", None)

    def _column_names(self):
        description = getattr(self._cursor, "description", None)
        if not description:
            return None
        try:
            return [column[0] for column in description]
        except Exception:
            return None

    def _normalize(self, row):
        if row is None or isinstance(row, (RowView, sqlite3.Row, dict)):
            return row
        keys = self._column_names()
        if keys is None:
            return row
        return RowView(keys, row)

    def execute(self, sql, parameters=()):
        self._cursor.execute(sql, parameters)
        return self

    def fetchone(self):
        return self._normalize(self._cursor.fetchone())

    def fetchall(self):
        return [self._normalize(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        for row in self._cursor:
            yield self._normalize(row)


def _new_local_connection():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        pass
    return conn


class CompatConnection:
    """Wraps a sqlite3-style connection (turso's Connection) so conn.execute,
    conn.cursor, commit and close all behave like the local sqlite3 API.

    turso's connect() is lazy: it only talks to the network on the first
    query. If any call fails, the wrapper marks Turso broken and re-runs the
    operation against a fresh local connection, so a Turso outage degrades
    to the local file transparently instead of 500ing the request."""

    def __init__(self, connection):
        self._connection = connection

    def _fallback_local(self):
        if not isinstance(self._connection, sqlite3.Connection):
            self._connection = _new_local_connection()
        return self._connection

    def execute(self, sql, parameters=()):
        try:
            return CompatCursor(self._connection.execute(sql, parameters))
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().execute(sql, parameters))

    def cursor(self):
        try:
            return CompatCursor(self._connection.cursor())
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().cursor())

    def executemany(self, sql, seq_of_parameters):
        try:
            return CompatCursor(self._connection.executemany(sql, seq_of_parameters))
        except Exception as error:
            _mark_turso_broken(error)
            return CompatCursor(self._fallback_local().executemany(sql, seq_of_parameters))

    def commit(self):
        try:
            return self._connection.commit()
        except Exception as error:
            _mark_turso_broken(error)
            return self._fallback_local().commit()

    def rollback(self):
        try:
            return self._connection.rollback()
        except Exception as error:
            _mark_turso_broken(error)
            return self._fallback_local().rollback()

    def close(self):
        # Don't close the persistent Turso connection — it's reused
        # across requests to avoid re-establishing TLS + auth each time.
        if isinstance(self._connection, sqlite3.Connection):
            try:
                return self._connection.close()
            except Exception:
                pass


# Persistent Turso connection — reuse instead of creating a new one
# per request, which eliminates the TLS handshake + auth round-trip
# that was making every page take 5-10+ seconds.
_turso_conn = None
_turso_conn_lock = threading.Lock()

# =====================================================================
# STANDALONE WAR ZONE (Phase 1 - Free / Friendly wars)
#
# A war is its own battlefield: the creator posts a declaration (the one
# position everyone is fighting over), any C+ user enters one battler, the
# crowd votes by like-ratio (review_type='warzone' votes reuse review_likes,
# so the C+ dislike gate still applies), and the best take is crowned when
# the timer ends. Guild / GvG duels (claims, owner pick, guild XP, winner /
# broken guild flags) layer on later on top of this shared engine.
# =====================================================================
WARZONE_HOURS = (24, 48, 72)
WARZONE_MIN_VOTES = 3  # an entry needs this many votes to be crowned


def _ensure_warzone_tables():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS warzones(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL DEFAULT 'friendly',
            title TEXT NOT NULL,
            declaration TEXT NOT NULL,
            topic_type TEXT NOT NULL DEFAULT 'blank',
            anime_slug TEXT,
            episode_ref TEXT,
            gif_url TEXT,
            created_by INTEGER NOT NULL,
            guild_a INTEGER,
            guild_b INTEGER,
            entry_scope TEXT NOT NULL DEFAULT 'open',
            is_private INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open',
            hours INTEGER NOT NULL DEFAULT 24,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            ends_at TEXT NOT NULL,
            settled INTEGER NOT NULL DEFAULT 0,
            winner_entry_id INTEGER
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS war_entries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            warzone_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(warzone_id, user_id)
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS gvg_claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            war_id INTEGER NOT NULL,
            guild_id INTEGER NOT NULL,
            claimant_owner_id INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(war_id, guild_id)
        )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS guild_war_xp(
            guild_id INTEGER PRIMARY KEY,
            xp INTEGER NOT NULL DEFAULT 0
        )"""
    )
    _gz_cols = [r[1] for r in cur.execute("PRAGMA table_info(warzones)").fetchall()]
    if "guild_awarded" not in _gz_cols:
        cur.execute("ALTER TABLE warzones ADD COLUMN guild_awarded INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    conn.close()


def create_warzone(user_id, title, declaration, hours=24, is_private=False,
                   topic_type='blank', anime_slug=None, episode_ref=None, gif_url=None):
    """Create a standalone war. Returns (ok, err, war_id)."""
    _ensure_warzone_tables()
    title = (title or '').strip()[:120]
    declaration = (declaration or '').strip()
    if len(title) < 3:
        return False, 'Give your war a short title.', None
    if len(declaration) < 2:
        return False, 'Write a declaration - the position everyone is fighting over.', None
    if len(declaration) > 1000:
        return False, 'Keep your declaration under 1000 characters.', None
    hours = hours if hours in WARZONE_HOURS else 24
    ends_s = datetime.fromtimestamp(int(time.time()) + hours * 3600, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO warzones
        (mode, title, declaration, topic_type, anime_slug, episode_ref, gif_url,
         created_by, entry_scope, is_private, hours, ends_at)
        VALUES ('friendly', ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
        (title, declaration, topic_type, anime_slug, episode_ref, gif_url,
         user_id, 1 if is_private else 0, hours, ends_s),
    )
    wid = cur.lastrowid
    conn.commit()
    conn.close()
    return True, None, wid


def _wz_entry_dict(row, my_votes=None):
    likes = row['likes'] or 0
    dislikes = row['dislikes'] or 0
    total = likes + dislikes
    return {
        'id': row['id'],
        'user_id': row['user_id'],
        'username': row['username'] or 'user',
        'avatar': row['avatar'],
        'avatar_color': row['avatar_color'] or '#374151',
        'rank': 'S+' if is_dev_username(row['username']) else get_xp_tier(row['xp'] or 0),
        'content': row['content'],
        'created_at': row['created_at'],
        'likes': likes,
        'dislikes': dislikes,
        'total': total,
        'ratio': round(likes / total, 4) if total else 0.0,
        'ratio_pct': round(likes / total * 100) if total else 0,
        'my_vote': (my_votes or {}).get(row['id']),
        'winner': False,
        'flag': 'open',
    }


def _wz_entries(wid, user_id=None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT e.id, e.user_id, e.content, e.created_at,
        u.username, u.avatar, u.avatar_color,
        (ux.xp + IFNULL(ux.war_reward_xp, 0)) as xp,
        SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM war_entries e
        LEFT JOIN users u ON u.id = e.user_id
        LEFT JOIN user_xp ux ON ux.user_id = e.user_id
        LEFT JOIN review_likes rl ON rl.review_type='warzone' AND rl.review_id = e.id
        WHERE e.warzone_id=?
        GROUP BY e.id ORDER BY e.id ASC""",
        (wid,),
    )
    rows = cur.fetchall()
    ids = [r['id'] for r in rows]
    my_votes = {}
    if user_id and ids:
        p = ','.join('?' * len(ids))
        cur.execute(
            f"SELECT review_id, is_like FROM review_likes WHERE review_type='warzone' AND user_id=? AND review_id IN ({p})",
            [user_id] + ids,
        )
        my_votes = {r['review_id']: r['is_like'] for r in cur.fetchall()}
    conn.close()
    return [_wz_entry_dict(r, my_votes) for r in rows]


def _warzone_view(war, entries):
    ends_ts = _iso_to_ts(war['ends_at'])
    now = int(time.time())
    # A declared GvG is still gathering rival claims; it hasn't started yet,
    # so its stored status wins and ends_at is just a placeholder until the
    # declaring owner picks a rival.
    if war['status'] == 'declared':
        status = 'declared'
    else:
        status = 'settled' if war['settled'] else ('open' if now < ends_ts else 'settled')
    eligible = sorted(
        [e for e in entries if e['total'] >= WARZONE_MIN_VOTES],
        key=lambda e: (e['ratio'], e['likes']), reverse=True,
    )
    winner = None
    podium = []
    if status == 'settled':
        podium = [{'place': i + 1, **e} for i, e in enumerate(eligible[:3])]
        winner = eligible[0] if eligible else None
        for e in entries:
            win_flag = bool(winner and e['id'] == winner['id'])
            e['winner'] = win_flag
            e['flag'] = 'win' if win_flag else 'lost'
    view = dict(war)
    view['id'] = war['id']
    view['ends_ts'] = ends_ts
    view['status'] = status
    view['entries'] = entries
    view['battlers'] = len(entries)
    view['leader'] = eligible[0] if eligible else None
    view['winner'] = winner
    view['podium'] = podium
    view['flag'] = 'win' if (status == 'settled' and winner) else ('no_contest' if status == 'settled' else ('declared' if status == 'declared' else 'open'))
    return view


def _mark_warzone_settled(wid, winner_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE warzones SET settled=1, winner_entry_id=?, status='settled' WHERE id=? AND settled=0",
        (winner_id if winner_id is not None else 0, wid),
    )
    conn.commit()
    conn.close()


def _attach_guild_meta(war):
    """Attach guild A/B info + Guild XP and, for declared GvG, the list of
    rival guilds that claimed the declaration."""
    for field, out in (('guild_a', 'guild_a_info'), ('guild_b', 'guild_b_info')):
        gid = war.get(field)
        if gid:
            info = _guild_info(gid)
            if info:
                info['xp'] = get_guild_xp(gid)
                war[out] = info
    if war.get('mode') == 'gvg' and war.get('status') == 'declared':
        claims = []
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT guild_id FROM gvg_claims WHERE war_id=?", (war['id'],))
        for row in cur.fetchall():
            info = _guild_info(row['guild_id'])
            if info:
                info['xp'] = get_guild_xp(row['guild_id'])
                claims.append(info)
        conn.close()
        war['claims'] = claims


def get_warzone(wid, user_id=None):
    """Return a single war as a dict (with entries, leader/winner/podium,
    flags), lazily settling it if the timer has ended."""
    _ensure_warzone_tables()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT w.*, (SELECT username FROM users u WHERE u.id=w.created_by) as creator_name
        FROM warzones w WHERE id=?""",
        (wid,),
    )
    war = cur.fetchone()
    conn.close()
    if not war:
        return None
    war = dict(war)
    _attach_guild_meta(war)
    entries = _wz_entries(wid, user_id)
    view = _warzone_view(war, entries)
    if view['status'] == 'settled' and not war['settled']:
        _mark_warzone_settled(wid, view['winner']['id'] if view['winner'] else None)
        _award_guild_xp(view, war)
    if war.get('mode') == 'gvg' and view.get('winner') and view.get('guild_a') and view.get('guild_b'):
        ga, gb = view.get('guild_a'), view.get('guild_b')
        win_gid = ga if _is_guild_member(ga, view['winner']['user_id']) else gb
        view['win_guild_id'] = win_gid
        view['lose_guild_id'] = gb if win_gid == ga else ga
    return view


def get_warzones(user_id=None):
    """Every standalone war, hottest first: nearest-deadline open wars on top,
    then settled ones (newest first). Private wars are hidden unless the
    viewer created them."""
    _ensure_warzone_tables()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT w.id, w.mode, w.title, w.declaration, w.topic_type, w.anime_slug,
        w.episode_ref, w.gif_url, w.created_by, w.guild_a, w.guild_b, w.entry_scope,
        w.is_private, w.status, w.hours, w.ends_at, w.settled, w.winner_entry_id,
        w.guild_awarded, w.created_at,
        (SELECT username FROM users u WHERE u.id=w.created_by) as creator_name,
        (SELECT avatar FROM users u WHERE u.id=w.created_by) as creator_avatar,
        (SELECT ux.xp FROM user_xp ux WHERE ux.user_id=w.created_by) as creator_xp
        FROM warzones w
        WHERE w.is_private=0 OR w.created_by=? ORDER BY w.id DESC LIMIT 80""",
        (user_id or 0,),
    )
    rows = cur.fetchall()
    conn.close()
    wars = []
    now = int(time.time())
    for r in rows:
        war = dict(r)
        war['creator_rank'] = 'S+' if is_dev_username(war.get('creator_name')) else get_xp_tier(war.get('creator_xp') or 0)
        war['creator_xp'] = war.get('creator_xp') or 0
        _attach_guild_meta(war)
        entries = _wz_entries(war['id'])
        view = _warzone_view(war, entries)
        if view['status'] == 'settled' and not war['settled']:
            _mark_warzone_settled(war['id'], view['winner']['id'] if view['winner'] else None)
            _award_guild_xp(view, war)
        wars.append(view)
    wars.sort(key=lambda w: (0 if w['status'] == 'open' else 1, w['ends_ts'] if w['status'] == 'open' else -now))
    return wars


def add_warzone_entry(user_id, wid, content):
    """Enter one battler into an open war. Returns (ok, err, entry)."""
    _ensure_warzone_tables()
    content = (content or '').strip()
    if len(content) < 2:
        return False, 'Your battler is too short.', None
    if len(content) > 500:
        return False, 'War battlers must be 500 characters or fewer.', None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT ends_at, settled, status FROM warzones WHERE id=?", (wid,))
    war = cur.fetchone()
    if not war:
        conn.close()
        return False, 'No such war.', None
    if war['settled'] or war['status'] != 'open' or int(time.time()) >= _iso_to_ts(war['ends_at']):
        conn.close()
        return False, 'This war is over.', None
    cur.execute("SELECT id FROM war_entries WHERE warzone_id=? AND user_id=?", (wid, user_id))
    if cur.fetchone():
        conn.close()
        return False, 'You already entered this war.', None
    cur.execute(
        "INSERT INTO war_entries (warzone_id, user_id, content) VALUES (?, ?, ?)",
        (wid, user_id, content),
    )
    eid = cur.lastrowid
    conn.commit()
    conn.close()
    entry = next((e for e in _wz_entries(wid, user_id) if e['id'] == eid), None)
    return True, None, entry


# =====================================================================
# GUILD & GvG WARS (Phase 2 core)
#
# Guilds are thr_communities (same DB as database.py). A GvG starts as a
# public DECLARATION of war by one guild's owner; rival guilds CLAIM it;
# the declaring owner PICKS one rival -> that books the duel (both guilds'
# members may enter battlers, the whole site votes). The war settles by
# like-ratio; the winning guild takes the flag and Guild XP. A single-guild
# war ('guild' mode) is members vs members for top-3, still guild-flagged.
# =====================================================================
GUILD_MIN_MEMBERS = 50
GUILD_XP_WIN = 100
GUILD_XP_LOSE = 20
# Personal war-reward pool (permanent): winner-guild every member +30,
# top-3 battlers +60 each, winner-guild owner +42 (30:42:60 = 5:7:10).
GUILD_REWARD_MEMBER = 30
GUILD_REWARD_OWNER = 42
GUILD_REWARD_TOP3 = 60
# Cooldown: a guild can only join a war once every 15 days (2 wars/month).
GUILD_WAR_COOLDOWN_DAYS = 15


def _guild_info(gid):
    if not gid:
        return None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT c.id, c.name, c.owner_id,
        (SELECT COUNT(*) FROM thr_community_members m WHERE m.community_id=c.id) as members
        FROM thr_communities c WHERE c.id=?""",
        (gid,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "owner_id": row["owner_id"], "members": row["members"]}


def _is_guild_member(gid, uid):
    if not gid or not uid:
        return False
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM thr_community_members WHERE community_id=? AND user_id=?", (gid, uid)
    )
    ok = cur.fetchone() is not None
    conn.close()
    return ok


def _guild_active_war(gid):
    """True if this guild is already host or rival in a declared/open war."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM warzones WHERE (guild_a=? OR guild_b=?) AND status IN ('declared','open')",
        (gid, gid),
    )
    row = cur.fetchone()
    conn.close()
    return row is not None


def _guild_war_block(gid):
    """Return an error string if this guild can't start/join a war right now
    (already in one, or on cooldown from a war that ended within the last 15
    days), else None."""
    if _guild_active_war(gid):
        return 'Your guild already has a war in flight.'
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT MAX(ends_at) as last FROM warzones
        WHERE (guild_a=? OR guild_b=?) AND settled=1 AND mode IN ('gvg','guild')""",
        (gid, gid),
    )
    row = cur.fetchone()
    conn.close()
    if row and row['last']:
        days = (int(time.time()) - _iso_to_ts(row['last'])) / 86400.0
        if days < GUILD_WAR_COOLDOWN_DAYS:
            left = int(GUILD_WAR_COOLDOWN_DAYS - days) + 1
            plural = '' if left == 1 else 's'
            return f'Your guild is on war cooldown - you can declare/join again in about {left} day{plural}.'
    return None


def create_guild_war(user_id, guild_id, title, declaration, hours=48, is_private=False,
                     topic_type='blank', anime_slug=None, episode_ref=None, gif_url=None):
    """A single-guild battle: only that guild's members can enter battlers."""
    g = _guild_info(guild_id)
    if not g:
        return False, 'Guild not found.', None
    ok, err, wid = create_warzone(user_id, title, declaration, hours, is_private,
                                  topic_type, anime_slug, episode_ref, gif_url)
    if not ok:
        return False, err, None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE warzones SET mode='guild', guild_a=?, entry_scope='guild' WHERE id=?", (guild_id, wid))
    conn.commit()
    conn.close()
    return True, None, wid


def create_gvg_declaration(user_id, guild_id, title, declaration, hours=72,
                           topic_type='blank', anime_slug=None, episode_ref=None, gif_url=None):
    """Guild A's owner posts a public declaration of war. Guild A must have
    50+ members. Returns (ok, err, war_id)."""
    g = _guild_info(guild_id)
    if not g:
        return False, 'Guild not found.', None
    if g['owner_id'] != user_id:
        return False, 'Only a guild owner can issue a declaration of war.', None
    if g['members'] < GUILD_MIN_MEMBERS:
        return False, 'Your guild needs 50+ members to declare war.', None
    block = _guild_war_block(guild_id)
    if block:
        return False, block, None
    ok, err, wid = create_warzone(user_id, title, declaration, hours, False,
                                  topic_type, anime_slug, episode_ref, gif_url)
    if not ok:
        return False, err, None
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE warzones SET mode='gvg', guild_a=?, entry_scope='guilds', status='declared' WHERE id=?",
        (guild_id, wid),
    )
    conn.commit()
    conn.close()
    return True, None, wid


def claim_gvg(claimant_user, claimant_guild_id, war_id):
    """A rival guild's owner claims a public declaration. Returns (ok, err)."""
    _ensure_warzone_tables()
    g = _guild_info(claimant_guild_id)
    if not g:
        return False, 'Guild not found.'
    if g['owner_id'] != claimant_user:
        return False, 'Only a guild owner can claim a declaration.'
    if g['members'] < GUILD_MIN_MEMBERS:
        return False, 'Your guild needs 50+ members to claim a war.'
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, mode, guild_a, status FROM warzones WHERE id=?", (war_id,))
    war = cur.fetchone()
    if not war or war['mode'] != 'gvg' or war['status'] != 'declared':
        conn.close()
        return False, 'This declaration is no longer open.'
    if war['guild_a'] == claimant_guild_id:
        conn.close()
        return False, 'You cannot claim your own declaration.'
    block = _guild_war_block(claimant_guild_id)
    if block:
        conn.close()
        return False, block
    cur.execute(
        "SELECT id FROM gvg_claims WHERE war_id=? AND guild_id=?", (war_id, claimant_guild_id)
    )
    if cur.fetchone():
        conn.close()
        return False, 'Your guild already claimed this declaration.'
    cur.execute(
        "INSERT INTO gvg_claims (war_id, guild_id, claimant_owner_id) VALUES (?, ?, ?)",
        (war_id, claimant_guild_id, claimant_user),
    )
    conn.commit()
    conn.close()
    return True, None


def pick_gvg_rival(declaring_owner, war_id, rival_guild_id):
    """The declaring guild's owner picks a rival from the claims, booking the
    duel (books a slot on the clock and opens it to both sides)."""
    _ensure_warzone_tables()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT mode, guild_a, status FROM warzones WHERE id=?", (war_id,))
    war = cur.fetchone()
    if not war or war['mode'] != 'gvg' or war['status'] != 'declared':
        conn.close()
        return False, 'This declaration is already booked.', None
    info = _guild_info(war['guild_a'])
    if not info or info['owner_id'] != declaring_owner:
        conn.close()
        return False, 'Only the declaring guild owner can pick a rival.', None
    cur.execute(
        "SELECT 1 FROM gvg_claims WHERE war_id=? AND guild_id=?", (war_id, rival_guild_id)
    )
    if not cur.fetchone():
        conn.close()
        return False, 'That guild never claimed this declaration.', None
    hours = 72
    ends_s = datetime.fromtimestamp(int(time.time()) + hours * 3600, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    cur.execute(
        "UPDATE warzones SET guild_b=?, status='open', ends_at=?, hours=? WHERE id=?",
        (rival_guild_id, ends_s, hours, war_id),
    )
    conn.commit()
    conn.close()
    return True, None, ends_s


def add_guild_war_entry(user_id, war_id, content):
    """Enter a battler into a guild or gvg war. Entries are guild-scoped:
    single-guild wars allow only that guild's members; GvG allows members of
    either fighting guild. Returns (ok, err, entry)."""
    _ensure_warzone_tables()
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT mode, guild_a, guild_b, status, settled FROM warzones WHERE id=?", (war_id,))
    war = cur.fetchone()
    conn.close()
    if not war or war['status'] != 'open' or war['settled']:
        return False, 'This war is not open.', None
    if war['mode'] == 'guild':
        if not _is_guild_member(war['guild_a'], user_id):
            return False, 'Only members of this guild can enter the war.', None
    elif war['mode'] == 'gvg':
        if not (_is_guild_member(war['guild_a'], user_id) or _is_guild_member(war['guild_b'], user_id)):
            return False, 'Only members of the two fighting guilds can enter.', None
    else:
        return False, 'Not a guild war.', None
    return add_warzone_entry(user_id, war_id, content)


def get_warzone_mode(wid):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT mode FROM warzones WHERE id=?", (wid,))
    row = cur.fetchone()
    conn.close()
    return row['mode'] if row else None


def _change_guild_xp(gid, amount):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT xp FROM guild_war_xp WHERE guild_id=?", (gid,))
    row = cur.fetchone()
    new_xp = (row["xp"] if row else 0) + amount
    if row:
        cur.execute("UPDATE guild_war_xp SET xp=? WHERE guild_id=?", (new_xp, gid))
    else:
        cur.execute("INSERT INTO guild_war_xp (guild_id, xp) VALUES (?, ?)", (gid, new_xp))
    conn.commit()
    conn.close()


def get_guild_xp(gid):
    if not gid:
        return 0
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT xp FROM guild_war_xp WHERE guild_id=?", (gid,))
    row = cur.fetchone()
    conn.close()
    return row["xp"] if row else 0


def _add_war_reward(user_id, amount):
    """Give a user a one-time war-reward XP pool. We credit BOTH the live
    `xp` (so the boost is visible on the profile right away) and a separate
    `war_reward_xp` record (so it survives a later vote-driven recalc - the
    preserving wrapper below re-adds the pool every time XP is recomputed).
    A war can therefore only ever raise XP/rank, never decay it."""
    if not user_id or not amount:
        return
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM user_xp WHERE user_id=?", (user_id,))
    if cur.fetchone():
        cur.execute(
            "UPDATE user_xp SET xp = xp + ?, war_reward_xp = war_reward_xp + ? WHERE user_id=?",
            (amount, amount, user_id),
        )
    else:
        cur.execute(
            "INSERT INTO user_xp (user_id, xp, war_reward_xp) VALUES (?, ?, ?)",
            (user_id, amount, amount),
        )
    conn.commit()
    conn.close()


def recalculate_user_xp_preserving_rewards(user_id):
    """Recalculate a user's vote-derived XP while keeping their permanent
    war-reward pool intact. recalculate_user_xp now folds war_reward_xp
    back into xp itself, so this is a thin alias kept for callers that
    explicitly want the preserving behavior."""
    recalculate_user_xp(user_id)


def get_guild_member_ids(gid):
    """Every member user id of a guild (thr_communities)."""
    if not gid:
        return []
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM thr_community_members WHERE community_id=?", (gid,))
    ids = [r["user_id"] for r in cur.fetchall()]
    conn.close()
    return ids


def _award_guild_xp(view, war_row):
    """Once per settled guild/GvG war: award Guild XP and the personal
    war-reward XP pool. Guarded by the warzones.guild_awarded flag so it
    pays out exactly once."""
    mode = war_row['mode']
    if war_row.get('guild_awarded') or not view.get('winner'):
        return
    if mode == 'gvg' and war_row.get('guild_a') and war_row.get('guild_b'):
        win_gid = war_row['guild_a'] if _is_guild_member(war_row['guild_a'], view['winner']['user_id']) else war_row['guild_b']
        lose_gid = war_row['guild_b'] if win_gid == war_row['guild_a'] else war_row['guild_a']
        _change_guild_xp(win_gid, GUILD_XP_WIN)
        _change_guild_xp(lose_gid, GUILD_XP_LOSE)
        # Personal rewards: every winner-guild member, its owner, and the
        # top-3 battlers (by like-ratio) all bank a permanent war-reward pool.
        owner = _guild_info(win_gid)
        owner_uid = owner['owner_id'] if owner else None
        for uid in get_guild_member_ids(win_gid):
            _add_war_reward(uid, GUILD_REWARD_MEMBER)
        if owner_uid:
            _add_war_reward(owner_uid, GUILD_REWARD_OWNER)
        for e in view.get('podium') or []:
            _add_war_reward(e.get('user_id'), GUILD_REWARD_TOP3)
    elif mode == 'guild' and war_row.get('guild_a'):
        _change_guild_xp(war_row['guild_a'], GUILD_XP_LOSE)
        for e in view.get('podium') or []:
            _add_war_reward(e.get('user_id'), GUILD_REWARD_TOP3)
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("UPDATE warzones SET guild_awarded=1 WHERE id=? AND guild_awarded=0", (war_row['id'],))
    conn.commit()
    conn.close()


def get_connection():
    global _turso_conn
    if TURSO_ENABLED and not TURSO_BROKEN:
        with _turso_conn_lock:
            if _turso_conn is not None:
                return _turso_conn
        try:
            connection = turso_serverless.connect(
                TURSO_DATABASE_URL,
                auth_token=TURSO_AUTH_TOKEN,
                isolation_level=None,
            )
            conn = CompatConnection(connection)
            with _turso_conn_lock:
                _turso_conn = conn
            return conn
        except Exception as error:
            _mark_turso_broken(error)

    return _new_local_connection()


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS anime_ratings(
            anime_slug TEXT PRIMARY KEY,
            total_rating INTEGER DEFAULT 0,
            total_votes INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT 'Anonymous',
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # A negative Reply War winner lands a one-time XP hit on the review it
    # beat (the crowd's take trumps the review). Zero by default.
    _rv_cols = [row[1] for row in cursor.execute("PRAGMA table_info(reviews)").fetchall()]
    if "war_penalty" not in _rv_cols:
        cursor.execute("ALTER TABLE reviews ADD COLUMN war_penalty INTEGER NOT NULL DEFAULT 0")
    if "war_bonus" not in _rv_cols:
        cursor.execute("ALTER TABLE reviews ADD COLUMN war_bonus INTEGER NOT NULL DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_ratings(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT,
            season_name TEXT,
            episode_number INTEGER,
            total_rating INTEGER DEFAULT 0,
            total_votes INTEGER DEFAULT 0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS episode_reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            season_name TEXT NOT NULL,
            episode_number INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL DEFAULT '#00c16a',
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, anime_slug, season_name, episode_number)
        )
    """)

    _erv_cols = [row[1] for row in cursor.execute("PRAGMA table_info(episode_reviews)").fetchall()]
    if "war_penalty" not in _erv_cols:
        cursor.execute("ALTER TABLE episode_reviews ADD COLUMN war_penalty INTEGER NOT NULL DEFAULT 0")
    if "war_bonus" not in _erv_cols:
        cursor.execute("ALTER TABLE episode_reviews ADD COLUMN war_bonus INTEGER NOT NULL DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            avatar TEXT NOT NULL DEFAULT 'profile1.png',
            is_verified INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migration: existing databases don't have the avatar column yet.
    user_cols = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
    if "avatar" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN avatar TEXT NOT NULL DEFAULT 'profile1.png'")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            anime_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'text',
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_presence(
            anime_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            avatar_color TEXT NOT NULL,
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (anime_slug, user_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quiz_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            answers TEXT NOT NULL,
            top_genres TEXT NOT NULL,
            result_slugs TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_lists(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_list_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            list_id INTEGER NOT NULL,
            anime_slug TEXT NOT NULL,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (list_id, anime_slug),
            FOREIGN KEY(list_id) REFERENCES user_lists(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS view_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            anime_slug TEXT NOT NULL,
            viewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, anime_slug),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_reactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (message_id, user_id, emoji),
            FOREIGN KEY(message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS community_members(
            anime_slug TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (anime_slug, user_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Migration: replies need a reply_to column on chat_messages.
    cols = [row[1] for row in cursor.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "reply_to" not in cols:
        cursor.execute("ALTER TABLE chat_messages ADD COLUMN reply_to INTEGER")

    # Migration: reviews may have user_id column
    review_cols = [row[1] for row in cursor.execute("PRAGMA table_info(reviews)").fetchall()]
    if "user_id" not in review_cols:
        cursor.execute("ALTER TABLE reviews ADD COLUMN user_id INTEGER DEFAULT NULL")

    # Migration: users may have is_public column
    user_cols = [row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()]
    if "is_public" not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN is_public INTEGER DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_xp(
            user_id INTEGER PRIMARY KEY,
            xp INTEGER DEFAULT 0,
            war_reward_xp INTEGER DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    ux_cols = [row[1] for row in cursor.execute("PRAGMA table_info(user_xp)").fetchall()]
    if "war_reward_xp" not in ux_cols:
        cursor.execute("ALTER TABLE user_xp ADD COLUMN war_reward_xp INTEGER DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_likes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            review_type TEXT NOT NULL,
            review_id INTEGER NOT NULL,
            is_like INTEGER NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, review_type, review_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # Rank-weighted vote points: each like/dislike row stores the point value
    # it was worth at vote time (like = 10 x voter-rank weight, dislike = -5 x
    # weight). Existing votes are backfilled with the flat legacy values.
    rl_cols = [row[1] for row in cursor.execute("PRAGMA table_info(review_likes)").fetchall()]
    if "points" not in rl_cols:
        cursor.execute("ALTER TABLE review_likes ADD COLUMN points INTEGER DEFAULT 0")
        cursor.execute(
            "UPDATE review_likes SET points = CASE WHEN is_like=1 THEN 10 ELSE -5 END WHERE points = 0"
        )

    # The /reviews feed looks up votes/points in bulk by (review_type,
    # review_id IN ...) - the UNIQUE(user_id, review_type, review_id)
    # index leads with user_id and can't serve it, so without this index
    # every 200-review page was doing ~200 full table scans.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_likes_type_review "
        "ON review_likes(review_type, review_id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_review_reasons_type_review "
        "ON review_reasons(review_type, review_id)"
    )

    # Community chat reads feed windows by (anime_slug, id) and reactions by
    # message_id - without these, every chat open/poll scanned the whole
    # chat_messages / chat_reactions tables.
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_messages_anime_id "
        "ON chat_messages(anime_slug, id)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_reactions_message "
        "ON chat_reactions(message_id)"
    )

    # Ratings moved from 5-star to 10-star. Legacy rows are 1-5; migrate them
    # to /10 exactly once (only when no rating above 5 exists yet, so a fresh
    # 10-star database is never touched twice). Episode reviews are already
    # stored /10 (2-10), so the guard also keeps them safe.
    row = cursor.execute("SELECT MAX(rating) as m FROM reviews").fetchone()
    if row and row["m"] is not None and row["m"] <= 5:
        cursor.execute("UPDATE reviews SET rating = rating * 2")
    row = cursor.execute("SELECT MAX(rating) as m FROM episode_reviews").fetchone()
    if row and row["m"] is not None and row["m"] <= 5:
        cursor.execute("UPDATE episode_reviews SET rating = rating * 2")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_reasons(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            review_type TEXT NOT NULL DEFAULT 'anime',
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, review_type, review_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS review_replies(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            review_type TEXT NOT NULL DEFAULT 'anime',
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reply_war(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL,
            review_type TEXT NOT NULL DEFAULT 'anime',
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            stance TEXT NOT NULL DEFAULT 'negative',
            rewarded INTEGER DEFAULT 0,
            penalty_applied INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, review_type, review_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)

    # War entries now carry a stance: the replier picks Positive or Negative
    # (legacy dislike-takes default to negative). A negative winner hits the
    # review once — penalty_applied guards that.
    _rw_cols = [row[1] for row in cursor.execute("PRAGMA table_info(reply_war)").fetchall()]
    if "stance" not in _rw_cols:
        cursor.execute("ALTER TABLE reply_war ADD COLUMN stance TEXT NOT NULL DEFAULT 'negative'")
    if "penalty_applied" not in _rw_cols:
        cursor.execute("ALTER TABLE reply_war ADD COLUMN penalty_applied INTEGER DEFAULT 0")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS claims(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            review_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(review_id) REFERENCES reviews(id)
        )
    """)

    # ---- Ota-chan AI assistant chat (one persistent conversation per user)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ota_chan_chat(
            user_id INTEGER NOT NULL,
            conversation_history TEXT NOT NULL DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()


def create_user(username, email, password_hash, avatar="profile1.png"):
    conn = get_connection()
    cursor = conn.cursor()
    avatar_color = random.choice(AVATAR_COLORS)
    cursor.execute(
        """
        INSERT INTO users (username, email, password_hash, avatar_color, avatar, is_verified)
        VALUES (?, ?, ?, ?, ?, 0)
        """,
        (username, email, password_hash, avatar_color, avatar)
    )
    conn.commit()
    user_id = cursor.lastrowid
    conn.close()
    return user_id


def update_user_profile(user_id, username, avatar):
    """Update a user's username and/or avatar image. Returns True on success."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET username = ?, avatar = ? WHERE id = ?",
        (username, avatar, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    if changed:
        _drop_user_cache(user_id)
    return changed


def update_password(email, password_hash):
    """Set a new password hash for the account with the given email."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash = ? WHERE email = ?",
        (password_hash, email),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    if changed:
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        if row:
            _drop_user_cache(row["id"])
    conn.close()
    return changed


def get_user_by_email(email):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE LOWER(email) = LOWER(?)", (email,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_username(username):
    conn = get_connection()
    cursor = conn.cursor()
    # Case-insensitive so shared profile/history links work regardless of
    # how the username is capitalized in the URL (the UNIQUE constraint on
    # username is case-sensitive in SQLite, so exact matches still win first).
    cursor.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE "
        "ORDER BY CASE WHEN username = ? THEN 0 ELSE 1 END LIMIT 1",
        (username, username),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# ---- Per-request user cache -------------------------------------------------
# Every request loads the logged-in user (app.before_request) and chat
# messages resolve their senders, so user rows are the hottest reads in the
# app. On the remote Turso DB each lookup is a network round trip; caching
# them briefly turns those into in-memory hits. Profile updates invalidate
# the entry immediately.
_USER_CACHE_TTL = 20
_user_cache = {}
_user_cache_lock = threading.Lock()


def _cache_user(user_id, row):
    with _user_cache_lock:
        _user_cache[user_id] = (time.time() + _USER_CACHE_TTL, row)


def _drop_user_cache(user_id):
    with _user_cache_lock:
        _user_cache.pop(user_id, None)


def get_user_by_id(user_id):
    if not user_id:
        return None
    now = time.time()
    with _user_cache_lock:
        hit = _user_cache.get(user_id)
        if hit and hit[0] > now:
            return dict(hit[1]) if hit[1] is not None else None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    result = dict(row) if row else None
    _cache_user(user_id, result)
    return result


def get_users_by_ids(user_ids):
    """Fetch many users in ONE query (huge win on remote Turso where every
    query is a network round trip). Returns {id: user_dict} for the ids that
    exist. Results are seeded into the per-user TTL cache."""
    ids = sorted({int(u) for u in user_ids if u})
    if not ids:
        return {}
    out = {}
    missing = []
    now = time.time()
    with _user_cache_lock:
        for uid in ids:
            hit = _user_cache.get(uid)
            if hit and hit[0] > now:
                if hit[1] is not None:
                    out[uid] = dict(hit[1])
            else:
                missing.append(uid)
    if missing:
        placeholders = ",".join("?" for _ in missing)
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM users WHERE id IN ({placeholders})", tuple(missing))
        for row in cursor.fetchall():
            user = dict(row)
            out[user["id"]] = user
            _cache_user(user["id"], user)
        conn.close()
        for uid in missing:
            if uid not in out:
                _cache_user(uid, None)
    return out


def mark_user_verified(user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_verified = 1 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    _drop_user_cache(user_id)


def _attach_reply_info(messages):
    """Add reply_to_username / reply_to_content / reply_to_kind to messages."""
    if not messages:
        return messages
    conn = get_connection()
    cursor = conn.cursor()
    for m in messages:
        m["reply_to_username"] = None
        m["reply_to_content"] = None
        m["reply_to_kind"] = None
        if m.get("reply_to"):
            cursor.execute(
                "SELECT username, content, kind FROM chat_messages WHERE id = ?",
                (m["reply_to"],)
            )
            row = cursor.fetchone()
            if row:
                content = row["content"]
                if len(content) > 180:
                    content = content[:180] + "…"
                m["reply_to_username"] = row["username"]
                m["reply_to_content"] = content
                m["reply_to_kind"] = row["kind"]
    conn.close()
    return messages


def _attach_reactions(messages, user_id=None):
    """Attach reaction counts + the requesting user's reactions to messages."""
    if not messages:
        return messages
    conn = get_connection()
    cursor = conn.cursor()
    ids = [m["id"] for m in messages]
    marks = ",".join("?" * len(ids))

    cursor.execute(
        f"""
        SELECT message_id, emoji, COUNT(*) AS cnt
        FROM chat_reactions
        WHERE message_id IN ({marks})
        GROUP BY message_id, emoji
        ORDER BY cnt DESC
        """,
        ids,
    )
    grouped = {}
    for row in cursor.fetchall():
        grouped.setdefault(row["message_id"], []).append(
            {"emoji": row["emoji"], "count": row["cnt"]}
        )

    my = {}
    if user_id:
        cursor.execute(
            f"""
            SELECT message_id, emoji
            FROM chat_reactions
            WHERE message_id IN ({marks}) AND user_id = ?
            """,
            ids + [user_id],
        )
        for row in cursor.fetchall():
            my.setdefault(row["message_id"], []).append(row["emoji"])
    conn.close()

    for m in messages:
        m["reactions"] = grouped.get(m["id"], [])
        m["my_reactions"] = my.get(m["id"], [])
    return messages


def add_chat_message(anime_slug, user_id, username, avatar_color, kind, content, reply_to=None):
    """Insert a new chat message and return the fully-formed message dict.

    Everything runs on a single DB connection (zero extra round trips):
      1. Validate reply_to (one query)
      2. INSERT the message (one query)
      3. Fetch the user's avatar (one query — same connection)
      4. Fetch reply snippet if applicable (one query — same connection)

    Reactions are skipped entirely: a brand-new message always has zero
    reactions, so querying chat_reactions was pure waste (~800 ms over Turso).
    """
    conn = get_connection()
    cursor = conn.cursor()
    if reply_to:
        cursor.execute("SELECT id FROM chat_messages WHERE id = ?", (reply_to,))
        if cursor.fetchone() is None:
            reply_to = None
    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
    )
    message_id = cursor.lastrowid

    # Fetch the user avatar in the same connection.
    cursor.execute("SELECT avatar FROM users WHERE id = ?", (user_id,))
    avatar_row = cursor.fetchone()
    avatar = (dict(avatar_row)["avatar"] if avatar_row else "profile1.png")

    # Build the message dict directly — no need to re-SELECT the row we just
    # inserted; we already know every column value.
    from datetime import datetime, timezone
    msg = {
        "id": message_id,
        "anime_slug": anime_slug,
        "user_id": user_id,
        "username": username,
        "avatar_color": avatar_color,
        "avatar": avatar,
        "kind": kind,
        "content": content,
        "reply_to": reply_to,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "reactions": [],
        "my_reactions": [],
    }

    # Attach reply snippet in the same connection if needed.
    if reply_to:
        cursor.execute(
            "SELECT username, content, kind FROM chat_messages WHERE id = ?",
            (reply_to,),
        )
        rrow = cursor.fetchone()
        if rrow:
            rc = dict(rrow)
            text = rc["content"] or ""
            if len(text) > 180:
                text = text[:180] + "…"
            msg["reply_to_username"] = rc["username"]
            msg["reply_to_content"] = text
            msg["reply_to_kind"] = rc["kind"]
        else:
            msg["reply_to_username"] = None
            msg["reply_to_content"] = None
            msg["reply_to_kind"] = None
    else:
        msg["reply_to_username"] = None
        msg["reply_to_content"] = None
        msg["reply_to_kind"] = None

    conn.close()
    return msg


def add_chat_message_with_presence(anime_slug, user_id, username, avatar_color, kind, content, reply_to=None):
    """Insert a chat message AND touch presence in a single DB connection.

    Same as add_chat_message but also updates the user's presence in the
    same connection — avoids the extra Turso round-trip that a separate
    touch_presence() call would cost (~800 ms).
    """
    conn = get_connection()
    cursor = conn.cursor()
    if reply_to:
        cursor.execute("SELECT id FROM chat_messages WHERE id = ?", (reply_to,))
        if cursor.fetchone() is None:
            reply_to = None
    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (anime_slug, user_id, username, avatar_color, kind, content, reply_to)
    )
    message_id = cursor.lastrowid

    # Fetch the user avatar in the same connection.
    cursor.execute("SELECT avatar FROM users WHERE id = ?", (user_id,))
    avatar_row = cursor.fetchone()
    avatar = (dict(avatar_row)["avatar"] if avatar_row else "profile1.png")

    # Touch presence in the same connection.
    cursor.execute(
        """
        INSERT INTO chat_presence (anime_slug, user_id, username, avatar_color, last_seen)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(anime_slug, user_id)
        DO UPDATE SET last_seen = CURRENT_TIMESTAMP, username = excluded.username, avatar_color = excluded.avatar_color
        """,
        (anime_slug, user_id, username, avatar_color)
    )
    conn.commit()

    from datetime import datetime, timezone
    msg = {
        "id": message_id,
        "anime_slug": anime_slug,
        "user_id": user_id,
        "username": username,
        "avatar_color": avatar_color,
        "avatar": avatar,
        "kind": kind,
        "content": content,
        "reply_to": reply_to,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "reactions": [],
        "my_reactions": [],
    }

    if reply_to:
        cursor.execute(
            "SELECT username, content, kind FROM chat_messages WHERE id = ?",
            (reply_to,),
        )
        rrow = cursor.fetchone()
        if rrow:
            rc = dict(rrow)
            text = rc["content"] or ""
            if len(text) > 180:
                text = text[:180] + "…"
            msg["reply_to_username"] = rc["username"]
            msg["reply_to_content"] = text
            msg["reply_to_kind"] = rc["kind"]
        else:
            msg["reply_to_username"] = None
            msg["reply_to_content"] = None
            msg["reply_to_kind"] = None
    else:
        msg["reply_to_username"] = None
        msg["reply_to_content"] = None
        msg["reply_to_kind"] = None

    conn.close()
    return msg


def get_chat_message(message_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM chat_messages WHERE id = ?", (message_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_chat_messages(anime_slug, after_id=0, limit=200, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    if after_id:
        cursor.execute(
            """
            SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind,
                   u.avatar AS avatar
            FROM chat_messages m
            LEFT JOIN chat_messages r ON r.id = m.reply_to
            LEFT JOIN users u ON u.id = m.user_id
            WHERE m.anime_slug = ? AND m.id > ?
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (anime_slug, after_id, limit)
        )
    else:
        cursor.execute(
            """
            SELECT * FROM (
                SELECT m.*, r.username AS reply_username, r.content AS reply_content, r.kind AS reply_kind,
                       u.avatar AS avatar
                FROM chat_messages m
                LEFT JOIN chat_messages r ON r.id = m.reply_to
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.anime_slug = ?
                ORDER BY m.id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (anime_slug, limit)
        )
    rows = [dict(row) for row in cursor.fetchall()]
    for m in rows:
        if m.get("reply_username"):
            content = m.get("reply_content") or ""
            if len(content) > 180:
                content = content[:180] + "…"
            m["reply_to_username"] = m.pop("reply_username")
            m["reply_to_content"] = content
            m["reply_to_kind"] = m.pop("reply_kind")
        else:
            m["reply_to_username"] = None
            m["reply_to_content"] = None
            m["reply_to_kind"] = None
    # Inline reactions query on the SAME connection — avoids opening a
    # second Turso connection (~800 ms round-trip saved per poll).
    if rows and user_id:
        ids = [m["id"] for m in rows]
        marks = ",".join("?" * len(ids))
        cursor.execute(
            f"""
            SELECT message_id, emoji, COUNT(*) AS cnt
            FROM chat_reactions
            WHERE message_id IN ({marks})
            GROUP BY message_id, emoji
            ORDER BY cnt DESC
            """,
            ids,
        )
        grouped = {}
        for rr in cursor.fetchall():
            grouped.setdefault(rr["message_id"], []).append(
                {"emoji": rr["emoji"], "count": rr["cnt"]}
            )
        my = {}
        cursor.execute(
            f"""
            SELECT message_id, emoji
            FROM chat_reactions
            WHERE message_id IN ({marks}) AND user_id = ?
            """,
            ids + [user_id],
        )
        for rr in cursor.fetchall():
            my.setdefault(rr["message_id"]).append(rr["emoji"]) if rr["message_id"] in my else my.setdefault(rr["message_id"], [rr["emoji"]])
        for m in rows:
            m["reactions"] = grouped.get(m["id"], [])
            m["my_reactions"] = my.get(m["id"], [])
    elif rows:
        for m in rows:
            m["reactions"] = []
            m["my_reactions"] = []
    conn.close()
    return rows


def toggle_reaction(message_id, user_id, emoji):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM chat_reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
        (message_id, user_id, emoji),
    )
    existing = cursor.fetchone()
    reaction_id = None
    if existing:
        cursor.execute("DELETE FROM chat_reactions WHERE id = ?", (existing["id"],))
        added = False
    else:
        cursor.execute(
            "INSERT INTO chat_reactions (message_id, user_id, emoji) VALUES (?, ?, ?)",
            (message_id, user_id, emoji),
        )
        reaction_id = cursor.lastrowid
        added = True
    conn.commit()
    conn.close()
    return {"added": added, "reaction_id": reaction_id}


def get_message_reactions(message_id, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT emoji, COUNT(*) AS cnt
        FROM chat_reactions
        WHERE message_id = ?
        GROUP BY emoji
        ORDER BY cnt DESC
        """,
        (message_id,),
    )
    reactions = [{"emoji": r["emoji"], "count": r["cnt"]} for r in cursor.fetchall()]
    my = []
    if user_id:
        cursor.execute(
            "SELECT emoji FROM chat_reactions WHERE message_id = ? AND user_id = ?",
            (message_id, user_id),
        )
        my = [r["emoji"] for r in cursor.fetchall()]
    conn.close()
    return reactions, my


def get_reactions_since(after_id, limit=100, user_id=None):
    """Reactions inserted after a given id, with current counts (idempotent)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, message_id, user_id, emoji FROM chat_reactions WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    latest = max((r["id"] for r in rows), default=after_id)
    updates = []
    for r in rows:
        cursor.execute(
            "SELECT COUNT(*) AS cnt FROM chat_reactions WHERE message_id = ? AND emoji = ?",
            (r["message_id"], r["emoji"]),
        )
        updates.append({
            "message_id": r["message_id"],
            "emoji": r["emoji"],
            "count": cursor.fetchone()["cnt"],
            "mine": user_id is not None and r["user_id"] == user_id,
        })
    conn.close()
    return {"latest_id": latest, "updates": updates}


def get_chat_gifs(anime_slug, limit=150):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, username, content AS url, created_at
        FROM chat_messages
        WHERE anime_slug = ? AND kind = 'gif'
        ORDER BY id DESC
        LIMIT ?
        """,
        (anime_slug, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def touch_presence(anime_slug, user_id, username, avatar_color):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO chat_presence (anime_slug, user_id, username, avatar_color, last_seen)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(anime_slug, user_id)
        DO UPDATE SET last_seen = CURRENT_TIMESTAMP, username = excluded.username, avatar_color = excluded.avatar_color
        """,
        (anime_slug, user_id, username, avatar_color)
    )
    conn.commit()
    conn.close()


def get_online_users(anime_slug, active_seconds=60):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT p.username, p.avatar_color, u.avatar AS avatar
        FROM chat_presence p
        LEFT JOIN users u ON u.id = p.user_id
        WHERE p.anime_slug = ?
        AND datetime(p.last_seen) >= datetime('now', ?)
        ORDER BY p.last_seen DESC
        """,
        (anime_slug, f"-{active_seconds} seconds")
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


# ------------------------------------------------------------------
#  Community membership
# ------------------------------------------------------------------

def join_community(anime_slug, user_id):
    """Add a user to a community. Idempotent."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO community_members (anime_slug, user_id)
        VALUES (?, ?)
        """,
        (anime_slug, user_id),
    )
    conn.commit()
    joined = cursor.rowcount > 0
    conn.close()
    return joined


def leave_community(anime_slug, user_id):
    """Remove a user from a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM community_members WHERE anime_slug = ? AND user_id = ?",
        (anime_slug, user_id),
    )
    conn.commit()
    left = cursor.rowcount > 0
    conn.close()
    return left


def is_community_member(anime_slug, user_id):
    """Check if a user has joined a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM community_members WHERE anime_slug = ? AND user_id = ?",
        (anime_slug, user_id),
    )
    found = cursor.fetchone() is not None
    conn.close()
    return found


def get_community_members(anime_slug, limit=200):
    """Return the list of members who joined a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT u.username, u.avatar, u.avatar_color, cm.joined_at
        FROM community_members cm
        JOIN users u ON u.id = cm.user_id
        WHERE cm.anime_slug = ?
        ORDER BY cm.joined_at DESC
        LIMIT ?
        """,
        (anime_slug, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_community_member_count(anime_slug):
    """Return the number of members in a community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM community_members WHERE anime_slug = ?",
        (anime_slug,),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count


# Rating aggregates are re-rendered on every catalog page; cache them
# briefly so browse/home loads don't each re-scan the whole reviews table
# over the remote DB.
_STATS_CACHE_TTL = 10
_stats_cache = {"at": 0.0, "data": None}


def get_all_anime_stats():
    now = time.time()
    if now - _stats_cache["at"] < _STATS_CACHE_TTL and _stats_cache["data"] is not None:
        return _stats_cache["data"]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT anime_slug, COUNT(*) AS votes, AVG(rating) AS avg_rating
        FROM reviews
        GROUP BY anime_slug
        """
    )
    rows = cursor.fetchall()
    conn.close()
    result = {
        row["anime_slug"]: {
            "votes": row["votes"],
            "average": round(row["avg_rating"], 2) if row["avg_rating"] is not None else 0,
        }
        for row in rows
    }
    _stats_cache["at"] = time.time()
    _stats_cache["data"] = result
    return result


# Simple in-memory cache for anime stats (avoids repeated Turso queries)
_anime_stats_cache = {}
_anime_stats_cache_ttl = 120  # seconds
_anime_stats_cache_times = {}

# Cache for episode stats (per anime slug)
_episode_stats_cache = {}
_episode_stats_cache_ttl = 120  # seconds
_episode_stats_cache_times = {}

# Cache for global reviews feed
_all_reviews_cache = None
_all_reviews_cache_time = 0
_all_reviews_cache_ttl = 60  # seconds


# The /reviews feed builds 400 enriched review cards from ~14 sequential DB
# round trips (more with the episode tab's bulk lookups). On a remote
# database (Turso) that is most of the page's server time, so the fully
# assembled shared payload is cached for a few seconds and replayed with
# only the per-user bits (your votes, war status, rank) resolved per
# request. Votes / new reviews / new replies invalidate it immediately.
_reviews_feed_cache = None
_reviews_feed_cache_time = 0.0
_reviews_feed_cache_ttl = 25.0  # seconds (votes/reviews/replies invalidate it instantly anyway)


def reviews_feed_cache_get():
    """Return the cached shared /reviews payload or None."""
    if _reviews_feed_cache is None:
        return None
    if time.time() - _reviews_feed_cache_time >= _reviews_feed_cache_ttl:
        return None
    return _reviews_feed_cache


def reviews_feed_cache_put(payload):
    """Store the shared /reviews payload (reviews, episode_reviews, and the
    derived leaderboards)."""
    global _reviews_feed_cache, _reviews_feed_cache_time
    _reviews_feed_cache = payload
    _reviews_feed_cache_time = time.time()


def invalidate_reviews_feed_cache():
    """Drop the cached /reviews payload after a vote, review, reply, or war
    outcome changes the feed."""
    global _reviews_feed_cache
    _reviews_feed_cache = None

def get_anime_stats(anime_slug):
    now = time.time()
    cached = _anime_stats_cache.get(anime_slug)
    if cached and now - _anime_stats_cache_times.get(anime_slug, 0) < _anime_stats_cache_ttl:
        return cached
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rating, COUNT(*) as count FROM reviews WHERE anime_slug=? GROUP BY rating",
        (anime_slug,)
    )
    breakdown = {str(n): 0 for n in range(1, 11)}
    for row in cursor.fetchall():
        breakdown[str(row["rating"])] = row["count"]

    total_votes = sum(breakdown.values())
    cursor.execute(
        "SELECT AVG(rating) as avg_rating FROM reviews WHERE anime_slug=?",
        (anime_slug,)
    )
    avg_row = cursor.fetchone()
    average = round(avg_row["avg_rating"], 2) if avg_row["avg_rating"] is not None else 0

    cursor.execute(
        """
        SELECT r.id, r.username, r.rating, r.comment, r.created_at, r.user_id,
               u.avatar, u.avatar_color
        FROM reviews r
        LEFT JOIN users u ON u.id = r.user_id
        WHERE r.anime_slug=?
        ORDER BY r.id DESC
        """,
        (anime_slug,)
    )
    reviews = [
        {
            "username": row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "avatar": row["avatar"] if "avatar" in row.keys() else None,
            "avatar_color": row["avatar_color"] if "avatar_color" in row.keys() else None,
        }
        for row in cursor.fetchall()
    ]
    result = {
        "average": average,
        "votes": total_votes,
        "breakdown": breakdown,
        "reviews": reviews,
    }
    _anime_stats_cache[anime_slug] = result
    _anime_stats_cache_times[anime_slug] = time.time()
    return result


def _invalidate_anime_stats_cache(anime_slug):
    """Drop cached stats so new/deleted reviews show up immediately."""
    _anime_stats_cache.pop(anime_slug, None)
    _anime_stats_cache_times.pop(anime_slug, None)


def add_review(anime_slug, username, rating, comment, user_id=None):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO reviews (anime_slug, username, rating, comment, user_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (anime_slug, username or "Anonymous", rating, comment or "", user_id)
    )
    conn.commit()
    conn.close()
    _invalidate_anime_stats_cache(anime_slug)
    global _all_reviews_cache
    _all_reviews_cache = None

    if user_id:
        # Posting deserves XP (matches the recalc formula's +5 per review), and
        # fresh reviewers earn a one-time boost on their very first review so
        # they can climb out of D faster (marketed on the reviews page).
        recalculate_user_xp(user_id)
        if _count_user_reviews(user_id) == 1:
            add_xp(user_id, 40)


def _count_user_reviews(user_id):
    """Total anime + episode reviews a user has posted."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as c FROM reviews WHERE user_id=?", (user_id,))
    n = cursor.fetchone()["c"] or 0
    cursor.execute("SELECT COUNT(*) as c FROM episode_reviews WHERE user_id=?", (user_id,))
    n += cursor.fetchone()["c"] or 0
    conn.close()
    return n

def get_all_reviews(limit=200):
    """Return the most recent reviews across ALL anime (for the /reviews page)."""
    global _all_reviews_cache, _all_reviews_cache_time
    now = time.time()
    if _all_reviews_cache and now - _all_reviews_cache_time < _all_reviews_cache_ttl:
        return _all_reviews_cache
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.id, r.anime_slug, r.username, r.rating, r.comment,
               r.created_at, r.user_id, u.avatar, u.avatar_color,
               u.username AS u_username
        FROM reviews r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (limit,)
    )
    reviews = [
        {
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "username": row["u_username"] or row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "avatar": row["avatar"] if "avatar" in row.keys() else None,
            "avatar_color": row["avatar_color"] if "avatar_color" in row.keys() else None,
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    _all_reviews_cache = reviews
    _all_reviews_cache_time = now
    return reviews


_all_episode_reviews_cache = None
_all_episode_reviews_cache_time = 0.0
_all_episode_reviews_cache_ttl = 30.0


def get_all_episode_reviews(limit=200):
    """Return the most recent EPISODE reviews across ALL anime (for the
    Episode Reviews tab on /reviews). Cached like get_all_reviews.
    """
    global _all_episode_reviews_cache, _all_episode_reviews_cache_time
    now = time.time()
    if _all_episode_reviews_cache and now - _all_episode_reviews_cache_time < _all_episode_reviews_cache_ttl:
        return _all_episode_reviews_cache
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT r.id, r.anime_slug, r.season_name, r.episode_number,
               r.username, r.rating, r.comment, r.created_at, r.user_id,
               u.username AS u_username, u.avatar, u.avatar_color
        FROM episode_reviews r
        LEFT JOIN users u ON u.id = r.user_id
        ORDER BY r.id DESC
        LIMIT ?
        """,
        (limit,)
    )
    reviews = [
        {
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "season_name": row["season_name"],
            "episode_number": row["episode_number"],
            "username": row["u_username"] or row["username"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
            "user_id": row["user_id"],
            "avatar": row["avatar"],
            "avatar_color": row["avatar_color"],
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    _all_episode_reviews_cache = reviews
    _all_episode_reviews_cache_time = time.time()
    return reviews


def get_user_review(anime_slug, user_id):
    """Return the user's existing review for an anime, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, rating, comment, created_at FROM reviews "
        "WHERE anime_slug=? AND user_id=? LIMIT 1",
        (anime_slug, user_id),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "rating": row["rating"],
        "comment": row["comment"] or "",
        "created_at": row["created_at"],
    }


def delete_user_review(review_id, user_id):
    """Delete a review only if it belongs to the given user."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT anime_slug FROM reviews WHERE id=? AND user_id=?",
        (review_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        return False
    cursor.execute(
        "DELETE FROM reviews WHERE id=? AND user_id=?",
        (review_id, user_id),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        _invalidate_anime_stats_cache(row["anime_slug"])
        global _all_reviews_cache
        _all_reviews_cache = None
    return deleted

def add_review_reply(user_id, review_type, review_id, content):
    """Insert a reply on a review. Returns (ok, err, reply_dict)."""
    content = (content or "").strip()
    if len(content) < 2:
        return False, "Reply is too short.", None
    if len(content) > 500:
        return False, "Reply must be 500 characters or fewer.", None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO review_replies (review_id, review_type, user_id, content) VALUES (?, ?, ?, ?)",
        (review_id, review_type, user_id, content),
    )
    conn.commit()
    rid = cursor.lastrowid
    conn.close()
    rows = get_review_replies(review_type, [review_id])
    reply = rows.get(review_id, [])
    reply = next((rp for rp in reply if rp["id"] == rid), None)
    return True, None, reply


def get_review_replies(review_type, review_ids):
    """Return {review_id: [reply dicts]} ordered oldest-first, with the
    replier's username, avatar and current rank tier."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT r.id, r.review_id, r.user_id, r.content, r.created_at,
        u.username, u.avatar, u.avatar_color, ux.xp
        FROM review_replies r
        LEFT JOIN users u ON u.id = r.user_id
        LEFT JOIN user_xp ux ON ux.user_id = r.user_id
        WHERE r.review_type=? AND r.review_id IN ({placeholders})
        ORDER BY r.id ASC""",
        [review_type] + list(review_ids),
    )
    rows = cursor.fetchall()
    conn.close()
    out = {}
    for row in rows:
        out.setdefault(row["review_id"], []).append({
            "id": row["id"],
            "user_id": row["user_id"],
            "username": row["username"] or "user",
            "avatar": row["avatar"],
            "avatar_color": row["avatar_color"] or "#374151",
            "content": row["content"],
            "created_at": row["created_at"],
            "rank": get_xp_tier(row["xp"] or 0),
        })
    return out


WAR_DURATION_SECONDS = 24 * 60 * 60  # a war runs for 24h from the first entry
WAR_MIN_VOTES = 3  # an entry needs 3+ votes to be crowned / placed
# A war's outcome only moves the review's XP when it was DECISIVE — the
# winner needs at least this share of likes (a blowout, not a split room).
WAR_DECISIVE_RATIO = 0.75
# The review it bet on gains/loses this much Review XP once (a settled, blowout
# win is worth a bit more than a single like so it can actually matter).
WAR_OUTCOME_XP = 15
WAR_NEGATIVE_PENALTY_XP = WAR_OUTCOME_XP  # legacy alias for the retired settler


def _iso_to_ts(s):
    """SQLite CURRENT_TIMESTAMP strings are UTC "YYYY-MM-DD HH:MM:SS" -> epoch."""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def war_state(entries):
    """Given war-entry dicts, return (started_ts, ends_ts, status, leader,
    podium). status is 'live' or 'ended'; leader is the live leader (or
    None); podium is the final top-3 [{place, entry}] for ended wars.
    Winner = best like-ratio among entries with 3+ votes (tie -> most
    likes), exactly: 100 likes/50 dislikes loses to 75 likes/0 dislikes."""
    if not entries:
        return 0, 0, "ended", None, []
    started = min(e["created_at"] for e in entries)
    start_ts = _iso_to_ts(started)
    ends_ts = start_ts + WAR_DURATION_SECONDS
    status = "live" if time.time() < ends_ts else "ended"
    eligible = sorted(
        [e for e in entries if e["total"] >= WAR_MIN_VOTES],
        key=lambda e: (e["ratio"], e["likes"]),
        reverse=True,
    )
    if status == "live":
        return start_ts, ends_ts, status, (eligible[0] if eligible else None), []
    podium = [
        {"place": i + 1, **e}
        for i, e in enumerate(eligible[:3])
    ]
    return start_ts, ends_ts, status, None, podium


def _war_entry_dict(row, my_votes=None):
    likes = row["likes"] or 0
    dislikes = row["dislikes"] or 0
    total = likes + dislikes
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "username": row["username"] or "user",
        "avatar": row["avatar"],
        "avatar_color": row["avatar_color"] or "#374151",
        "rank": "S+" if is_dev_username(row["username"]) else get_xp_tier(row["xp"] or 0),
        "content": row["content"],
        "stance": (row["stance"] or "negative") if "stance" in row.keys() else "negative",
        "created_at": row["created_at"],
        "likes": likes,
        "dislikes": dislikes,
        "total": total,
        "ratio": round(likes / total, 4) if total else 0.0,
        "ratio_pct": round(likes / total * 100) if total else 0,
        "my_vote": my_votes.get(row["id"]) if my_votes else None,
        "rewarded": bool(row["rewarded"]),
    }


def add_war_entry(user_id, review_type, review_id, content, stance="negative"):
    """Post a reply-war entry on a review (one per user per review).

    stance: 'positive' (the reply defends the review) or 'negative' (it
    attacks it). Returns (ok, err, entry). Rank gating (C+ only) is
    enforced by the endpoint before calling this.
    """
    content = (content or "").strip()
    if len(content) < 2:
        return False, "Your war entry is too short.", None
    if len(content) > 500:
        return False, "War entries must be 500 characters or fewer.", None
    stance = stance if stance in ("positive", "negative") else "negative"
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id FROM reply_war WHERE user_id=? AND review_type=? AND review_id=?",
        (user_id, review_type, review_id),
    )
    if cursor.fetchone():
        conn.close()
        return False, "You already posted an entry in this war.", None
    cursor.execute(
        "INSERT INTO reply_war (review_id, review_type, user_id, content, stance) VALUES (?, ?, ?, ?, ?)",
        (review_id, review_type, user_id, content, stance),
    )
    conn.commit()
    eid = cursor.lastrowid
    conn.close()
    rows = get_war_entries(review_type, [review_id], user_id)
    entry = next((e for e in rows.get(review_id, {}).get("entries", []) if e["id"] == eid), None)
    return True, None, entry


def get_war_entries(review_type, review_ids, user_id=None):
    """Return {review_id: war dict} with entries, like/dislike counts, my
    vote and the live leader / ended podium.

    war dict = {"entries": [...], "started_at", "ends_at", "ends_ts",
    "status": 'live'|'ended', "leader": {...}|None, "podium": [...]}
    """
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT w.id, w.review_id, w.user_id, w.content, w.stance, w.rewarded, w.created_at,
        u.username, u.avatar, u.avatar_color, ux.xp,
        SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM reply_war w
        LEFT JOIN users u ON u.id = w.user_id
        LEFT JOIN user_xp ux ON ux.user_id = w.user_id
        LEFT JOIN review_likes rl ON rl.review_type='war' AND rl.review_id = w.id
        WHERE w.review_type=? AND w.review_id IN ({placeholders})
        GROUP BY w.id ORDER BY w.id ASC""",
        [review_type] + list(review_ids),
    )
    rows = cursor.fetchall()
    ids = [row["id"] for row in rows]
    my_votes = {}
    if user_id and ids:
        p2 = ",".join("?" * len(ids))
        cursor.execute(
            f"SELECT review_id, is_like FROM review_likes WHERE review_type='war' AND user_id=? AND review_id IN ({p2})",
            [user_id] + ids,
        )
        my_votes = {row["review_id"]: row["is_like"] for row in cursor.fetchall()}
    conn.close()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["review_id"], []).append(_war_entry_dict(row, my_votes))
    out = {}
    for rid, entries in grouped.items():
        start_ts, ends_ts, status, leader, podium = war_state(entries)
        out[rid] = {
            "review_type": review_type,
            "entries": entries,
            "battlers": len(entries),
            "started_at": entries[0]["created_at"],
            "started_ts": start_ts,
            "ends_ts": ends_ts,
            "status": status,
            "leader": leader,
            "podium": podium,
        }
    return out


def get_all_wars():
    """Every war across all reviews (one per review that has entries),
    hottest first: live wars on top, then by battler count.

    Returns [{review_type, review_id, battlers, status, started_at,
    started_ts, ends_ts, leader, podium}].
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT w.id, w.review_id, w.review_type, w.user_id, w.content, w.stance, w.rewarded, w.created_at,
        u.username, u.avatar, u.avatar_color, ux.xp,
        SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM reply_war w
        LEFT JOIN users u ON u.id = w.user_id
        LEFT JOIN user_xp ux ON ux.user_id = w.user_id
        LEFT JOIN review_likes rl ON rl.review_type='war' AND rl.review_id = w.id
        GROUP BY w.id ORDER BY w.id ASC""",
    )
    rows = cursor.fetchall()
    conn.close()
    grouped = {}
    for row in rows:
        grouped.setdefault((row["review_type"], row["review_id"]), []).append(
            _war_entry_dict(row)
        )
    wars = []
    for (rtype, rid), entries in grouped.items():
        start_ts, ends_ts, status, leader, podium = war_state(entries)
        wars.append({
            "review_type": rtype,
            "review_id": rid,
            "battlers": len(entries),
            "started_at": entries[0]["created_at"],
            "started_ts": start_ts,
            "ends_ts": ends_ts,
            "status": status,
            "leader": leader,
            "podium": podium,
        })
    # Hottest first: live wars above ended ones, then most battlers.
    wars.sort(key=lambda w: (0 if w["status"] == "live" else 1, -w["battlers"]))
    return wars


def war_is_live(review_type, review_id):
    """True while a review's war is still accepting votes (24h from the
    first entry). Wars with no entries are not live."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT MIN(created_at) as c FROM reply_war WHERE review_type=? AND review_id=?",
        (review_type, review_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row["c"]:
        return False
    return time.time() < _iso_to_ts(row["c"]) + WAR_DURATION_SECONDS


def get_war_entry_review(entry_id):
    """War votes close once the 24h clock runs out (vote gating)."""
    """Return (review_type, review_id) for a war entry, or None."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "SELECT review_type, review_id FROM reply_war WHERE id=?",
        (entry_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return row["review_type"], row["review_id"]


def settle_war_outcomes():
    """Settle every ended war's effect on the review it was fought over, once.

    Only a DECISIVE winner (>= WAR_DECISIVE_RATIO share of likes) moves the
    review's XP: a Negative blowout makes it lose WAR_OUTCOME_XP, a Positive
    blowout makes it gain WAR_OUTCOME_XP. Runs after reward_war_leaders;
    penalty_applied guards against double settles. (Supersedes the earlier
    apply_war_penalties; callers should use this.)"""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT w.id, w.review_id, w.review_type, w.stance,
            SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
            SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
            FROM reply_war w
            LEFT JOIN review_likes rl ON rl.review_type='war' AND rl.review_id = w.id
            WHERE w.rewarded=1 AND w.penalty_applied=0
            GROUP BY w.id"""
        )
        rows = cursor.fetchall()
        for row in rows:
            likes = row["likes"] or 0
            dislikes = row["dislikes"] or 0
            total = likes + dislikes
            ratio = likes / total if total else 0
            tbl = "reviews" if row["review_type"] == "anime" else "episode_reviews"
            # Decisive blowout only — a split room (closer than 75% likes)
            # leaves the review alone.
            if total >= WAR_MIN_VOTES and ratio >= WAR_DECISIVE_RATIO:
                if row["stance"] == "negative":
                    cursor.execute(
                        f"UPDATE {tbl} SET war_penalty = war_penalty + ? WHERE id = ?",
                        (WAR_OUTCOME_XP, row["review_id"]),
                    )
                elif row["stance"] == "positive":
                    cursor.execute(
                        f"UPDATE {tbl} SET war_bonus = war_bonus + ? WHERE id = ?",
                        (WAR_OUTCOME_XP, row["review_id"]),
                    )
            cursor.execute("UPDATE reply_war SET penalty_applied=1 WHERE id=?", (row["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()


def get_war_effects(review_type, review_ids):
    """Return {review_id: {"penalty": n, "bonus": n}} — the Review XP a
    review gained/lost to settled Reply War outcomes (0 when untouched)."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    tbl = "reviews" if review_type == "anime" else "episode_reviews"
    try:
        cursor.execute(
            f"SELECT id, war_penalty, war_bonus FROM {tbl} WHERE id IN ({placeholders})",
            list(review_ids),
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []
    conn.close()
    return {
        row["id"]: {"penalty": (row["war_penalty"] or 0), "bonus": (row["war_bonus"] or 0)}
        for row in rows
    }


def get_war_penalties(review_type, review_ids):
    """Return {review_id: war_penalty} for the given reviews (XP a review
    lost to a negative Reply War winner; 0 when untouched)."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    tbl = "reviews" if review_type == "anime" else "episode_reviews"
    try:
        cursor.execute(
            f"SELECT id, war_penalty FROM {tbl} WHERE id IN ({placeholders})",
            list(review_ids),
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []
    conn.close()
    return {row["id"]: (row["war_penalty"] or 0) for row in rows}


def apply_war_penalties():
    """One-time: when an ENDED war's rewarded winner took the NEGATIVE
    stance, the review it beat loses WAR_NEGATIVE_PENALTY_XP Review XP.
    Runs right after reward_war_leaders (which marks the winner rewarded=1);
    penalty_applied guards against double hits."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT w.id, w.review_id, w.review_type, w.stance
            FROM reply_war w
            WHERE w.rewarded=1 AND w.penalty_applied=0 AND w.stance='negative'"""
        )
        rows = cursor.fetchall()
        for row in rows:
            tbl = "reviews" if row["review_type"] == "anime" else "episode_reviews"
            cursor.execute(
                f"UPDATE {tbl} SET war_penalty = war_penalty + ? WHERE id = ?",
                (WAR_NEGATIVE_PENALTY_XP, row["review_id"]),
            )
            cursor.execute(
                "UPDATE reply_war SET penalty_applied=1 WHERE id=?", (row["id"],)
            )
        conn.commit()
    except Exception:
        conn.rollback()
    conn.close()


def migrate_replies_to_war():
    # War replies are stance-tagged (legacy dislike-takes are 'negative').
    """One-time migration: existing review replies become war entries so
    nothing already posted is lost when the War replaces the reply stream."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) as c FROM review_replies WHERE NOT EXISTS (SELECT 1 FROM reply_war w WHERE w.review_type=review_replies.review_type AND w.review_id=review_replies.review_id AND w.user_id=review_replies.user_id)"
        )
        _row = cursor.fetchone()
        n = _row["c"] if _row else 0
        if n:
            cursor.execute(
                """INSERT OR IGNORE INTO reply_war (review_id, review_type, user_id, content, created_at)
                SELECT review_id, review_type, user_id, content, created_at FROM review_replies"""
            )
            conn.commit()
    except Exception:
        conn.rollback()
    conn.close()


def reward_war_leaders():
    """Give a one-time +25 XP crown bonus to the WINNER of every war that
    has ended (24h up, 3+ votes, best like-ratio). The rewarded flag guards
    against double pays; a war pays its winner exactly once, at the end.

    A NEGATIVE-stance winner also lands a one-time Review XP penalty on the
    review it beat (the crowd's take trumps the review)."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """SELECT w.id, w.user_id, w.review_id, w.review_type, w.created_at,
            SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) as likes,
            SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) as dislikes
            FROM reply_war w
            LEFT JOIN review_likes rl ON rl.review_type='war' AND rl.review_id = w.id
            WHERE w.rewarded=0
            GROUP BY w.id"""
        )
        rows = cursor.fetchall()
        by_war = {}
        for row in rows:
            likes = row["likes"] or 0
            dislikes = row["dislikes"] or 0
            total = likes + dislikes
            if total < WAR_MIN_VOTES:
                continue
            ratio = likes / total
            key = (row["review_type"], row["review_id"])
            cur = by_war.get(key)
            if cur is None or (ratio, likes) > (cur[0], cur[1]):
                by_war[key] = (ratio, likes, row["id"], row["user_id"], row["created_at"])
        winners = []
        for (_, _, eid, uid, created) in by_war.values():
            # Only reward when the war has actually ended (24h from first entry).
            start_ts = _iso_to_ts(created)
            if time.time() >= start_ts + WAR_DURATION_SECONDS:
                winners.append((eid, uid))
        for eid, _ in winners:
            cursor.execute("UPDATE reply_war SET rewarded=1 WHERE id=?", (eid,))
        conn.commit()
        # Pay XP after the commit so add_xp's own connection doesn't lock.
        for _, uid in winners:
            add_xp(uid, 25)
    except Exception:
        conn.rollback()
    conn.close()


def episode_season_keys(season_name, season_index=None):
    """Both spellings one episode's season may be stored under in
    episode_reviews: the display name (e.g. 'Season 1', written by the
    form POST) and the raw index (e.g. '1', written by the AJAX rate
    endpoint). Reads must try both, otherwise reviews posted through
    the AJAX UI vanish on the next page load. Deduped tuple."""
    keys = []
    for k in (season_name, season_index):
        if k is None:
            continue
        k = str(k).strip()
        if k and k not in keys:
            keys.append(k)
    return tuple(keys) or ("",)


def add_episode_review(anime_slug, season_name, episode_number, user_id,
                       username, avatar_color, rating, comment,
                       season_index=None):
    conn = get_connection()
    cursor = conn.cursor()
    # Clear any previous review of this same episode written under the
    # OTHER season_name spelling (the AJAX endpoint stores the raw index,
    # the form POST stores the display name) so one episode never holds
    # two rows for the same user — the review is found and replaced under
    # either spelling.
    keys = episode_season_keys(season_name, season_index)
    cursor.execute(
        "DELETE FROM episode_reviews WHERE anime_slug=? AND episode_number=? AND user_id=? AND season_name IN (%s)"
        % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number, user_id] + list(keys)),
    )
    cursor.execute(
        """
        INSERT INTO episode_reviews
        (anime_slug, season_name, episode_number, user_id, username,
         avatar_color, rating, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, anime_slug, season_name, episode_number)
        DO UPDATE SET
            rating = excluded.rating,
            comment = excluded.comment,
            created_at = CURRENT_TIMESTAMP
        """,
        (anime_slug, season_name, episode_number, user_id,
         username or "Anonymous", avatar_color or "#00c16a",
         rating, comment or "")
    )
    conn.commit()
    invalidate_reviews_feed_cache()
    conn.close()
    _episode_stats_cache.pop(anime_slug, None)
    _episode_stats_cache_times.pop(anime_slug, None)


def delete_episode_review(anime_slug, season_name, episode_number, user_id,
                          season_index=None):
    """Delete an episode review only if it belongs to the given user.
    Matches either season_name spelling (display name or raw index)."""
    conn = get_connection()
    cursor = conn.cursor()
    keys = episode_season_keys(season_name, season_index)
    cursor.execute(
        "SELECT id FROM episode_reviews WHERE anime_slug=? AND episode_number=? AND user_id=? AND season_name IN (%s)"
        % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number, user_id] + list(keys)),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False
    cursor.execute(
        "DELETE FROM episode_reviews WHERE anime_slug=? AND episode_number=? AND user_id=? AND season_name IN (%s)"
        % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number, user_id] + list(keys)),
    )
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if deleted:
        _episode_stats_cache.pop(anime_slug, None)
        _episode_stats_cache_times.pop(anime_slug, None)
    return deleted


def get_episode_stats(anime_slug, season_name, episode_number, season_index=None):
    conn = get_connection()
    cursor = conn.cursor()
    keys = episode_season_keys(season_name, season_index)
    cursor.execute(
        """SELECT rating, COUNT(*) as count FROM episode_reviews
        WHERE anime_slug=? AND episode_number=? AND season_name IN (%s)
        GROUP BY rating""" % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number] + list(keys))
    )
    breakdown = {str(n): 0 for n in range(1, 11)}
    for row in cursor.fetchall():
        breakdown[str(row["rating"])] = row["count"]

    total_votes = sum(breakdown.values())
    cursor.execute(
        """SELECT AVG(rating) as avg_rating FROM episode_reviews
        WHERE anime_slug=? AND episode_number=? AND season_name IN (%s)""" % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number] + list(keys))
    )
    avg_row = cursor.fetchone()
    average = round(avg_row["avg_rating"], 1) if avg_row["avg_rating"] is not None else 0

    cursor.execute(
        """SELECT user_id, username, avatar_color, rating, comment, created_at
        FROM episode_reviews
        WHERE anime_slug=? AND episode_number=? AND season_name IN (%s)
        ORDER BY id DESC""" % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number] + list(keys))
    )
    reviews = [
        {
            "user_id": row["user_id"],
            "username": row["username"],
            "avatar_color": row["avatar_color"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        }
        for row in cursor.fetchall()
    ]
    result = {
        "average": average,
        "votes": total_votes,
        "breakdown": breakdown,
        "reviews": reviews,
    }
    _anime_stats_cache[anime_slug] = result
    _anime_stats_cache_times[anime_slug] = time.time()
    return result


def get_user_episode_review(anime_slug, season_name, episode_number, user_id,
                            season_index=None):
    if not user_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    keys = episode_season_keys(season_name, season_index)
    cursor.execute(
        """SELECT rating, comment FROM episode_reviews
        WHERE anime_slug=? AND episode_number=? AND user_id=? AND season_name IN (%s)
        ORDER BY id""" % ",".join("?" * len(keys)),
        tuple([anime_slug, episode_number, user_id] + list(keys))
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_episode_stats(anime_slug):
    now = time.time()
    cached = _episode_stats_cache.get(anime_slug)
    if cached and now - _episode_stats_cache_times.get(anime_slug, 0) < _episode_stats_cache_ttl:
        return cached
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT season_name, episode_number, AVG(rating) as avg_rating,
        COUNT(*) as votes
        FROM episode_reviews WHERE anime_slug=?
        GROUP BY season_name, episode_number""",
        (anime_slug,)
    )
    out = {}
    for row in cursor.fetchall():
        season = out.setdefault(row["season_name"], {})
        season[row["episode_number"]] = {
            "average": round(row["avg_rating"], 1) if row["avg_rating"] is not None else 0,
            "votes": row["votes"],
        }
    conn.close()
    _episode_stats_cache[anime_slug] = out
    _episode_stats_cache_times[anime_slug] = now
    return out


def save_quiz_result(user_id, answers, top_genres, result_slugs):
    import json

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO quiz_results (user_id, answers, top_genres, result_slugs)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_id,
            json.dumps(answers),
            json.dumps(top_genres),
            json.dumps(result_slugs),
        ),
    )
    conn.commit()
    conn.close()


def get_latest_quiz_result(user_id):
    import json

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT * FROM quiz_results
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["answers"] = json.loads(result["answers"])
    result["top_genres"] = json.loads(result["top_genres"])
    result["result_slugs"] = json.loads(result["result_slugs"])
    return result


# ---------------------------------------------------------------------------
# User anime lists (max 10, Crunchyroll-style) + view history
# ---------------------------------------------------------------------------

MAX_USER_LISTS = 10


def create_user_list(user_id, name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] >= MAX_USER_LISTS:
        conn.close()
        return None
    cursor.execute(
        "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
        (user_id, name),
    )
    conn.commit()
    list_id = cursor.lastrowid
    cursor.execute(
        "SELECT * FROM user_lists WHERE id = ?", (list_id,)
    )
    row = dict(cursor.fetchone())
    row["slugs"] = []
    conn.close()
    return row


def ensure_default_lists(user_id):
    """First time a user has any list access, seed the 10 default lists
    ("List 1" .. "List 10") so the add-to-list picker always has somewhere
    to put an anime. Users rename them into their own lists later."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] > 0:
        conn.close()
        return
    for i in range(1, MAX_USER_LISTS + 1):
        cursor.execute(
            "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
            (user_id, f"List {i}"),
        )
    conn.commit()
    conn.close()


def ensure_and_get_lists(user_id):
    """Seed default lists if needed AND return them — one DB connection.

    The old path opened two separate Turso connections (ensure → close →
    get → close), costing ~1.6 s of round-trip latency on every list
    picker open. This fuses them into a single connection.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS n FROM user_lists WHERE user_id = ?", (user_id,))
    if cursor.fetchone()["n"] == 0:
        for i in range(1, MAX_USER_LISTS + 1):
            cursor.execute(
                "INSERT INTO user_lists (user_id, name) VALUES (?, ?)",
                (user_id, f"List {i}"),
            )
        conn.commit()

    # Now fetch lists in the same connection.
    cursor.execute(
        """
        SELECT l.id, l.name, l.created_at, l.updated_at, i.anime_slug
        FROM user_lists l
        LEFT JOIN user_list_items i ON i.list_id = l.id
        WHERE l.user_id = ?
        ORDER BY l.updated_at DESC, l.id ASC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    by_id, order = {}, []
    for r in rows:
        lst_id = r["id"]
        if lst_id not in by_id:
            by_id[lst_id] = {
                "id": lst_id,
                "name": r["name"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "slugs": [],
            }
            order.append(lst_id)
        slug = r["anime_slug"]
        if slug:
            by_id[lst_id]["slugs"].append(slug)
    return [by_id[i] for i in order]


def get_user_lists(user_id):
    """All of a user's lists with their anime slugs attached.

    One LEFT JOIN query instead of one query per list: the remote Turso DB
    is hit over HTTP, so the old per-list loop cost N+1 network round trips
    every time the "Add to List" picker opened.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT l.id, l.name, l.created_at, l.updated_at, i.anime_slug
        FROM user_lists l
        LEFT JOIN user_list_items i ON i.list_id = l.id
        WHERE l.user_id = ?
        ORDER BY l.updated_at DESC, l.id ASC
        """,
        (user_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    by_id, order = {}, []
    for r in rows:
        lst_id = r["id"]
        if lst_id not in by_id:
            by_id[lst_id] = {
                "id": lst_id,
                "name": r["name"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "slugs": [],
            }
            order.append(lst_id)
        slug = r["anime_slug"]
        if slug:
            by_id[lst_id]["slugs"].append(slug)
    return [by_id[i] for i in order]


def get_user_list(list_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    lst = dict(row)
    cursor.execute(
        "SELECT anime_slug FROM user_list_items WHERE list_id = ?",
        (lst["id"],),
    )
    lst["slugs"] = [r["anime_slug"] for r in cursor.fetchall()]
    conn.close()
    return lst


def rename_user_list(list_id, user_id, name):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        UPDATE user_lists SET name = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
        """,
        (name, list_id, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def delete_user_list(list_id, user_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_list_items WHERE list_id = ?",
        (list_id,),
    )
    cursor.execute(
        "DELETE FROM user_lists WHERE id = ? AND user_id = ?",
        (list_id, user_id),
    )
    conn.commit()
    changed = cursor.rowcount > 0
    conn.close()
    return changed


def _touch_list(list_id, cursor):
    cursor.execute(
        "UPDATE user_lists SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (list_id,),
    )


def add_to_user_list(list_id, user_id, anime_slug):
    """Add an anime to a list owned by the user.

    Returns True if newly added, False if it was already in the list, or
    None if the list doesn't exist / isn't owned by the user.

    The ownership check is folded into the INSERT (SELECT ... WHERE EXISTS)
    so the common case costs one remote round trip instead of three.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT OR IGNORE INTO user_list_items (list_id, anime_slug)
        SELECT ?, ?
        WHERE EXISTS (SELECT 1 FROM user_lists WHERE id = ? AND user_id = ?)
        """,
        (list_id, anime_slug, list_id, user_id),
    )
    inserted = cursor.rowcount
    if inserted == 0:
        # rowcount 0 means duplicate row OR unowned list; disambiguate once.
        cursor.execute(
            "SELECT id FROM user_lists WHERE id = ? AND user_id = ?",
            (list_id, user_id),
        )
        if not cursor.fetchone():
            conn.close()
            return None
    _touch_list(list_id, cursor)
    conn.commit()
    conn.close()
    return bool(inserted)


def remove_from_user_list(list_id, user_id, anime_slug):
    """Remove an anime from a list the user owns. Returns True when a row
    was actually deleted, False otherwise (not in the list or not owned)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        DELETE FROM user_list_items
        WHERE anime_slug = ?
          AND list_id IN (SELECT id FROM user_lists WHERE id = ? AND user_id = ?)
        """,
        (anime_slug, list_id, user_id),
    )
    removed = cursor.rowcount > 0
    if removed:
        _touch_list(list_id, cursor)
    conn.commit()
    conn.close()
    return removed


def record_view(user_id, anime_slug):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO view_history (user_id, anime_slug, viewed_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, anime_slug)
        DO UPDATE SET viewed_at = CURRENT_TIMESTAMP
        """,
        (user_id, anime_slug),
    )
    conn.commit()
    conn.close()


def get_taste_slugs(user_id, limit=80):
    """Slugs the user has shown interest in, most recent first.

    Merges view history with everything saved in their lists in a single
    query/connection so the homepage stays one round trip. List entries
    count as a stronger signal than a passive view, which the caller uses
    to weight genres.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT anime_slug, MAX(at) AS at, MAX(saved) AS saved FROM (
            SELECT h.anime_slug AS anime_slug, h.viewed_at AS at, 0 AS saved
            FROM view_history h
            WHERE h.user_id = ?
            UNION ALL
            SELECT i.anime_slug AS anime_slug, i.added_at AS at, 1 AS saved
            FROM user_list_items i
            JOIN user_lists l ON l.id = i.list_id
            WHERE l.user_id = ?
        )
        GROUP BY anime_slug
        ORDER BY at DESC
        LIMIT ?
        """,
        (user_id, user_id, limit),
    )
    rows = [{"slug": r["anime_slug"], "saved": bool(r["saved"])} for r in cursor.fetchall()]
    conn.close()
    return rows


_query_cache = {}
_query_cache_time = {}
_QUERY_CACHE_TTL = 60  # seconds

def _cached_query(key, sql, params=(), one=False):
    """Run a query with a short TTL cache to avoid repeated Turso round-trips."""
    now = time.time()
    if key in _query_cache and now - _query_cache_time.get(key, 0) < _QUERY_CACHE_TTL:
        return _query_cache[key]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(sql, params)
    result = cursor.fetchone() if one else cursor.fetchall()
    _query_cache[key] = result
    _query_cache_time[key] = now
    return result

def get_history_count(user_id):
    """Fast COUNT(*) for the profile header."""
    row = _cached_query(
        f"hcount:{user_id}",
        "SELECT COUNT(*) AS n FROM view_history WHERE user_id = ?",
        (user_id,),
        one=True,
    )
    return row["n"] if row else 0


def get_view_history(user_id, limit=60):
    key = f"vhist:{user_id}:{limit}"
    now = time.time()
    if key in _query_cache and now - _query_cache_time.get(key, 0) < _QUERY_CACHE_TTL:
        return _query_cache[key]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT anime_slug, viewed_at FROM view_history
        WHERE user_id = ?
        ORDER BY viewed_at DESC
        LIMIT ?
        """,
        (user_id, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    _query_cache[key] = rows
    _query_cache_time[key] = now
    return rows


def get_all_community_member_counts():
    """Return {slug: count} for every community that has members."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT anime_slug, COUNT(*) AS cnt FROM community_members GROUP BY anime_slug"
    )
    result = {row["anime_slug"]: row["cnt"] for row in cursor.fetchall()}
    conn.close()
    return result


# ------------------------------------------------------------------
#  Site-wide real stats (for homepage / community hero sections)
# ------------------------------------------------------------------

def get_site_stats():
    """Return real counts: total_users, total_messages, total_communities."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    total_users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM chat_messages")
    total_messages = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT anime_slug) FROM chat_messages")
    total_communities = cursor.fetchone()[0]
    conn.close()
    return {
        "total_users": total_users,
        "total_messages": total_messages,
        "total_communities": total_communities,
    }


def get_community_chat_stats(anime_slug):
    """Return real member + message counts for one community."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE anime_slug = ?",
        (anime_slug,),
    )
    message_count = cursor.fetchone()[0]
    conn.close()
    member_count = get_community_member_count(anime_slug)
    return {"message_count": message_count, "member_count": member_count}

def insert_system_message(anime_slug, content, user_id=1):
    """Insert a system message (e.g. 'X joined the community') into chat."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO chat_messages (anime_slug, user_id, username, avatar_color, kind, content)
        VALUES (?, ?, 'System', '#6b7280', 'system', ?)
        """,
        (anime_slug, user_id, content),
    )
    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


# ================================================================
#  RANKINGS — Show & Episode Leaderboards
# ================================================================

def get_show_rankings(limit=50, genre=None, season=None):
    """Return top anime ranked by average episode rating."""
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT
            er.anime_slug,
            COUNT(DISTINCT er.id) AS total_reviews,
            AVG(er.rating) AS avg_rating
        FROM episode_reviews er
        GROUP BY er.anime_slug
        HAVING total_reviews >= 1
    """
    params = []
    if genre:
        query += " HAVING total_reviews >= 1 AND er.anime_slug IN (SELECT slug FROM anime_list WHERE genre LIKE ?)"
        params.append(f"%{genre}%")

    query += " ORDER BY avg_rating DESC, total_reviews DESC"
    query += " LIMIT ?"
    params.append(limit)

    cursor.execute(query, params)
    rows = cursor.fetchall()

    results = []
    for row in rows:
        slug = row["anime_slug"]
        entry = anime_database.get(slug)
        if not entry:
            continue
        results.append({
            "slug": slug,
            "title": entry.get("title") or slug,
            "image": entry.get("image") or "",
            "genre": entry.get("genre") or "",
            "rating": entry.get("rating") or "N/A",
            "avg_rating": round(row["avg_rating"], 1),
            "total_reviews": row["total_reviews"],
        })
    conn.close()
    return results


def get_episode_rankings_for_anime(anime_slug, limit=20):
    """Return top-rated episodes for a specific anime."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT season_name, episode_number, AVG(rating) AS avg_rating, COUNT(*) AS total_votes
        FROM episode_reviews
        WHERE anime_slug = ?
        GROUP BY season_name, episode_number
        HAVING total_votes >= 1
        ORDER BY avg_rating DESC, total_votes DESC
        LIMIT ?""",
        (anime_slug, limit),
    )
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "season_name": r["season_name"],
            "episode_number": r["episode_number"],
            "avg_rating": round(r["avg_rating"], 1),
            "total_votes": r["total_votes"],
        }
        for r in rows
    ]


def get_user_episode_rating(user_id, anime_slug, season_name, episode_number):
    """Return the user's existing rating for an episode, or None."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT rating FROM episode_reviews
        WHERE user_id=? AND anime_slug=? AND season_name=? AND episode_number=?""",
        (user_id, anime_slug, season_name, episode_number),
    )
    row = cursor.fetchone()
    conn.close()
    return row["rating"] if row else None


def rate_episode(user_id, username, avatar_color, anime_slug, season_name, episode_number, rating):
    """Insert or update a user's episode rating."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id FROM episode_reviews
        WHERE user_id=? AND anime_slug=? AND season_name=? AND episode_number=?""",
        (user_id, anime_slug, season_name, episode_number),
    )
    existing = cursor.fetchone()
    if existing:
        cursor.execute(
            "UPDATE episode_reviews SET rating=? WHERE id=?",
            (rating, existing["id"]),
        )
    else:
        cursor.execute(
            """INSERT INTO episode_reviews
            (anime_slug, season_name, episode_number, user_id, username, avatar_color, rating)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (anime_slug, season_name, episode_number, user_id, username, avatar_color, rating),
        )
    conn.commit()
    conn.close()


# ================================================================
#  XP & RANK SYSTEM
# ================================================================

# Rank thresholds: XP required to reach each rank
RANK_THRESHOLDS = {
    "F": -1,    # Below 0 XP
    "D": 0,     # New users start here
    "C": 500,
    "B": 1000,
    "A": 2000,
    "S": 5000,
    "S+": 15000,
}

def get_xp_tier(xp):
    """Return the rank tier string for a given XP value."""
    if xp >= 15000:
        return "S+"
    elif xp >= 5000:
        return "S"
    elif xp >= 2000:
        return "A"
    elif xp >= 1000:
        return "B"
    elif xp >= 500:
        return "C"
    elif xp >= 0:
        return "D"
    else:
        return "F"

# Rank boundaries: (lower_threshold, upper_threshold)
_RANK_RANGES = {
    "F": (-999, 0),
    "D": (0, 500),
    "C": (500, 1000),
    "B": (1000, 2000),
    "A": (2000, 5000),
    "S": (5000, 15000),
    "S+": (15000, 15000),
}

def xp_progress(xp):
    """Return (rank, progress_pct) where progress is 0-100 toward the next rank."""
    rank = get_xp_tier(xp)
    lo, hi = _RANK_RANGES.get(rank, (0, 100))
    if hi <= lo:
        return rank, 100
    pct = int(min(100, max(0, (xp - lo) / (hi - lo) * 100)))
    return rank, pct


def _received_vote_totals(cur, user_id):
    """Sum likes/dislikes RECEIVED on a user's own anime and episode
    reviews (review_likes rows pointing at their reviews), not votes the
    user cast. Returns (likes, dislikes).

    review_likes stores review_type + review_id where 'anime' maps to
    reviews.id and 'episode' maps to episode_reviews.id; episode_reviews
    carries an id column for that join.
    """
    try:
        cur.execute(
            """
            SELECT
                SUM(CASE WHEN rl.is_like=1 THEN 1 ELSE 0 END) AS likes,
                SUM(CASE WHEN rl.is_like=0 THEN 1 ELSE 0 END) AS dislikes
            FROM (
                SELECT id FROM reviews WHERE user_id = ?
                UNION ALL
                SELECT id FROM episode_reviews WHERE user_id = ?
                UNION ALL
                SELECT id FROM war_entries WHERE user_id = ?
            ) mine
            JOIN review_likes rl ON rl.review_id = mine.id
            WHERE rl.review_type IN ('anime', 'episode', 'warzone')
            """,
            (user_id, user_id, user_id),
        )
        row = cur.fetchone()
        likes = int(row["likes"] or 0)
        dislikes = int(row["dislikes"] or 0)
        return likes, dislikes
    except Exception:
        return 0, 0



def _compute_xp(user_id):
    """Compute XP live from review votes + reviews posted (same formula as
    recalculate_user_xp, but read-only). Keeps profiles from showing 0 XP
    when the user_xp cache row hasn't been written yet."""
    conn = get_connection()
    cursor = conn.cursor()
    try:
        likes, dislikes = _received_vote_totals(cursor, user_id)
        total = likes + dislikes
        cursor.execute("SELECT COUNT(*) as cnt FROM reviews WHERE user_id = ?", (user_id,))
        review_count = cursor.fetchone()["cnt"] or 0
        cursor.execute("SELECT COUNT(*) as cnt FROM episode_reviews WHERE user_id = ?", (user_id,))
        ep_review_count = cursor.fetchone()["cnt"] or 0
        total_reviews = review_count + ep_review_count
        if total > 0:
            ratio = likes / total
            xp = 100 + int(ratio * total * 25) + (total_reviews * 5)
        else:
            xp = 100 + (total_reviews * 5)
        return xp
    finally:
        conn.close()


def get_user_xp(user_id):
    """Get a user's current XP. Falls back to a live computation so a
    missing user_xp row never shows 0 XP on a profile."""
    row = _cached_query(
        f"xp:{user_id}",
        "SELECT xp FROM user_xp WHERE user_id=?",
        (user_id,),
        one=True,
    )
    if row and row["xp"]:
        return row["xp"]
    try:
        computed = _compute_xp(user_id)
        return computed if computed else 100
    except Exception:
        return 100


def get_user_rank(user_id):
    """Return the rank tier string for a user."""
    return get_xp_tier(get_user_xp(user_id))


def add_xp(user_id, amount):
    """Add (or subtract) XP for a user. Creates record if needed."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT xp FROM user_xp WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if row:
        new_xp = row["xp"] + amount
        cursor.execute("UPDATE user_xp SET xp=? WHERE user_id=?", (new_xp, user_id))
    else:
        cursor.execute("INSERT INTO user_xp (user_id, xp) VALUES (?, ?)", (user_id, amount))
    conn.commit()
    conn.close()


def _get_war_reward_pool(conn, user_id):
    """Banked war-reward XP (war_reward_xp) for a user, 0 if none/absent."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT war_reward_xp FROM user_xp WHERE user_id=?", (user_id,))
        row = cur.fetchone()
        return (row["war_reward_xp"] or 0) if row else 0
    except Exception:
        return 0


def recalculate_user_xp(user_id):
    """Recalculate XP based on like/dislike ratio across ALL reviews.

    XP = base (100) + ratio * total_votes * 10
    where ratio = likes / (likes + dislikes)

    The banked war-reward pool is ALWAYS preserved: wars credit both the
    live `xp` column and a separate `war_reward_xp` record, and this
    recompute folds the pool back in so posting a review / a vote swing /
    a startup recalc can never silently strip earned war rank (a plain
    recalc used to overwrite xp and demote S-rank war winners to D).
    """
    conn = get_connection()
    cursor = conn.cursor()
    pool = _get_war_reward_pool(conn, user_id)
    # Count likes/dislikes RECEIVED on this user's own anime + episode
    # reviews. (The old query filtered review_likes by user_id — votes the
    # user cast on OTHER people's reviews — so a reviewer's rank never grew
    # from being liked and voters were rewarded for casting instead.)
    likes, dislikes = _received_vote_totals(cursor, user_id)
    total = likes + dislikes
    # Count reviews posted (for posting bonus)
    cursor.execute("SELECT COUNT(*) as cnt FROM reviews WHERE user_id = ?", (user_id,))
    rev_row = cursor.fetchone()
    review_count = rev_row["cnt"] if rev_row else 0
    # Also count episode reviews
    cursor.execute("SELECT COUNT(*) as cnt FROM episode_reviews WHERE user_id = ?", (user_id,))
    ep_rev_row = cursor.fetchone()
    ep_review_count = ep_rev_row["cnt"] if ep_rev_row else 0
    total_reviews = review_count + ep_review_count
    if total > 0:
        ratio = likes / total
        xp = 100 + int(ratio * total * 25) + (total_reviews * 5)  # Base 100 + 25 per vote + 5 per review posted
    else:
        xp = 100 + (total_reviews * 5)  # Base 100 + 5 per review posted
    xp += pool  # the banked war-reward pool rides on top of every recompute
    # Update or insert
    cursor.execute("SELECT xp FROM user_xp WHERE user_id=?", (user_id,))
    existing = cursor.fetchone()
    if existing:
        cursor.execute("UPDATE user_xp SET xp=? WHERE user_id=?", (xp, user_id))
    else:
        cursor.execute("INSERT INTO user_xp (user_id, xp, war_reward_xp) VALUES (?, ?, ?)", (user_id, xp, pool))
    conn.commit()
    conn.close()
    return xp


def get_all_user_ranks(user_ids):
    """Return {user_id: {xp, rank}} for a list of user IDs.

    Every requested user gets an entry -- those without a user_xp row
    default to 100 XP / rank D so badges always render. Developer
    accounts are pinned to S+ (15000 XP) like every other rank display
    (profile, reviews, threads mini-profile), so thread badges never
    show a stale D/B for dev usernames.
    """
    if not user_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(user_ids))
    cursor.execute(f"SELECT user_id, xp FROM user_xp WHERE user_id IN ({placeholders})", user_ids)
    xp_map = {row["user_id"]: row["xp"] for row in cursor.fetchall()}
    # One batched lookup for dev usernames (S+ always), instead of N queries.
    dev_ids = set()
    try:
        cursor.execute(f"SELECT id, username FROM users WHERE id IN ({placeholders})", user_ids)
        for row in cursor.fetchall():
            if is_dev_username(row["username"]):
                dev_ids.add(row["id"])
    except Exception:
        pass
    conn.close()
    result = {}
    for uid in user_ids:
        xp = xp_map.get(uid, 0)
        if not xp:
            xp = 100
        if uid in dev_ids:
            xp = 15000
        result[uid] = {"xp": xp, "rank": get_xp_tier(xp)}
    return result


def toggle_review_like(user_id, review_type, review_id, is_like):
    """Toggle a like/dislike on a review. Returns (new_is_like, removed)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, is_like FROM review_likes WHERE user_id=? AND review_type=? AND review_id=?",
        (user_id, review_type, review_id),
    )
    existing = cursor.fetchone()

    # Find the review author to adjust XP
    review_author_id = None
    if review_type == "episode":
        cursor.execute("SELECT user_id FROM episode_reviews WHERE id=?", (review_id,))
        r = cursor.fetchone()
        if r:
            review_author_id = r["user_id"]
    elif review_type == "anime":
        cursor.execute("SELECT user_id FROM reviews WHERE id=?", (review_id,))
        r = cursor.fetchone()
        if r:
            review_author_id = r["user_id"]

    removed = False
    new_is_like = is_like

    if existing:
        if existing["is_like"] == is_like:
            # Same vote → remove it
            cursor.execute("DELETE FROM review_likes WHERE id=?", (existing["id"],))
            removed = True
            if review_author_id:
                recalculate_user_xp(review_author_id)  # Undo: reverse the penalty/bonus
        else:
            # Different vote → switch
            cursor.execute("UPDATE review_likes SET is_like=? WHERE id=?", (is_like, existing["id"]))
            if review_author_id:
                recalculate_user_xp(review_author_id)  # Swing from dislike to like or vice versa
    else:
        # New vote
        cursor.execute(
            "INSERT INTO review_likes (user_id, review_type, review_id, is_like) VALUES (?, ?, ?, ?)",
            (user_id, review_type, review_id, is_like),
        )
        if review_author_id:
            recalculate_user_xp(review_author_id)

    conn.commit()
    invalidate_reviews_feed_cache()
    conn.close()
    return new_is_like, removed


def get_review_likes(review_type, review_id):
    """Return {likes: N, dislikes: N, user_vote: 1|-1|0} for a review."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes, "
        "SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes "
        "FROM review_likes WHERE review_type=? AND review_id=?",
        (review_type, review_id),
    )
    row = cursor.fetchone()
    conn.close()
    return {
        "likes": row["likes"] or 0,
        "dislikes": row["dislikes"] or 0,
    }


def get_bulk_review_likes(review_type, review_ids):
    """Return {review_id: {likes, dislikes}} for multiple reviews."""
    if not review_ids:
        return {}
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join("?" * len(review_ids))
    cursor.execute(
        f"""SELECT review_id,
        SUM(CASE WHEN is_like=1 THEN 1 ELSE 0 END) as likes,
        SUM(CASE WHEN is_like=0 THEN 1 ELSE 0 END) as dislikes
        FROM review_likes WHERE review_type=? AND review_id IN ({placeholders})
        GROUP BY review_id""",
        [review_type] + list(review_ids),
    )
    result = {}
    for row in cursor.fetchall():
        result[row["review_id"]] = {"likes": row["likes"] or 0, "dislikes": row["dislikes"] or 0}
    conn.close()
    return result


def get_user_review_history(user_id, limit=50):
    """Return all reviews by a user (episode + anime)."""
    conn = get_connection()
    cursor = conn.cursor()
    reviews = []
    # Episode reviews
    cursor.execute(
        "SELECT id, anime_slug, season_name, episode_number, rating, comment, created_at "
        "FROM episode_reviews WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    for row in cursor.fetchall():
        entry = anime_database.get(row["anime_slug"])
        reviews.append({
            "type": "episode",
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "anime_title": (entry.get("title") if entry else None) or row["anime_slug"],
            "season_name": row["season_name"],
            "episode_number": row["episode_number"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        })
    # Anime reviews
    cursor.execute(
        "SELECT id, anime_slug, rating, comment, created_at "
        "FROM anime_ratings WHERE user_id=? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    for row in cursor.fetchall():
        entry = anime_database.get(row["anime_slug"])
        reviews.append({
            "type": "anime",
            "id": row["id"],
            "anime_slug": row["anime_slug"],
            "anime_title": (entry.get("title") if entry else None) or row["anime_slug"],
            "rating": row["rating"],
            "comment": row["comment"] or "",
            "created_at": row["created_at"],
        })
    conn.close()
    reviews.sort(key=lambda r: r["created_at"] or "", reverse=True)
    return reviews[:limit]


def set_profile_public(user_id, is_public):
    """Set a user profile visibility."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_public=? WHERE id=?", (1 if is_public else 0, user_id))
    conn.commit()
    conn.close()


# Ota-chan AI assistant (one persistent chat per user)
# -------------------------------------------------------------------

def get_ota_chan_chat(user_id):
    """Return the user's Ota-chan chat row, or None."""
    conn = get_connection()
    row = conn.execute(
        "SELECT user_id, conversation_history, created_at, updated_at "
        "FROM ota_chan_chat WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def set_ota_chan_chat(user_id, conversation_history_json):
    """Create or update the user's Ota-chan conversation."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT 1 FROM ota_chan_chat WHERE user_id = ?",
        (user_id,),
    )
    if cursor.fetchone():
        cursor.execute(
            "UPDATE ota_chan_chat SET conversation_history = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (conversation_history_json, user_id),
        )
    else:
        cursor.execute(
            "INSERT INTO ota_chan_chat (user_id, conversation_history) VALUES (?, ?)",
            (user_id, conversation_history_json),
        )
    conn.commit()
    conn.close()
