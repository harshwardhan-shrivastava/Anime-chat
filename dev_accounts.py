"""Developer accounts on Otakul.

These usernames get a Developer tag next to their badges and are always
treated as S+ rank (15000 XP) so the team can test every feature on any
environment (the XP lives in each environment's own DB, so a DB bump in
one sandbox wouldn't reach production).
"""

DEV_USERNAMES = {"kakkarot69", "kageyama"}


def is_dev_username(username):
    return bool(username) and str(username).strip().lower() in DEV_USERNAMES
