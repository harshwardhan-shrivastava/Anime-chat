"""
Sends the email-verification code when someone signs up or resets a
password.

Delivery is tried in this order:

1. Gmail API OAuth (GMAIL_TOKEN_JSON) - HTTPS on port 443, so it works
   from any host, including Render (which cannot reach Gmail's SMTP
   servers at all - outbound SMTP is blocked there). This is the same
   method Project Tohoku uses: the token comes from gmail_auth.py, and
   the email arrives in the inbox from your own Gmail account.
2. Gmail/plain SMTP (SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS) -
   the original primary method. Works on local dev and hosts that allow
   outbound SMTP.
3. SendGrid REST API (SENDGRID_API_KEY) - backup method, HTTPS on port
   443. Only used if the Gmail methods are not configured or fail. You
   must verify a sender address once in the SendGrid dashboard
   (Settings -> Sender Authentication -> Single Sender Verification) -
   no domain needed.
4. Dev fallback - the code is logged to logs/emails.log and returned so
   the verify page can show it directly on screen while real email is
   being configured.

Environment variables:

    GMAIL_TOKEN_JSON   the token.json contents from gmail_auth.py (primary on Render)
    SMTP_HOST          e.g. smtp.gmail.com
    SMTP_PORT          e.g. 465
    SMTP_USER          the mailbox username / address to send from
    SMTP_PASS          the mailbox password or app password
    MAIL_FROM          the "From" address shown to recipients (defaults to SMTP_USER)
    SENDGRID_API_KEY   free Twilio SendGrid API key (backup only)
"""

import base64
import json
import os
import socket
import smtplib
import ssl
import urllib.request
from email.mime.text import MIMEText


def _ipv4_first_socket(host, port, timeout, tls, context, server_hostname):
    """Connect to host:port preferring IPv4.

    Gmail's DNS returns IPv6 (AAAA) records first; Render free instances
    have no IPv6 route, so Python's default connection attempt dies with
    '[Errno 101] Network is unreachable'. Sorting AF_INET first fixes it.
    """
    addrs = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    addrs.sort(key=lambda a: 0 if a[0] == socket.AF_INET else 1)
    last = None
    for family, stype, proto, _, addr in addrs:
        raw = None
        try:
            raw = socket.socket(family, stype, proto)
            raw.settimeout(timeout)
            raw.connect(addr)
            if tls:
                return context.wrap_socket(raw, server_hostname=server_hostname)
            return raw
        except OSError as exc:
            last = exc
            if raw is not None:
                try:
                    raw.close()
                except OSError:
                    pass
    raise last or OSError(f"could not connect to {host}:{port}")


class _Ipv4FirstSMTP(smtplib.SMTP):
    """SMTP that connects IPv4-first (for STARTTLS on port 587)."""

    def _get_socket(self, host, port, timeout):
        return _ipv4_first_socket(host, port, timeout, False, None, None)


class _Ipv4FirstSMTP_SSL(smtplib.SMTP_SSL):
    """SMTP_SSL that connects IPv4-first (for implicit TLS on port 465)."""

    def _get_socket(self, host, port, timeout):
        return _ipv4_first_socket(host, port, timeout, True, self.context, self._host)

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


def _send_via_gmail_api(to_email, subject, body):
    """Send through the Gmail REST API using an OAuth token.

    GMAIL_TOKEN_JSON is the token.json produced by gmail_auth.py in the
    Project Tohoku setup (paste its full contents into the env var). The
    request goes over HTTPS (port 443), which works from Render even
    though Gmail SMTP is unreachable there. The access token refreshes
    automatically; the refresh token stays valid while the Google Cloud
    OAuth app is published ("In production").
    """
    from google.auth.exceptions import GoogleAuthError, RefreshError
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token_json = os.environ.get("GMAIL_TOKEN_JSON") or ""
    if not token_json.strip():
        raise RuntimeError("GMAIL_TOKEN_JSON is not configured")

    try:
        token_info = json.loads(token_json)
    except Exception as exc:
        raise RuntimeError(
            f"GMAIL_TOKEN_JSON is not valid JSON: {exc}"
        ) from exc

    try:
        credentials = Credentials.from_authorized_user_info(
            token_info,
            ["https://www.googleapis.com/auth/gmail.send"],
        )
    except Exception as exc:
        raise RuntimeError(
            "GMAIL_TOKEN_JSON is not a usable Gmail token. Re-run gmail_auth.py "
            f"and paste the new token.json contents into GMAIL_TOKEN_JSON. ({exc})"
        ) from exc

    sender = (
        (os.environ.get("MAIL_FROM") or "").strip()
        or (os.environ.get("SMTP_USER") or "").strip()
    )
    msg = MIMEText(body)
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    msg["To"] = to_email

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

    try:
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
    except (RefreshError, GoogleAuthError) as exc:
        raise RuntimeError(
            "Gmail API token is expired or invalid. Fix: in Google Cloud Console "
            "set the OAuth consent screen to 'In production' (so it never expires "
            f"again), re-run gmail_auth.py, then update GMAIL_TOKEN_JSON. ({exc})"
        ) from exc
    except Exception as exc:
        raise RuntimeError(f"Gmail API error: {exc}") from exc

    print("[AnimeChat] EMAIL SENT THROUGH GMAIL API")


