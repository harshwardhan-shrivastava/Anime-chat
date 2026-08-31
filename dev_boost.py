"""Runtime patch: developer accounts are always S+ (15000 XP).

Every gate and display (war posting, dislikes, replies, vote pricing,
profile rank, xp bars) flows through database.get_user_xp. Wrapping it
means dev accounts unlock everything on ANY environment -- no per-DB XP
bump needed, which is why the earlier local-only bump didn't show up on
Render.
"""

import database
from dev_accounts import is_dev_username

_ORIG = database.get_user_xp


def _boosted(user_id):
    try:
        user = database.get_user_by_id(user_id)
        if user and is_dev_username(user.get("username")):
            return 15000
    except Exception:
        pass
    return _ORIG(user_id)


def apply_dev_boost():
    database.get_user_xp = _boosted
