import os
import random
import re
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from werkzeug.security import generate_password_hash, check_password_hash

import database

auth = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

DEFAULT_AVATAR = "profile1.png"
CODE_TTL = timedelta(minutes=5)
RESEND_COOLDOWN = timedelta(seconds=30)


def _new_code():
    return str(random.randint(100000, 999999))


def _public_user(user):
    """Strips the password hash before a user dict goes anywhere near a
    template or a JSON response."""
    if not user:
        return None
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "avatar_color": user["avatar_color"],
        "avatar": user.get("avatar") or DEFAULT_AVATAR,
        "is_verified": bool(user["is_verified"]),
        "created_at": user.get("created_at", ""),
    }


def load_logged_in_user():
    """Called once per request (see app.py before_request) to populate
    g.user from the session cookie."""
    user_id = session.get("user_id")
    g.user = _public_user(database.get_user_by_id(user_id)) if user_id else None


def login_required(view):
    from functools import wraps

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("user"):
            flash("Log in to do that.", "error")
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def _send_code_email(email, username, code, purpose="verify"):
    """Send the 6-digit code; falls back to a dev code when SMTP is off."""
    from services.mailer import send_verification_email

    return send_verification_email(email, username, code, purpose=purpose)


# ==========================================================
# SIGNUP (starts the 6-digit-code flow)
# ==========================================================
@auth.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""
    avatar = (request.form.get("avatar") or DEFAULT_AVATAR).strip()

    if not username or len(username) > 100:
        flash("Username must be 1-100 characters.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    if not EMAIL_RE.match(email):
        flash("Enter a valid email address.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    if password != confirm:
        flash("Passwords don't match.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    if database.get_user_by_username(username):
        flash("That username is already taken.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    if database.get_user_by_email(email):
        flash("An account with that email already exists. Try logging in.", "error")
        return render_template("signup.html", username=username, email=email, avatar=avatar)

    code = _new_code()
    session["pending_registration"] = {
        "username": username,
        "email": email,
        "password": generate_password_hash(password),
        "avatar": avatar,
        "verification_code": code,
        "expires_at": (datetime.now() + CODE_TTL).isoformat(),
        "last_sent_at": datetime.now().isoformat(),
    }

    mail_result = _send_code_email(email, username, code, purpose="verify")

    return render_template(
        "verify_email.html",
        email=email,
        purpose="verify",
        dev_code=mail_result.get("dev_code"),
        dev_reason=mail_result.get("dev_reason"),
        dev_error=mail_result.get("dev_error"),
        resent=False,
    )


# ==========================================================
# VERIFY EMAIL (enter the 6-digit code)
# ==========================================================
@auth.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    pending = session.get("pending_registration")
    if not pending:
        return redirect(url_for("auth.signup"))

    email = pending["email"]
    dev_code = None

    if request.method == "POST":
        entered = (request.form.get("verification_code") or "").strip()

        try:
            expires_at = datetime.fromisoformat(pending["expires_at"])
        except (ValueError, TypeError):
            expires_at = datetime.min

        if datetime.now() > expires_at:
            session.pop("pending_registration", None)
            flash("That verification code expired. Please sign up again.", "error")
            return redirect(url_for("auth.signup"))

        if entered != pending["verification_code"]:
            flash("That code isn't right — check it and try again.", "error")
            return render_template("verify_email.html", email=email, purpose="verify", dev_code=None, resent=False)

        user_id = database.create_user(
            pending["username"],
            pending["email"],
            pending["password"],
            avatar=pending["avatar"],
        )
        # The account is only created after the code checks out, so it's
        # verified by construction.
        database.mark_user_verified(user_id)

        session.permanent = True
        session["user_id"] = user_id
        session.pop("pending_registration", None)
        flash(f"Email verified! Welcome, {pending['username']}!", "success")
        return redirect(url_for("home"))

    return render_template("verify_email.html", email=email, purpose="verify", dev_code=dev_code, resent=False)


# ==========================================================
# RESEND REGISTRATION CODE
# ==========================================================
@auth.route("/resend-verification-code", methods=["POST"])
def resend_verification_code():
    pending = session.get("pending_registration")
    if not pending:
        return redirect(url_for("auth.signup"))

    last_sent = pending.get("last_sent_at")
    if last_sent:
        try:
            if datetime.now() < datetime.fromisoformat(last_sent) + RESEND_COOLDOWN:
                flash("Please wait 30 seconds before requesting another code.", "error")
                return render_template(
                    "verify_email.html",
                    email=pending["email"],
                    purpose="verify",
                    dev_code=None,
                    resent=False,
                )
        except (ValueError, TypeError):
            pass

    code = _new_code()
    pending["verification_code"] = code
    pending["expires_at"] = (datetime.now() + CODE_TTL).isoformat()
    pending["last_sent_at"] = datetime.now().isoformat()
    session["pending_registration"] = pending

    mail_result = _send_code_email(pending["email"], pending["username"], code, purpose="verify")

    return render_template(
        "verify_email.html",
        email=pending["email"],
        purpose="verify",
        dev_code=mail_result.get("dev_code"),
        dev_reason=mail_result.get("dev_reason"),
        dev_error=mail_result.get("dev_error"),
        resent=True,
    )


# ==========================================================
# USERNAME AVAILABILITY (live green-check on the signup form)
# ==========================================================
@auth.route("/api/username-available")
def username_available():
    """Live check used by the signup form: is this name free to use?"""
    from flask import jsonify

    username = (request.args.get("username") or "").strip()
    if not username:
        return jsonify({"available": False, "reason": "empty"})
    if len(username) > 100:
        return jsonify({"available": False, "reason": "too_long"})
    if database.get_user_by_username(username):
        return jsonify({"available": False, "reason": "taken"})
    return jsonify({"available": True, "reason": "ok"})


# ==========================================================
# LOGIN
# ==========================================================
@auth.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    identifier = (request.form.get("identifier") or "").strip()
    password = request.form.get("password") or ""
    next_url = request.form.get("next") or ""

    user = database.get_user_by_email(identifier.lower()) or database.get_user_by_username(identifier)

    if not user or not check_password_hash(user["password_hash"], password):
        flash("Incorrect username/email or password.", "error")
        return render_template("login.html", identifier=identifier, next=next_url)

    session.permanent = True
    session["user_id"] = user["id"]
    flash(f"Welcome back, {user['username']}!", "success")

    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("home"))


# ==========================================================
# FORGOT PASSWORD
# ==========================================================
@auth.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        user = database.get_user_by_email(email)

        # Don't reveal whether an email is registered -- same screen either way.
        if user:
            code = _new_code()
            session["password_reset"] = {
                "email": email,
                "verification_code": code,
                "expires_at": (datetime.now() + CODE_TTL).isoformat(),
                "last_sent_at": datetime.now().isoformat(),
            }
            mail_result = _send_code_email(email, user["username"], code, purpose="reset")
            return render_template(
                "verify_email.html",
                email=email,
                purpose="reset",
                dev_code=mail_result.get("dev_code"),
        dev_reason=mail_result.get("dev_reason"),
        dev_error=mail_result.get("dev_error"),
                resent=False,
            )

        flash("If an account exists for that email, we've sent a reset code.", "success")
        return render_template("forgot_password.html")

    return render_template("forgot_password.html")


# ==========================================================
# VERIFY PASSWORD RESET CODE
# ==========================================================
@auth.route("/verify-password-reset", methods=["GET", "POST"])
def verify_password_reset():
    reset = session.get("password_reset")
    if not reset:
        return redirect(url_for("auth.forgot_password"))

    email = reset["email"]
    dev_code = None

    if request.method == "POST":
        entered = (request.form.get("verification_code") or "").strip()

        try:
            expires_at = datetime.fromisoformat(reset["expires_at"])
        except (ValueError, TypeError):
            expires_at = datetime.min

        if datetime.now() > expires_at:
            session.pop("password_reset", None)
            flash("That code expired. Request a new one.", "error")
            return redirect(url_for("auth.forgot_password"))

        if entered != reset["verification_code"]:
            flash("That code isn't right — check it and try again.", "error")
            return render_template("verify_email.html", email=email, purpose="reset", dev_code=None, resent=False)

        session["password_reset_verified"] = True
        return redirect(url_for("auth.reset_password"))

    return render_template("verify_email.html", email=email, purpose="reset", dev_code=dev_code, resent=False)


# ==========================================================
# RESEND PASSWORD RESET CODE
# ==========================================================
@auth.route("/resend-password-reset-code", methods=["POST"])
def resend_password_reset_code():
    reset = session.get("password_reset")
    if not reset:
        return redirect(url_for("auth.forgot_password"))

    last_sent = reset.get("last_sent_at")
    if last_sent:
        try:
            if datetime.now() < datetime.fromisoformat(last_sent) + RESEND_COOLDOWN:
                flash("Please wait 30 seconds before requesting another code.", "error")
                return render_template(
                    "verify_email.html",
                    email=reset["email"],
                    purpose="reset",
                    dev_code=None,
                    resent=False,
                )
        except (ValueError, TypeError):
            pass

    code = _new_code()
    reset["verification_code"] = code
    reset["expires_at"] = (datetime.now() + CODE_TTL).isoformat()
    reset["last_sent_at"] = datetime.now().isoformat()
    session["password_reset"] = reset

    user = database.get_user_by_email(reset["email"])
    username = user["username"] if user else reset["email"]
    mail_result = _send_code_email(reset["email"], username, code, purpose="reset")

    return render_template(
        "verify_email.html",
        email=reset["email"],
        purpose="reset",
        dev_code=mail_result.get("dev_code"),
        dev_reason=mail_result.get("dev_reason"),
        dev_error=mail_result.get("dev_error"),
        resent=True,
    )


# ==========================================================
# RESET PASSWORD
# ==========================================================
@auth.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    reset = session.get("password_reset")
    if not reset or not session.get("password_reset_verified"):
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password") or ""
        confirm = request.form.get("confirm_password") or ""

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html")

        if new_password != confirm:
            flash("Passwords don't match.", "error")
            return render_template("reset_password.html")

        database.update_password(reset["email"], generate_password_hash(new_password))
        session.pop("password_reset", None)
        session.pop("password_reset_verified", None)
        flash("Password updated! Log in with your new password.", "success")
        return redirect(url_for("auth.login"))

    return render_template("reset_password.html")


# ==========================================================
# LOGOUT
# ==========================================================
@auth.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "success")
    return redirect(url_for("home"))
