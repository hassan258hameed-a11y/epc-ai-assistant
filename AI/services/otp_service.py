"""One-time password issuing and verification.

Codes are random, stored hashed, expire quickly, are single-use and attempt
capped.  Either channel — e-mail or Telegram — can verify an account.

A code only ever goes to Telegram when the account is linked to a *private*
chat that has not blocked the bot. A group, a channel, or a chat Telegram says
is unreachable never receives one: the code falls back to e-mail instead, and
if there is no e-mail either, issuing fails rather than sending it somewhere
it should not go.
"""
from __future__ import annotations

import logging
import secrets

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from AI.models import OTPChannel, OTPCode, OTPPurpose, TelegramDeliveryStatus, hash_code
from AI.services import settings_service, telegram_service
from AI.utils.request import mask_email

logger = logging.getLogger("AI.otp")


class OTPError(Exception):
    def __init__(self, message: str, *, code: str = "otp_error"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


def _generate_code() -> str:
    length = max(4, min(8, int(settings_service.get("otp_length", 6))))
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def _ttl_minutes() -> int:
    return max(1, int(settings_service.get("otp_ttl_minutes", 10)))


def telegram_target(user):
    """The linked, private, unblocked chat a code may go to — or ``None``."""
    return telegram_service.private_target(user)


def available_channels(user) -> list[str]:
    channels = []
    if user.email:
        channels.append(OTPChannel.EMAIL)
    if telegram_target(user) is not None:
        channels.append(OTPChannel.TELEGRAM)
    return channels


def preferred_channel(user) -> str:
    """Telegram when a code can safely go there, otherwise e-mail."""
    return OTPChannel.TELEGRAM if telegram_target(user) is not None else OTPChannel.EMAIL


def students_must_use_codes() -> bool:
    """The administrator requires a sign-in code from every student."""
    return bool(settings_service.get("login_otp_students_required", False))


def login_code_required(user) -> bool:
    """Does this sign-in need a code after the password?

    A student's own choice, or the administrator's rule for every student.
    Staff accounts only ever opt in for themselves.
    """
    if user.login_otp_enabled:
        return True
    return user.is_student and students_must_use_codes()


def can_reach(user) -> bool:
    """Is there anywhere at all a code could safely go?"""
    return bool(user.email) or telegram_target(user) is not None


#: Codes that prove who is at the keyboard of an existing account. A sign-up
#: code proves the e-mail address itself, so it stays on the channel asked for.
EVERYWHERE_PURPOSES = (OTPPurpose.LOGIN, OTPPurpose.RESET)


def sends_everywhere(purpose: str) -> bool:
    """Does a code for this purpose go to Telegram and e-mail together?"""
    return purpose in EVERYWHERE_PURPOSES and bool(settings_service.get("otp_send_everywhere", True))


def _destination(user, channel: str, telegram_account=None) -> str:
    if channel == OTPChannel.EMAIL:
        return mask_email(user.email)
    if telegram_account is not None and telegram_account.username:
        return f"@{telegram_account.username}"
    return "Telegram"


def issue(
    user, purpose: str, *, channel: str = OTPChannel.EMAIL, ip: str | None = None, fallback: bool = True,
) -> OTPCode:
    """Create and deliver a code. Any previous unused code is invalidated.

    Asked for Telegram when there is no safe chat to send it to — not linked,
    not private, or blocked — or when Telegram refuses the message, the code
    goes by e-mail instead (``fallback``). The returned code's ``channel`` and
    ``destination`` say where it actually went.

    For sign-in and reset codes, with "send codes to Telegram and e-mail" on,
    the channel asked for is ignored and the one code goes to every safe
    place the account has.
    """
    if sends_everywhere(purpose):
        return _issue_everywhere(user, purpose, ip=ip)

    telegram_account = None
    if channel == OTPChannel.TELEGRAM:
        telegram_account = telegram_target(user)
        if telegram_account is None:
            if fallback and user.email:
                channel = OTPChannel.EMAIL
            elif not telegram_service.is_configured():
                raise OTPError("Telegram delivery is not available right now.", code="telegram_off")
            else:
                raise OTPError(
                    "We can't reach your Telegram. Open our bot, press Start, then request the code again.",
                    code="no_telegram",
                )

    if channel == OTPChannel.EMAIL and not user.email:
        raise OTPError("No e-mail address on file for this account.", code="no_email")

    OTPCode.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
        expires_at=timezone.now()
    )

    raw_code = _generate_code()
    otp = OTPCode.objects.create(
        user=user,
        purpose=purpose,
        channel=channel,
        code_hash=hash_code(raw_code),
        destination=_destination(user, channel, telegram_account),
        expires_at=timezone.now() + timezone.timedelta(minutes=_ttl_minutes()),
        ip_address=ip,
    )

    try:
        if channel == OTPChannel.TELEGRAM and not _send_telegram(telegram_account, raw_code, purpose):
            if not (fallback and user.email):
                raise OTPError("Telegram did not accept the message.", code="delivery_failed")
            # Blocked, deleted, or Telegram is down: the same code by e-mail.
            channel = OTPChannel.EMAIL
            otp.channel, otp.destination = channel, _destination(user, channel)
            otp.save(update_fields=["channel", "destination"])
        if channel == OTPChannel.EMAIL:
            _send_email(user, raw_code, purpose)
    except Exception as exc:
        otp.expires_at = timezone.now()
        otp.save(update_fields=["expires_at"])
        logger.warning("OTP delivery failed user=%s channel=%s: %s", user.pk, channel, exc)
        raise OTPError(
            "We could not deliver your code. Please try the other verification method.",
            code="delivery_failed",
        )

    _announce_for_development(user, raw_code, channel)
    return otp


