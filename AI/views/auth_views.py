"""Authentication: signup, login, OTP, password reset, admin login."""
from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.db import transaction
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from urllib.parse import quote

from AI.backends import EmailOrUsernameBackend
from AI.forms import ForgotPasswordForm, LoginForm, OTPForm, ResetPasswordForm, SignupForm
from AI.models import (
    AccountSource,
    AccountStatus,
    AuditAction,
    OTPChannel,
    OTPCode,
    OTPPurpose,
    Role,
    TelegramAccount,
    User,
)
from AI.services import (
    audit_service,
    model_service,
    otp_service,
    referral_campaign_service,
    referral_service,
    settings_service,
    telegram_service,
)
from AI.services.otp_service import OTPError
from AI.utils import qr, ratelimit
from AI.utils.decorators import anonymous_required
from AI.utils.ratelimit import RateLimited
from AI.utils.request import client_ip, mask_email

logger = logging.getLogger("AI.auth")

PENDING_USER_KEY = "pending_user_id"
PENDING_PURPOSE_KEY = "pending_otp_purpose"
PENDING_NEXT_KEY = "pending_next"
#: "Keep me signed in", carried across the code step of a two-step sign-in.
PENDING_REMEMBER_KEY = "pending_remember"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _establish_session(request, user, *, remember: bool = True) -> None:
    """Log the user in and claim the single active session slot."""
    auth_login(request, user, backend="AI.backends.EmailOrUsernameBackend")
    request.session.cycle_key()
    if not remember:
        request.session.set_expiry(0)

    User.objects.filter(pk=user.pk).update(
        active_session_key=request.session.session_key or "",
        last_login_ip=client_ip(request),
    )
    user.active_session_key = request.session.session_key or ""


#: Carries the signed-in person's first name back to the sign-in page's unlock
#: animation, which greets them by name before the redirect lands. Readable by
#: JavaScript on purpose, holds nothing secret, and the page clears it as soon
#: as it has been read.
GREET_COOKIE = "epc_hello"


def _greet(response, user):
    """Attach the greeting name to a post-sign-in redirect."""
    name = (user.display_name or "").strip().split(" ")[0]
    if name:
        response.set_cookie(
            GREET_COOKIE, quote(name[:40]),
            max_age=30, samesite="Lax", secure=settings.SESSION_COOKIE_SECURE,
        )
    return response


def _home_for(user) -> str:
    """Where a sign-in lands. Students go straight to the chat — it is the product."""
    return reverse("console:dashboard") if user.is_admin_user else reverse("student:chat")


def _safe_next(request, fallback: str) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    # Only allow local paths — never redirect off-site from a login form.
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _start_verification(request, user, purpose: str, *, next_url: str = "") -> None:
    request.session[PENDING_USER_KEY] = user.pk
    request.session[PENDING_PURPOSE_KEY] = purpose
    request.session[PENDING_NEXT_KEY] = next_url


def _pending_user(request):
    user_id = request.session.get(PENDING_USER_KEY)
    if not user_id:
        return None
    return User.objects.filter(pk=user_id).first()


def _challenge_login(request, user, *, next_url: str, remember: bool, ip: str):
    """The password was right, and this account asked for a code as well.

    Nothing is signed in yet: the session only remembers *who* is part-way
    through, and the sign-in happens in :func:`otp_verify` once the code
    checks out. The code goes to the student's private Telegram chat when one
    is linked and reachable, otherwise by e-mail — never to a group, a
    channel, or a chat that has blocked the bot.
    """
    try:
        ratelimit.enforce("otp_send", f"{user.pk}")
        otp = otp_service.issue(user, OTPPurpose.LOGIN, channel=otp_service.preferred_channel(user), ip=ip)
        messages.info(request, f"Enter the sign-in code we sent to {otp_service.sent_to(otp)}.")
    except RateLimited:
        messages.warning(request, "We sent you a code a moment ago — use that one, or wait a little and resend.")
    except OTPError as exc:
        messages.warning(request, exc.friendly_message)
    _start_verification(request, user, OTPPurpose.LOGIN, next_url=next_url)
    request.session[PENDING_REMEMBER_KEY] = bool(remember)
    return redirect("auth:otp")


