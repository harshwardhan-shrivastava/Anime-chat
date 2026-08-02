import os
import re

from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

import database

auth = Blueprint("auth", __name__)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VERIFY_SALT = "animechat-email-verify"
VERIFY_MAX_AGE = 60 * 60 * 24 * 3  # 3 days


def _serializer():
    secret = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
    return URLSafeTimedSerializer(secret)


def make_verify_token(user_id):
    return _serializer().dumps({"uid": user_id}, salt=VERIFY_SALT)


def read_verify_token(token, max_age=VERIFY_MAX_AGE):
    data = _serializer().loads(token, salt=VERIFY_SALT, max_age=max_age)
    return data["uid"]


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
        "is_verified": bool(user["is_verified"]),
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


@auth.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html")

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""

    if not USERNAME_RE.match(username):
        flash("Username must be 3-20 characters: letters, numbers, underscores only.", "error")
        return render_template("signup.html", username=username, email=email)

    if not EMAIL_RE.match(email):
        flash("Enter a valid email address.", "error")
        return render_template("signup.html", username=username, email=email)

    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return render_template("signup.html", username=username, email=email)

    if password != confirm:
        flash("Passwords don't match.", "error")
        return render_template("signup.html", username=username, email=email)

    if database.get_user_by_username(username):
        flash("That username is already taken.", "error")
        return render_template("signup.html", username=username, email=email)

    if database.get_user_by_email(email):
        flash("An account with that email already exists. Try logging in.", "error")
        return render_template("signup.html", username=username, email=email)

    password_hash = generate_password_hash(password)
    user_id = database.create_user(username, email, password_hash)

    token = make_verify_token(user_id)
    verify_url = url_for("auth.verify_email", token=token, _external=True)

    from services.mailer import send_verification_email
    mail_result = send_verification_email(email, username, verify_url)

    if not mail_result.get("sent"):
        # No SMTP configured yet, so a verification link can't be delivered
        # by email. Don't strand the account: verify it right away and log
        # straight in, so new users (your brother, future members) can start
        # chatting immediately instead of hitting the verification wall.
        database.mark_user_verified(user_id)
        session["user_id"] = user_id
        flash(f"Welcome, {username}! Your account is ready to chat.", "success")
        return redirect(url_for("home"))

    return render_template(
        "check_email.html",
        email=email,
        dev_link=mail_result.get("dev_link"),
    )


@auth.route("/verify/<token>")
def verify_email(token):
    try:
        user_id = read_verify_token(token)
    except SignatureExpired:
        flash("That verification link expired. Sign up again or request a new one.", "error")
        return redirect(url_for("auth.signup"))
    except BadSignature:
        flash("That verification link isn't valid.", "error")
        return redirect(url_for("auth.signup"))

    user = database.get_user_by_id(user_id)
    if not user:
        flash("That account no longer exists.", "error")
        return redirect(url_for("auth.signup"))

    if not user["is_verified"]:
        database.mark_user_verified(user_id)

    session["user_id"] = user_id
    flash("Email verified! You're in.", "success")
    return redirect(url_for("home"))


@auth.route("/resend-verification", methods=["POST"])
def resend_verification():
    email = (request.form.get("email") or "").strip().lower()
    user = database.get_user_by_email(email)

    if user and not user["is_verified"]:
        token = make_verify_token(user["id"])
        verify_url = url_for("auth.verify_email", token=token, _external=True)

        from services.mailer import send_verification_email
        mail_result = send_verification_email(email, user["username"], verify_url)

        return render_template("check_email.html", email=email, dev_link=mail_result.get("dev_link"), resent=True)

    # Don't reveal whether the email exists -- just show the same screen.
    return render_template("check_email.html", email=email, resent=True)


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

    if not user["is_verified"]:
        if os.environ.get("SMTP_HOST"):
            flash("Please verify your email before logging in -- check your inbox.", "error")
            return render_template("login.html", identifier=identifier, next=next_url, unverified_email=user["email"])

        # Dev mode (no mail server): verification links can't be delivered,
        # so let the user in and mark the account verified on the spot.
        database.mark_user_verified(user["id"])

    session["user_id"] = user["id"]
    flash(f"Welcome back, {user['username']}!", "success")

    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(url_for("home"))


@auth.route("/logout")
def logout():
    session.pop("user_id", None)
    flash("Logged out.", "success")
    return redirect(url_for("home"))