def _issue_everywhere(user, purpose: str, *, ip: str | None) -> OTPCode:
    """One code, sent to the private Telegram chat and to the e-mail address.

    Either delivery may fail on its own — the chat was blocked, the mail
    server is down — and the code still stands as long as one got through;
    ``channel`` and ``destination`` record only where it did.
    """
    telegram_account = telegram_target(user)
    if telegram_account is None and not user.email:
        raise OTPError("No e-mail address or Telegram on file for this account.", code="no_email")

    OTPCode.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True).update(
        expires_at=timezone.now()
    )
    raw_code = _generate_code()
    otp = OTPCode.objects.create(
        user=user,
        purpose=purpose,
        channel=OTPChannel.TELEGRAM if telegram_account is not None else OTPChannel.EMAIL,
        code_hash=hash_code(raw_code),
        destination="",
        expires_at=timezone.now() + timezone.timedelta(minutes=_ttl_minutes()),
        ip_address=ip,
    )

    # E-mail first: it is already on its way while Telegram is tried, so a
    # network that stalls Telegram costs the student a wait, never the code.
    reached = []
    if user.email:
        try:
            _send_email(user, raw_code, purpose)
            reached.append(OTPChannel.EMAIL)
        except Exception as exc:
            logger.warning("OTP e-mail delivery failed user=%s: %s", user.pk, exc)
    if telegram_account is not None:
        try:
            if _send_telegram(telegram_account, raw_code, purpose):
                reached.insert(0, OTPChannel.TELEGRAM)
        except Exception as exc:  # pragma: no cover - deliver() records its own failures
            logger.warning("OTP Telegram delivery failed user=%s: %s", user.pk, exc)

    if not reached:
        otp.expires_at = timezone.now()
        otp.save(update_fields=["expires_at"])
        raise OTPError("We could not deliver your code. Please try again in a moment.", code="delivery_failed")

    otp.channel = OTPChannel.BOTH if len(reached) == 2 else reached[0]
    otp.destination = " and ".join(_destination(user, channel, telegram_account) for channel in reached)
    otp.save(update_fields=["channel", "destination"])
    if OTPChannel.EMAIL in reached:
        _announce_for_development(user, raw_code, OTPChannel.EMAIL)
    return otp


def sent_to(otp: OTPCode) -> str:
    """Where a code went, for "we sent a code to …" messages."""
    if otp.channel == OTPChannel.TELEGRAM:
        return "your Telegram"
    if otp.channel == OTPChannel.BOTH:
        email = otp.destination.rsplit(" and ", 1)[-1]
        return f"your Telegram and {email}"
    return otp.destination