def _free_trial_tokens() -> int:
    """How many tokens a new account is actually credited with, or zero.

    The sign-up panel offers "free tokens" — it should only say so when the
    grant is switched on and non-zero.
    """
    if not settings_service.get("free_trial_enabled", True):
        return 0
    return int(settings_service.get("free_trial_tokens", 0) or 0)


def _login_context(form, next_url: str) -> dict:
    """Sign in and sign up render the same card, so both supply what it reads."""
    return {"form": form, "next": next_url, "free_trial_tokens": _free_trial_tokens(),
            "free_trial_model": referral_service.welcome_model_name()}


def _signup_context(form, prefilled: str) -> dict:
    return {
        "form": form,
        "referral_prefill": prefilled,
        "free_trial_tokens": _free_trial_tokens(),
        "free_trial_model": referral_service.welcome_model_name(),
    }


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------
@never_cache
@anonymous_required
def signup(request):
    if not settings_service.get("signup_enabled", True):
        messages.info(request, "Public signup is currently closed. Please contact support for an account.")
        return redirect("auth:login")

    prefilled = (request.GET.get("ref") or "").strip().upper()
    form = SignupForm(request.POST or None, initial={"referral_code": prefilled} if prefilled else None)

    if request.method == "POST":
        try:
            ratelimit.enforce("signup", client_ip(request) or "unknown")
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return render(request, "auth/signup.html", _signup_context(form, prefilled))

        if form.is_valid():
            data = form.cleaned_data
            referrer = getattr(form, "referrer", None)
            campaign = getattr(form, "campaign", None)

            with transaction.atomic():
                user = User(
                    username=data["username"],
                    email=data["email"],
                    full_name=data["full_name"],
                    role=Role.STUDENT,
                    source=AccountSource.WEB,
                    status=AccountStatus.PENDING
                    if settings_service.get("require_verification", True)
                    else AccountStatus.ACTIVE,
                    is_verified=not settings_service.get("require_verification", True),
                )
                user.set_password(data["password"])
                user.save()

                # A campaign code and a personal code are mutually exclusive:
                # the form resolved the one that was typed, and only one of
                # them can be set.
                if campaign is not None:
                    referral_campaign_service.attach(user, campaign, ip=client_ip(request))
                elif referrer is not None:
                    referral_service.register_referral(user, referrer, ip=client_ip(request))

            audit_service.record(
                AuditAction.USER_CREATE, actor=user, request=request, target=user,
                description="Self-registered from the website",
            )

            if not settings_service.get("require_verification", True):
                referral_service.onboard(user)
                _establish_session(request, user)
                messages.success(request, f"Welcome to {user.display_name.split()[0] if user.full_name else 'the platform'}! Your account is ready.")
                return _greet(redirect("student:chat"), user)

            try:
                otp_service.issue(user, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL, ip=client_ip(request))
            except OTPError as exc:
                logger.warning("Signup OTP failed for %s: %s", user.pk, exc)
                messages.warning(
                    request,
                    "Your account was created, but we could not send the verification code. "
                    "You can request a new one below.",
                )

            _start_verification(request, user, OTPPurpose.SIGNUP)
            return redirect("auth:otp")

        messages.error(request, "Please fix the highlighted fields and try again.")

    return render(request, "auth/signup.html", _signup_context(form, prefilled))


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
@never_cache
@anonymous_required
def login_view(request):
    form = LoginForm(request.POST or None)
    next_url = _safe_next(request, "")

    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["identifier"]
        password = form.cleaned_data["password"]
        remember = form.cleaned_data.get("remember", True)
        ip = client_ip(request) or "unknown"

        try:
            ratelimit.enforce("login", f"{ip}:{identifier.lower()}")
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return render(request, "auth/login.html", _login_context(form, next_url))

        backend = EmailOrUsernameBackend()
        user = backend.authenticate(request, username=identifier, password=password)

        if user is None:
            # Distinguish a disabled account from a bad password, without
            # leaking whether an unknown username exists.
            candidate = User.objects.filter(username__iexact=identifier).first() or (
                User.objects.filter(email__iexact=identifier).first()
            )
            if candidate and candidate.check_password(password) and not candidate.is_active_account:
                audit_service.record(
                    AuditAction.LOGIN_FAILED, actor=candidate, request=request, target=candidate,
                    description="Login blocked — account disabled",
                )
                messages.error(
                    request,
                    "This account has been disabled. Please contact support if you think this is a mistake.",
                )
            else:
                audit_service.record(
                    AuditAction.LOGIN_FAILED, request=request,
                    target_label=identifier[:140], description="Invalid credentials",
                )
                messages.error(request, "That username or password is not correct.")
            return render(request, "auth/login.html", _login_context(form, next_url))

        ratelimit.reset("login", f"{ip}:{identifier.lower()}")

        if user.is_student and settings_service.get("require_verification", True) and not user.is_verified:
            try:
                otp_service.issue(user, OTPPurpose.SIGNUP, channel=OTPChannel.EMAIL, ip=ip)
                messages.info(request, "Please verify your account to continue. We sent you a new code.")
            except OTPError as exc:
                messages.warning(request, exc.friendly_message)
            _start_verification(request, user, OTPPurpose.SIGNUP, next_url=next_url)
            return redirect("auth:otp")

        if otp_service.login_code_required(user):
            if otp_service.can_reach(user):
                return _challenge_login(request, user, next_url=next_url, remember=remember, ip=ip)
            # Required for everyone, but this account has neither e-mail nor
            # Telegram: a code could never arrive, so locking it out would be
            # the only effect. Let it in and leave a trail to fix it.
            logger.warning("Sign-in code required but unreachable user=%s", user.pk)

        _establish_session(request, user, remember=remember)
        audit_service.record(AuditAction.LOGIN, actor=user, request=request, target=user, description="Signed in")

        if user.must_change_password:
            messages.info(request, "Please set a new password before continuing.")
            return redirect("student:profile")

        return _greet(redirect(next_url or _home_for(user)), user)

    if request.method == "POST":
        messages.error(request, "Enter your username and password.")

    return render(request, "auth/login.html", _login_context(form, next_url))


