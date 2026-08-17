"""
Sends the email-verification code when someone signs up or resets a
password.

Delivery is tried in this order:

1. SendGrid REST API (SENDGRID_API_KEY) - HTTPS on port 443, so it works
   from any hosting, even where outbound SMTP is blocked (e.g. Render).
   This is the recommended method. You must verify a sender address once
   in the SendGrid dashboard (Settings -> Sender Authentication ->
   Single Sender Verification) - no domain needed.
2. Plain SMTP (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS).
3. Dev fallback - the code is logged to logs/emails.log and returned so
   the verify page can show it directly on screen while real email is
   being configured.

Environment variables:

    SENDGRID_API_KEY   free Twilio SendGrid API key (recommended)
    SMTP_HOST          e.g. smtp.gmail.com
    SMTP_PORT          e.g. 465
    SMTP_USER          the mailbox username / address to send from
    SMTP_PASS          the mailbox password or app password
    MAIL_FROM          the "From" address shown to recipients (defaults to SMTP_USER)
"""

import json
import os
import smtplib
import ssl
import urllib.request
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


def _message_parts(username, code, purpose):
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
    return subject, body


def _send_via_sendgrid(to_email, subject, body, api_key):
    """Send through the Twilio SendGrid REST API (HTTPS, port 443).

    Requires a verified sender address set in the SendGrid dashboard
    (Settings -> Sender Authentication -> Single Sender Verification).
    """
    sender = os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER")
    if not sender:
        raise RuntimeError("SendGrid needs MAIL_FROM or SMTP_USER as the verified sender")

    payload = {
        "personalizations": [{"to": [{"email": to_email}]}],
        "from": {"email": sender},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    request = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + api_key.strip(),
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        if response.status != 202:
            raise RuntimeError("SendGrid returned status %s" % response.status)

    print("[AnimeChat] EMAIL SENT THROUGH SENDGRID")


def _send_via_smtp(to_email, subject, body):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("MAIL_FROM", user)

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(host, port, context=context, timeout=10) as server:
        server.login(user, password)
        server.sendmail(sender, [to_email], msg.as_string())

    print("[AnimeChat] EMAIL SENT THROUGH SMTP")


def send_verification_email(to_email, username, code, purpose="verify"):
    """Sends a 6-digit verification code.

    purpose is "verify" (signup) or "reset" (forgot password) -- it only
    changes the email wording.

    Returns a dict: {"sent": bool, "dev_code": str|None}.

    dev_code is only set when no email method is configured (or all of
    them failed), so the caller can surface it on the "enter your code"
    page for easy local testing.
    """

    subject, body = _message_parts(username, code, purpose)

    api_key = os.environ.get("SENDGRID_API_KEY")
    if api_key:
        try:
            _send_via_sendgrid(to_email, subject, body, api_key)
            return {"sent": True, "dev_code": None}
        except Exception as exc:
            # Don't crash signup just because one method failed -- try the
            # next method, and worst case surface the dev code.
            print(f"[AnimeChat][MAIL ERROR] SendGrid failed for {to_email}: {exc}")

    if os.environ.get("SMTP_HOST"):
        try:
            _send_via_smtp(to_email, subject, body)
            return {"sent": True, "dev_code": None}
        except Exception as exc:
            print(f"[AnimeChat][MAIL ERROR] SMTP failed for {to_email}: {exc}")

    _log_dev_code(to_email, code)
    return {"sent": False, "dev_code": code}
