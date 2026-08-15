"""
Sends the email-verification code when someone signs up or resets a
password.

If real SMTP credentials are configured via environment variables, a real
email goes out. If they're not configured yet, we don't block testing --
we log the code to the console and to logs/emails.log, and return it so
the verify page can show it directly on screen while you're setting real
SMTP up.

Environment variables (put these in a .env file or your host's config,
see .env.example):

    SMTP_HOST        e.g. smtp.gmail.com
    SMTP_PORT        e.g. 465
    SMTP_USER        the mailbox username / address to send from
    SMTP_PASS        the mailbox password or app password
    MAIL_FROM        the "From" address shown to recipients (defaults to SMTP_USER)
"""

import os
import smtplib
import ssl
from email.mime.text import MIMEText

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "emails.log")


def _log_dev_code(to_email, code):
    print(f"[AnimeChat][DEV MAIL] Verification code for {to_email}: {code}")

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{to_email} | {code}\n")
    except OSError:
        pass


def send_verification_email(to_email, username, code, purpose="verify"):
    """Sends a 6-digit verification code.

    purpose is "verify" (signup) or "reset" (forgot password) -- it only
    changes the email wording.

    Returns a dict: {"sent": bool, "dev_code": str|None}.

    dev_code is only set when SMTP isn't configured, so the caller can
    surface it on the "enter your code" page for easy local testing.
    """

    host = os.environ.get("SMTP_HOST")

    if not host:
        _log_dev_code(to_email, code)
        return {"sent": False, "dev_code": code}

    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("MAIL_FROM", user)

    if purpose == "reset":
        subject = "AnimeChat password reset code"
        body = (
            f"Hey {username},\n\n"
            f"We got a request to reset your AnimeChat password. Your "
            f"6-digit code is:\n\n"
            f"   {code}\n\n"
            f"This code expires in 5 minutes. If you didn't request a "
            f"password reset, just ignore this email.\n\n"
            f"-- AnimeChat"
        )
    else:
        subject = "Your AnimeChat verification code"
        body = (
            f"Hey {username},\n\n"
            f"Welcome to AnimeChat! Your 6-digit verification code is:\n\n"
            f"   {code}\n\n"
            f"This code expires in 5 minutes. If you didn't create this "
            f"account, just ignore this email.\n\n"
            f"-- AnimeChat"
        )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
            server.login(user, password)
            server.sendmail(sender, [to_email], msg.as_string())
        return {"sent": True, "dev_code": None}
    except Exception as exc:
        # Don't crash signup just because mail delivery failed -- fall back
        # to the dev code so the person can still verify and tell us why.
        print(f"[AnimeChat][MAIL ERROR] Could not send to {to_email}: {exc}")
        _log_dev_code(to_email, code)
        return {"sent": False, "dev_code": code}