@never_cache
@anonymous_required
def admin_login(request):
    """Separate entry point for the console, with the same backend checks."""
    form = LoginForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["identifier"]
        ip = client_ip(request) or "unknown"
        try:
            ratelimit.enforce("login", f"admin:{ip}:{identifier.lower()}")
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return render(request, "auth/admin_login.html", {"form": form})

        user = EmailOrUsernameBackend().authenticate(
            request, username=identifier, password=form.cleaned_data["password"]
        )
        if user is None or not user.is_admin_user:
            audit_service.record(
                AuditAction.LOGIN_FAILED, request=request, target_label=identifier[:140],
                description="Failed admin console login",
            )
            messages.error(request, "Those administrator credentials are not correct.")
            return render(request, "auth/admin_login.html", {"form": form})

        ratelimit.reset("login", f"admin:{ip}:{identifier.lower()}")
        if user.login_otp_enabled:
            # Administrators get the code step only if they opted in.
            return _challenge_login(
                request, user, next_url=_safe_next(request, reverse("console:dashboard")),
                remember=form.cleaned_data.get("remember", True), ip=ip,
            )
        _establish_session(request, user, remember=form.cleaned_data.get("remember", True))
        audit_service.record(
            AuditAction.LOGIN, actor=user, request=request, target=user, description="Signed in to the admin console"
        )
        return redirect(_safe_next(request, reverse("console:dashboard")))

    return render(request, "auth/admin_login.html", {"form": form})