def _send_via_smtp(to_email, subject, body):
    host = (os.environ.get("SMTP_HOST") or "").strip()
    user = (os.environ.get("SMTP_USER") or "").strip()
    # Gmail app passwords are generated with spaces ("abcd efgh ijkl mnop");
    # smtplib rejects them unless the spaces are removed.
    password = (os.environ.get("SMTP_PASS") or "").replace(" ", "")
    sender = (os.environ.get("MAIL_FROM") or "").strip() or user
    try:
        port = int((os.environ.get("SMTP_PORT") or "465").strip())
    except (TypeError, ValueError):
        port = 465

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email

    context = ssl.create_default_context()
    errors = []

    # Attempt 1: implicit TLS on the configured port (Gmail 465), IPv4-first.
    try:
        with _Ipv4FirstSMTP_SSL(host, port, context=context, timeout=10) as server:
            server.login(user, password)
            server.sendmail(sender, [to_email], msg.as_string())
        print("[AnimeChat] EMAIL SENT THROUGH SMTP (SSL)")
        return
    except Exception as exc:
        errors.append(f"ssl/{port}: {exc}")

    # Attempt 2: STARTTLS on 587 (Gmail's alternate port), IPv4-first.
    try:
        with _Ipv4FirstSMTP(host, 587, timeout=10) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(user, password)
            server.sendmail(sender, [to_email], msg.as_string())
        print("[AnimeChat] EMAIL SENT THROUGH SMTP (STARTTLS)")
        return
    except Exception as exc:
        errors.append(f"starttls/587: {exc}")

    raise RuntimeError("; ".join(errors))


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
    smtp_configured = bool((os.environ.get("SMTP_HOST") or "").strip())
    smtp_user = (os.environ.get("SMTP_USER") or "").strip()
    smtp_pass = (os.environ.get("SMTP_PASS") or "").strip()
    gmail_api_error = None
    smtp_error = None
    sendgrid_error = None

    if os.environ.get("GMAIL_TOKEN_JSON"):
        try:
            # Primary: Gmail API over HTTPS - works from Render.
            _send_via_gmail_api(to_email, subject, body)
            return {"sent": True, "dev_code": None, "dev_reason": None, "dev_error": None}
        except Exception as exc:
            gmail_api_error = str(exc)
            print(f"[AnimeChat][MAIL ERROR] Gmail API failed for {to_email}: {exc}")

    if smtp_configured and (not smtp_user or not smtp_pass):
        smtp_error = "SMTP_USER/SMTP_PASS are empty on the server"
        print(f"[AnimeChat][MAIL ERROR] {smtp_error}")
    elif smtp_configured:
        try:
            # Fallback: Gmail/plain SMTP (works on local dev).
            _send_via_smtp(to_email, subject, body)
            return {"sent": True, "dev_code": None, "dev_reason": None, "dev_error": None}
        except Exception as exc:
            smtp_error = str(exc)
            print(f"[AnimeChat][MAIL ERROR] SMTP failed for {to_email}: {exc}")

    api_key = os.environ.get("SENDGRID_API_KEY")
    if api_key:
        try:
            # Last backup: only used when the Gmail methods aren't configured or fail.
            _send_via_sendgrid(to_email, subject, body, api_key)
            return {"sent": True, "dev_code": None, "dev_reason": None, "dev_error": None}
        except Exception as exc:
            sendgrid_error = str(exc)
            print(f"[AnimeChat][MAIL ERROR] SendGrid failed for {to_email}: {exc}")

    _log_dev_code(to_email, code)

    failed = [e for e in (gmail_api_error, smtp_error, sendgrid_error) if e]
    if failed:
        detail = "All email methods failed. " + " | ".join(failed)
        reason = "smtp_failed"
    else:
        detail = "No email service is configured on this server."
        reason = "not_configured"

    return {
        "sent": False,
        "dev_code": code,
        "dev_reason": reason,
        "dev_error": detail,
    }