def _announce_for_development(user, raw_code: str, channel: str) -> None:
    """Make the code impossible to miss when there is no real delivery.

    With no SMTP configured Django prints the message to the server console,
    where it is easy to overlook among request logs. This logs the code on its
    own line instead — only when DEBUG is on *and* mail is going to the console,
    so a production deployment can never leak a live code into its logs.
    """
    using_console_mail = "console" in getattr(settings, "EMAIL_BACKEND", "").lower()
    if not (settings.DEBUG and channel == OTPChannel.EMAIL and using_console_mail):
        return

    logger.warning(
        "DEV ONLY — no SMTP configured, so no e-mail was actually sent.\n"
        "    Verification code for %s (%s): %s\n"
        "    Configure EMAIL_HOST_USER / EMAIL_HOST_PASSWORD in .env to send real mail.",
        user.username, user.email or "no e-mail", raw_code,
    )


PURPOSE_LABEL = {
    OTPPurpose.SIGNUP: "verify your account",
    OTPPurpose.LOGIN: "sign in",
    OTPPurpose.RESET: "reset your password",
    OTPPurpose.TELEGRAM: "confirm your Telegram link",
}


def _send_email(user, raw_code: str, purpose: str) -> None:
    subject = f"{raw_code} is your {settings.BRAND['full_name']} verification code"
    context = {
        "code": raw_code,
        "user": user,
        "brand": settings.BRAND,
        "minutes": _ttl_minutes(),
        "purpose_label": PURPOSE_LABEL.get(purpose, "continue"),
        "site_url": settings.SITE_URL,
    }
    text_body = render_to_string("emails/otp.txt", context)
    html_body = render_to_string("emails/otp.html", context)

    email = EmailMultiAlternatives(subject, text_body, settings.DEFAULT_FROM_EMAIL, [user.email])
    email.attach_alternative(html_body, "text/html")
    email.send(fail_silently=False)


def _send_telegram(account, raw_code: str, purpose: str) -> bool:
    """Send the code to the student's private chat. True when Telegram took it.

    Goes through the recorded delivery path, so a block is noticed (and the
    account marked) the first time a code bounces. The delivery row never
    holds the text — only that an OTP was sent, and what happened.
    """
    if not telegram_service.can_receive_private(account):
        return False
    label = PURPOSE_LABEL.get(purpose, "continue")
    warning = (
        "\n\n<b>Not you?</b> Someone just entered your password. Change your password now."
        if purpose == OTPPurpose.LOGIN else "\nIf you did not request it, ignore this message."
    )
    delivery = telegram_service.deliver(
        account.telegram_id,
        f"<b>Verification code</b>\n\n"
        f"<code>{raw_code}</code>\n\n"
        f"Use this code to {label}. It expires in {_ttl_minutes()} minutes.{warning}",
        user=account.user, account=account, kind="otp",
        # Someone is waiting on the sign-in page: give up on a stalled
        # connection quickly, and let the e-mail copy carry the code.
        timeout=OTP_TELEGRAM_TIMEOUT,
    )
    return delivery.status == TelegramDeliveryStatus.SENT


#: (connect, read) seconds for a code's Telegram message.
OTP_TELEGRAM_TIMEOUT = (4, 8)


def verify(user, purpose: str, raw_code: str) -> OTPCode:
    """Consume a code. Raises :class:`OTPError` with a friendly message."""
    raw_code = (raw_code or "").strip()
    if not raw_code.isdigit():
        raise OTPError("Enter the numeric code we sent you.", code="invalid_format")

    # Newest first — by id as well as time: a code re-sent within the same
    # clock tick (about 15 ms on Windows) has the same created_at as the one it
    # replaced, and picking the voided one would say "expired".
    otp = (
        OTPCode.objects.filter(user=user, purpose=purpose, consumed_at__isnull=True)
        .order_by("-created_at", "-id")
        .first()
    )
    if otp is None:
        raise OTPError("No active code found. Request a new one.", code="not_found")
    if otp.is_expired:
        raise OTPError("That code has expired. Request a new one.", code="expired")
    if otp.attempts >= OTPCode.MAX_ATTEMPTS:
        raise OTPError("Too many incorrect attempts. Request a new code.", code="too_many_attempts")

    otp.attempts += 1
    otp.save(update_fields=["attempts"])

    if not otp.matches(raw_code):
        remaining = OTPCode.MAX_ATTEMPTS - otp.attempts
        if remaining <= 0:
            raise OTPError("Too many incorrect attempts. Request a new code.", code="too_many_attempts")
        raise OTPError(f"That code is not correct. {remaining} attempt(s) left.", code="mismatch")

    otp.consumed_at = timezone.now()
    otp.save(update_fields=["consumed_at"])
    return otp