# ---------------------------------------------------------------------------
# OTP
# ---------------------------------------------------------------------------
@never_cache
def otp_verify(request):
    user = _pending_user(request)
    if user is None:
        messages.info(request, "Start again to receive a verification code.")
        return redirect("auth:login")

    purpose = request.session.get(PENDING_PURPOSE_KEY, OTPPurpose.SIGNUP)
    form = OTPForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        try:
            ratelimit.enforce("otp_verify", f"{user.pk}")
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return redirect("auth:otp")

        try:
            otp_service.verify(user, purpose, form.cleaned_data["code"])
        except OTPError as exc:
            messages.error(request, exc.friendly_message)
            return redirect("auth:otp")

        next_url = request.session.get(PENDING_NEXT_KEY, "")
        remember = request.session.get(PENDING_REMEMBER_KEY, True)
        for key in (PENDING_USER_KEY, PENDING_PURPOSE_KEY, PENDING_NEXT_KEY, PENDING_REMEMBER_KEY):
            request.session.pop(key, None)

        if purpose == OTPPurpose.RESET:
            request.session["reset_user_id"] = user.pk
            return redirect("auth:reset_password")

        if purpose == OTPPurpose.LOGIN:
            # Second step of a sign-in: the account may have been disabled in
            # the minutes between the password and the code.
            if not user.is_active_account:
                messages.error(request, "This account has been disabled. Please contact support.")
                return redirect("auth:login")
            _establish_session(request, user, remember=remember)
            audit_service.record(
                AuditAction.LOGIN, actor=user, request=request, target=user,
                description="Signed in with a verification code",
            )
            if user.must_change_password:
                messages.info(request, "Please set a new password before continuing.")
                return redirect("student:profile")
            return _greet(redirect(next_url or _home_for(user)), user)

        # Signup / login verification — activate and onboard.
        newly_verified = not user.is_verified
        user.is_verified = True
        if user.status == AccountStatus.PENDING:
            user.status = AccountStatus.ACTIVE
        user.save(update_fields=["is_verified", "status"])

        if newly_verified:
            result = referral_service.onboard(user)
            if result["free_trial_tokens"]:
                messages.success(
                    request,
                    f"Account verified — {result['free_trial_tokens']:,} free "
                    f"{getattr(model_service.default_model(), 'name', '')} tokens have been added.".replace("  ", " "),
                )
            if result["referral_bonus"]:
                messages.success(request, f"Referral bonus applied: +{result['referral_bonus']:,} tokens.")
        else:
            messages.success(request, "Verification complete.")

        _establish_session(request, user)
        audit_service.record(AuditAction.LOGIN, actor=user, request=request, target=user, description="Verified and signed in")
        return _greet(redirect(next_url or _home_for(user)), user)

    telegram = TelegramAccount.objects.filter(user=user).first()
    telegram_ready = otp_service.telegram_target(user) is not None
    latest = (
        OTPCode.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at", "-id").first()
    )
    # Linking Telegram from this page is offered only to someone who has just
    # proved the password (a new sign-up). A password reset starts from a
    # username alone, so it must never hand out a link that could attach a
    # stranger's Telegram — and with it, the reset code — to the account.
    link_url = app_url = link_qr = ""
    if purpose == OTPPurpose.SIGNUP and not telegram_ready and telegram_service.is_configured():
        account = telegram_service.get_or_create_link(user)
        if not account.is_linked:
            if not telegram_service.link_is_valid(account):
                account = telegram_service.issue_link_token(user)
            link_url = telegram_service.deep_link(account)
            # t.me is a web page some networks block; tg:// goes straight to
            # an installed Telegram app, and the QR to a phone on another network.
            if link_url:
                app_url = (f"tg://resolve?domain={quote(telegram_service.bot_username())}"
                           f"&start={quote(account.link_token)}")
                link_qr = qr.svg(link_url, dark="currentColor")

    return render(
        request,
        "auth/otp.html",
        {
            "form": form,
            "otp_user": user,
            "purpose": purpose,
            "is_login": purpose == OTPPurpose.LOGIN,
            "masked_email": mask_email(user.email),
            "has_email": bool(user.email),
            "sent_via": latest.channel if latest else "",
            "sent_to": latest.destination if latest else "",
            "telegram": telegram,
            "telegram_available": telegram_ready,
            "telegram_blocked": bool(telegram and telegram.is_linked and telegram.blocked_at),
            "telegram_bot_url": telegram_service.bot_url(),
            "telegram_link_url": link_url,
            "telegram_app_url": app_url,
            "telegram_link_qr": link_qr,
            "channels": otp_service.available_channels(user),
            # One "send a new code" button when codes go everywhere at once.
            "sends_everywhere": otp_service.sends_everywhere(purpose),
            # The code step is the administrator's rule, not the student's choice.
            "required_for_everyone": (purpose == OTPPurpose.LOGIN and not user.login_otp_enabled
                                      and otp_service.login_code_required(user)),
            # Development aid: with no SMTP the code is only printed to the
            # server console, so say so instead of letting the student wait.
            "mail_is_console_only": settings.DEBUG and "console" in settings.EMAIL_BACKEND.lower(),
        },
    )


