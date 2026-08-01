"""
Sends the email-verification message when someone signs up.

If real SMTP credentials are configured via environment variables, a real
email goes out. If they're not configured yet, we don't block testing --
we log the verification link to the console and to logs/emails.log, and
the signup route also shows it directly on screen, so you (or your
brother) can still click through and verify the account immediately
while you're setting real SMTP up.

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


def _log_dev_link(to_email, verify_url):
    print(f"[AnimeChat][DEV MAIL] Verification link for {to_email}: {verify_url}")

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{to_email} | {verify_url}\n")
    except OSError:
        pass


def send_verification_email(to_email, username, verify_url):
    """Returns a dict: {"sent": bool, "dev_link": str|None}.

    dev_link is only set when SMTP isn't configured, so the caller can
    surface it on the "check your email" page for easy local testing.
    """

    host = os.environ.get("SMTP_HOST")

    if not host:
        _log_dev_link(to_email, verify_url)
        return {"sent": False, "dev_link": verify_url}

    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("MAIL_FROM", user)

    subject = "Verify your AnimeChat account"
    body = (
        f"Hey {username},\n\n"
        f"Welcome to AnimeChat! Confirm your email to activate your account "
        f"and start chatting in the communities:\n\n"
        f"{verify_url}\n\n"
        f"This link expires in 3 days. If you didn't create this account, "
        f"just ignore this email.\n\n"
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
        return {"sent": True, "dev_link": None}
    except Exception as exc:
        # Don't crash signup just because mail delivery failed -- fall back
        # to the dev link so the person can still verify and tell us why.
        print(f"[AnimeChat][MAIL ERROR] Could not send to {to_email}: {exc}")
        _log_dev_link(to_email, verify_url)
        return {"sent": False, "dev_link": verify_url}
