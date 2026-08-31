"""Server-side gate: dislikes require C rank (500 XP).

D-rank accounts can only like (dislikes AND replies require C rank), so a
fresh-account mob has no dislike weapon. This wraps database.toggle_review_like
-- the single choke point behind /api/review/<id>/vote -- and is applied at
import time, before app.py binds its `from database import ...` names, so the
bound toggle_review_like is already gated everywhere it is called.
"""

import database

_ORIG_TOGGLE = database.toggle_review_like


def _gated_toggle(user_id, review_type, review_id, is_like):
    if not is_like and database.get_xp_tier(database.get_user_xp(user_id)) == "D":
        raise PermissionError(
            "Dislikes require C rank (500 XP) - D-rank accounts can only like."
        )
    return _ORIG_TOGGLE(user_id, review_type, review_id, is_like)


def apply_review_vote_gate():
    database.toggle_review_like = _gated_toggle