@never_cache
@require_http_methods(["POST"])
def otp_resend(request):
    user = _pending_user(request)
    if user is None:
        return redirect("auth:login")

    channel = request.POST.get("channel", OTPChannel.EMAIL)
    if channel not in {OTPChannel.EMAIL, OTPChannel.TELEGRAM}:
        channel = OTPChannel.EMAIL

    try:
        ratelimit.enforce("otp_send", f"{user.pk}")
    except RateLimited as exc:
        messages.error(request, exc.friendly_message)
        return redirect("auth:otp")

    purpose = request.session.get(PENDING_PURPOSE_KEY, OTPPurpose.SIGNUP)
    try:
        otp = otp_service.issue(user, purpose, channel=channel, ip=client_ip(request))
        # Say where it really went: a Telegram request falls back to e-mail
        # when the chat is blocked or unreachable.
        target = otp_service.sent_to(otp)
        if (channel == OTPChannel.TELEGRAM and not otp_service.sends_everywhere(purpose)
                and otp.channel != OTPChannel.TELEGRAM):
            messages.info(request, "We couldn't reach your Telegram, so the code went by e-mail instead.")
        messages.success(request, f"A new code is on its way to {target}.")
    except OTPError as exc:
        messages.error(request, exc.friendly_message)

    return redirect("auth:otp")


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------
@never_cache
@anonymous_required
def forgot_password(request):
    form = ForgotPasswordForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        identifier = form.cleaned_data["identifier"]
        try:
            ratelimit.enforce("password_reset", f"{client_ip(request)}:{identifier.lower()}")
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return render(request, "auth/forgot_password.html", {"form": form})

        user = (
            User.objects.filter(username__iexact=identifier).first()
            or User.objects.filter(email__iexact=identifier).first()
        )

        if user and user.is_active_account:
            try:
                otp_service.issue(user, OTPPurpose.RESET, channel=OTPChannel.EMAIL, ip=client_ip(request))
                _start_verification(request, user, OTPPurpose.RESET)
                return redirect("auth:otp")
            except OTPError as exc:
                logger.info("Reset OTP failed: %s", exc)

        # Never disclose whether an account exists.
        messages.success(
            request,
            "If an account matches that detail, we have sent a reset code. Check your inbox and spam folder.",
        )
        return redirect("auth:login")

    return render(request, "auth/forgot_password.html", {"form": form})


@never_cache
@anonymous_required
def reset_password(request):
    user_id = request.session.get("reset_user_id")
    user = User.objects.filter(pk=user_id).first() if user_id else None
    if user is None:
        messages.info(request, "Start the password reset again.")
        return redirect("auth:forgot_password")

    form = ResetPasswordForm(request.POST or None, user=user)
    if request.method == "POST" and form.is_valid():
        user.set_password(form.cleaned_data["password"])
        user.must_change_password = False
        user.active_session_key = ""
        user.save(update_fields=["password", "must_change_password", "active_session_key"])
        request.session.pop("reset_user_id", None)

        audit_service.record(
            AuditAction.PASSWORD_RESET, actor=user, request=request, target=user,
            description="Password reset via verification code",
        )
        messages.success(request, "Your password has been updated. Please sign in.")
        return redirect("auth:login")

    return render(request, "auth/reset_password.html", {"form": form, "reset_user": user})


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------
@require_http_methods(["POST", "GET"])
def logout_view(request):
    user = request.user if request.user.is_authenticated else None
    if user is not None:
        audit_service.record(AuditAction.LOGOUT, actor=user, request=request, target=user, description="Signed out")
        User.objects.filter(pk=user.pk).update(active_session_key="")
    auth_logout(request)
    messages.success(request, "You have been signed out.")
    return redirect("public:home")
