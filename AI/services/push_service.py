"""Browser notifications that arrive when the site is closed (Web Push).

A student turns notifications on in Settings → "Notifications on this device".
Their browser then hands us a *subscription*: a push-service URL (the
``endpoint``) and two keys every message is encrypted with. To notify them we
encrypt a small JSON payload for each of their devices and POST it to that
endpoint, signed with our VAPID key so the push service knows it is us.

Everything here is a courtesy on top of the in-app notification: a push that
cannot be sent is logged and counted, never raised, and never undoes what the
caller already did.

* :func:`send` — one student, now, on this thread. Respects ``push_enabled``
  and the category switch the notification belongs to.
* :func:`queue` — the same, once the current transaction commits, on a
  background thread. What ``notification_service.notify`` uses, so no request
  waits on a push service.
* :func:`answer_ready` — "Your answer is ready", for an answer that finished
  after the tab was closed. Respects ``push_answers``.
* :func:`queue_announcement` — an announcement to its whole audience, off the
  request thread.

Keys come from ``settings.VAPID_PRIVATE_KEY`` / ``VAPID_PUBLIC_KEY`` /
``VAPID_SUBJECT``; ``python manage.py generate_vapid_keys`` prints a pair.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import threading
import time
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from AI.models import NotificationPreference, PushSubscription

try:  # pragma: no cover - exercised only when the dependency is missing
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover
    webpush = None

    class WebPushException(Exception):  # type: ignore[no-redef]
        response = None


logger = logging.getLogger("AI.push")

#: How long a push service keeps a message for a device that is offline.
#: A day: "your answer is ready" is still worth seeing tomorrow morning, a
#: week-old one is not.
TTL_SECONDS = 24 * 60 * 60
#: Seconds to wait for a push service before counting the attempt as failed.
TIMEOUT_SECONDS = 10
#: Consecutive failures (other than "gone") after which a device is dropped.
MAX_FAILURES = 5
#: Devices kept per account; the oldest beyond this are forgotten.
MAX_DEVICES = 20
#: Seconds between devices in an announcement broadcast.
BROADCAST_PACE_SECONDS = 0.02

#: The pseudo-category for "your answer is ready", switched by ``push_answers``.
ANSWER = "answer"

_TITLE_MAX = 120
_BODY_MAX = 240


class PushError(ValueError):
    """A subscription the browser sent that we cannot use."""

    def __init__(self, message: str):
        super().__init__(message)
        self.friendly_message = message


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------
_vapid_cache: dict[str, object] = {}
_vapid_lock = threading.Lock()
_warned: set[str] = set()


def _b64url_decode(value: str) -> bytes:
    value = (value or "").strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _vapid():
    """The signing key, loaded once per configured value. ``None`` if unusable."""
    private = (getattr(settings, "VAPID_PRIVATE_KEY", "") or "").strip()
    if not private or webpush is None:
        return None
    with _vapid_lock:
        if private in _vapid_cache:
            return _vapid_cache[private]
        vapid = None
        try:
            from py_vapid import Vapid

            if "BEGIN" in private:  # a PEM pasted with literal "\n"s
                vapid = Vapid.from_pem(private.replace("\\n", "\n").encode())
            else:
                vapid = Vapid.from_string(private_key=private)
        except Exception:
            # Never log the value itself.
            logger.error("VAPID_PRIVATE_KEY could not be loaded; browser notifications are off.")
            vapid = None
        _vapid_cache[private] = vapid
        return vapid


def is_configured() -> bool:
    """True when a usable VAPID key pair is set up."""
    return _vapid() is not None


def public_key() -> str:
    """The application server key browsers subscribe with (URL-safe base64).

    Derived from the private key, so it always matches what messages are
    signed with; a mismatched ``VAPID_PUBLIC_KEY`` is reported, not used.
    """
    vapid = _vapid()
    if vapid is None:
        return ""
    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint,
    )
    derived = _b64url_encode(raw)
    configured = (getattr(settings, "VAPID_PUBLIC_KEY", "") or "").strip().rstrip("=")
    if configured and configured != derived and configured not in _warned:
        _warned.add(configured)
        logger.warning("VAPID_PUBLIC_KEY does not match VAPID_PRIVATE_KEY; using the key derived from the private one.")
    return derived


def _claims() -> dict:
    subject = (getattr(settings, "VAPID_SUBJECT", "") or "").strip()
    if not subject.startswith(("mailto:", "https://")):
        subject = f"mailto:{settings.BRAND.get('support_email') or 'support@example.com'}"
    # A fresh dict every call: pywebpush writes the endpoint's origin into it
    # as "aud", and a shared one would sign the next device's message for the
    # wrong push service.
    return {"sub": subject}


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
def _check_key(value, *, length: int, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PushError(f"The browser did not send its {name} key.")
    value = value.strip().rstrip("=")
    try:
        raw = _b64url_decode(value)
    except (binascii.Error, ValueError):
        raise PushError(f"The browser's {name} key is not valid.") from None
    if len(raw) != length or (name == "p256dh" and raw[:1] != b"\x04"):
        raise PushError(f"The browser's {name} key is not valid.")
    return value


def _clean(data) -> tuple[str, str, str]:
    """``(endpoint, p256dh, auth)`` from a ``PushSubscription.toJSON()``, or PushError."""
    if not isinstance(data, dict):
        raise PushError("That is not a browser subscription.")
    endpoint = data.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise PushError("The browser did not say where to send notifications.")
    endpoint = endpoint.strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname or any(c.isspace() for c in endpoint):
        raise PushError("Notifications can only be delivered over a secure (https) push service.")
    if len(endpoint) > PushSubscription._meta.get_field("endpoint").max_length:
        raise PushError("The browser's push address is too long.")
    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        raise PushError("The browser did not send its keys.")
    p256dh = _check_key(keys.get("p256dh"), length=65, name="p256dh")
    auth = _check_key(keys.get("auth"), length=16, name="auth")
    return endpoint, p256dh, auth


def subscribe(user, data, user_agent: str = "", *, old_endpoint: str = "") -> PushSubscription:
    """Remember one device for ``user``. Raises :class:`PushError` if unusable.

    Keyed by endpoint: the same browser subscribing again updates its row. If
    the endpoint belonged to somebody else — one browser, two students taking
    turns — it moves to whoever signed in last, so nobody receives another
    student's alerts. ``old_endpoint`` (from a ``pushsubscriptionchange``) is
    forgotten when it was this student's.
    """
    endpoint, p256dh, auth = _clean(data)
    fields = {
        "user": user,
        "p256dh": p256dh,
        "auth": auth,
        "user_agent": (user_agent or "")[:200],
        "failure_count": 0,
    }
    for attempt in range(2):
        try:
            with transaction.atomic():
                existing = PushSubscription.objects.select_for_update().filter(endpoint=endpoint).first()
                if existing is None:
                    subscription = PushSubscription.objects.create(endpoint=endpoint, **fields)
                else:
                    moved = existing.user_id != user.pk
                    for name, value in fields.items():
                        setattr(existing, name, value)
                    existing.save()
                    subscription = existing
                    if moved:
                        logger.info("Push device %s moved to user=%s", existing.pk, user.pk)
            break
        except IntegrityError:
            # Two tabs subscribing the same browser at once: the other insert
            # won, so the second pass updates its row.
            if attempt:
                raise
    if old_endpoint and old_endpoint != endpoint:
        PushSubscription.objects.filter(user=user, endpoint=old_endpoint).delete()

    # Turning a device on is an explicit "yes": make sure the account-wide
    # switch agrees, or the device would be subscribed and silent.
    NotificationPreference.objects.get_or_create(user=user)
    NotificationPreference.objects.filter(user=user, push_enabled=False).update(push_enabled=True)

    extra = list(
        PushSubscription.objects.filter(user=user).order_by("-created_at", "-pk").values_list("pk", flat=True)[MAX_DEVICES:]
    )
    if extra:
        PushSubscription.objects.filter(pk__in=extra).delete()
    return subscription


def unsubscribe(user, endpoint: str) -> int:
    """Forget one of ``user``'s devices. Returns how many rows went (0 or 1)."""
    if not isinstance(endpoint, str) or not endpoint.strip():
        return 0
    removed, _ = PushSubscription.objects.filter(user=user, endpoint=endpoint.strip()).delete()
    return removed


def device_count(user) -> int:
    return PushSubscription.objects.filter(user=user).count()


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _preferences(user) -> NotificationPreference:
    prefs, _ = NotificationPreference.objects.get_or_create(user=user)
    return prefs


def _category_allowed(prefs: NotificationPreference, category) -> bool:
    if not category:
        return True
    if category == ANSWER:
        return prefs.push_answers
    # The same switches the in-app and Telegram alerts obey.
    from AI.services.notification_service import _category_enabled

    return _category_enabled(prefs, category)


def wanted(user, category=None) -> bool:
    """Would ``user`` want a push in ``category`` right now (ignoring devices)?"""
    if not is_configured() or user is None or not getattr(user, "pk", None):
        return False
    prefs = _preferences(user)
    return prefs.push_enabled and _category_allowed(prefs, category)


def _safe_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("/") and not url.startswith("//"):
        return url[:500]
    if url.startswith(("https://", "http://")):
        return url[:500]
    return "/chat/"


def _topic(tag: str) -> str:
    """A push-service Topic (collapses undelivered messages with the same one)."""
    return re.sub(r"[^A-Za-z0-9_-]", "", tag or "")[:32]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def payload(title: str, body: str = "", *, url: str = "/chat/", tag: str = "") -> dict:
    """What the service worker receives — small, and nothing it has to trust."""
    return {
        "title": _clip(title, _TITLE_MAX) or settings.BRAND.get("full_name", ""),
        "body": _clip(body, _BODY_MAX),
        "url": _safe_url(url),
        "tag": (tag or "")[:64],
    }


def _host(endpoint: str) -> str:
    """The push service's host — the only part of an endpoint safe to log."""
    try:
        return urlparse(endpoint).hostname or "?"
    except ValueError:  # pragma: no cover
        return "?"


def _deliver(subscription: PushSubscription, data: str, *, topic: str = "") -> bool:
    """One device. Returns True when the push service accepted the message."""
    vapid = _vapid()
    if vapid is None or webpush is None:
        return False
    headers = {"Urgency": "normal"}
    if topic:
        headers["Topic"] = topic
    status = None
    try:
        webpush(
            subscription_info={
                "endpoint": subscription.endpoint,
                "keys": {"p256dh": subscription.p256dh, "auth": subscription.auth},
            },
            data=data,
            vapid_private_key=vapid,
            vapid_claims=_claims(),
            ttl=TTL_SECONDS,
            timeout=TIMEOUT_SECONDS,
            headers=headers,
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    except Exception as exc:  # network errors, a corrupt key, …
        status = type(exc).__name__
    else:
        PushSubscription.objects.filter(pk=subscription.pk).update(
            last_success_at=timezone.now(), failure_count=0,
        )
        return True

    if status in (404, 410):
        # The browser unsubscribed or the subscription expired: it will never
        # work again.
        PushSubscription.objects.filter(pk=subscription.pk).delete()
        logger.info("Push device %s (%s) is gone (%s); removed.", subscription.pk, _host(subscription.endpoint), status)
        return False

    failures = (subscription.failure_count or 0) + 1
    if failures >= MAX_FAILURES:
        PushSubscription.objects.filter(pk=subscription.pk).delete()
        logger.warning(
            "Push device %s (%s) failed %s times in a row (last: %s); removed.",
            subscription.pk, _host(subscription.endpoint), failures, status,
        )
    else:
        PushSubscription.objects.filter(pk=subscription.pk).update(failure_count=failures)
        logger.warning("Push to device %s (%s) failed: %s", subscription.pk, _host(subscription.endpoint), status)
    return False


def _send(user, message: dict, *, category=None, respect_preferences: bool = True) -> int:
    if not is_configured() or user is None:
        return 0
    if respect_preferences:
        prefs = _preferences(user)
        if not prefs.push_enabled or not _category_allowed(prefs, category):
            return 0
    devices = list(PushSubscription.objects.filter(user=user).order_by("pk"))
    if not devices:
        return 0
    data = json.dumps(message, ensure_ascii=False)
    topic = _topic(message.get("tag", ""))
    return sum(1 for device in devices if _deliver(device, data, topic=topic))


def send(user, title: str, body: str = "", *, url: str = "/chat/", tag: str = "", category=None) -> int:
    """Push to every device ``user`` turned on. Returns how many accepted it.

    Honours the account-wide ``push_enabled`` switch and, when ``category`` is
    given, the same per-category switch the in-app alert obeys (``"answer"``
    means ``push_answers``). Never raises.
    """
    try:
        return _send(user, payload(title, body, url=url, tag=tag), category=category)
    except Exception:
        logger.exception("Browser notification failed for user=%s", getattr(user, "pk", None))
        return 0


def send_test(user) -> int:
    """A test the student asked for, to every one of their devices."""
    try:
        message = payload(
            "Notifications are working",
            "This is how alerts from the assistant will look on this device.",
            url="/app/settings/#push",
            tag="push-test",
        )
        return _send(user, message, respect_preferences=False)
    except Exception:
        logger.exception("Test notification failed for user=%s", getattr(user, "pk", None))
        return 0


def _spawn(target, *args) -> None:
    """Run ``target`` on a daemon thread that closes its own DB connections."""

    def run():
        from django.db import connections

        try:
            target(*args)
        except Exception:  # pragma: no cover
            logger.exception("Background push job %s failed", getattr(target, "__name__", target))
        finally:
            connections.close_all()

    threading.Thread(target=run, name="web-push", daemon=True).start()


def _send_to(user_id: int, message: dict, category) -> int:
    from AI.models import User

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return 0
    return _send(user, message, category=category)


def queue(user, title: str, body: str = "", *, url: str = "/chat/", tag: str = "", category=None) -> int:
    """:func:`send`, after the current transaction commits, off this thread.

    The cheap checks (keys, switches, any devices at all) happen here, so the
    common case — a student with no devices — starts no thread. Returns how
    many devices it was queued for.
    """
    try:
        if not wanted(user, category):
            return 0
        devices = PushSubscription.objects.filter(user=user).count()
        if not devices:
            return 0
        message = payload(title, body, url=url, tag=tag)
        user_id = user.pk
        transaction.on_commit(lambda: _spawn(_send_to, user_id, message, category))
        return devices
    except Exception:
        logger.exception("Could not queue a browser notification for user=%s", getattr(user, "pk", None))
        return 0


# ---------------------------------------------------------------------------
# "Your answer is ready"
# ---------------------------------------------------------------------------
_CODE_BLOCK = re.compile(r"```.*?(```|$)", re.S)
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MARKS = re.compile(r"(^|\s)[#>]+\s*|[*_~`|]+|^\s*[-+]\s+", re.M)


def snippet(text: str, limit: int = 140) -> str:
    """The start of an answer as plain words: no code, links or markdown."""
    text = _CODE_BLOCK.sub(" [code] ", text or "")
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _MARKS.sub(" ", text)
    text = re.sub(r"\s*\[code\](\s*\[code\])+", " [code]", text)
    return _clip(text, limit)


def answer_ready(user, conversation, message=None) -> int:
    """Tell ``user`` an answer finished while they were away. Returns devices reached.

    Title "Your answer is ready"; the body is the chat's title and the start
    of the answer; a click opens ``/chat/<id>/``. One notification per chat —
    a second answer in the same chat replaces the first rather than stacking.
    Only when ``push_answers`` (and ``push_enabled``) is on. Synchronous and
    never raises: call it after the answer is saved.
    """
    try:
        if conversation is None:
            return 0
        content = getattr(message, "content", message) if message is not None else ""
        chat_title = _clip(getattr(conversation, "title", "") or "Your chat", 60)
        words = snippet(content if isinstance(content, str) else "")
        body = f"{chat_title}: {words}" if words else f"{chat_title} — tap to read it."
        return _send(
            user,
            payload("Your answer is ready", body, url=f"/chat/{conversation.pk}/", tag=f"answer-{conversation.pk}"),
            category=ANSWER,
        )
    except Exception:
        logger.exception("Answer-ready notification failed for user=%s", getattr(user, "pk", None))
        return 0


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------
def announcement_recipients(announcement) -> list[int]:
    """Students in the audience with push on and at least one device."""
    from AI.services.notification_service import audience_students

    audience = audience_students(
        announcement.audience, plan=announcement.audience_plan, model=announcement.audience_model,
    ).exclude(notification_preference__push_enabled=False)
    return list(
        PushSubscription.objects.filter(user__in=audience).values_list("user_id", flat=True).distinct().order_by("user_id")
    )


def announcement_payload(announcement) -> dict:
    url = announcement.action_url if announcement.action_url and announcement.action_label else "/app/notifications/"
    return payload(announcement.title, announcement.body, url=url, tag=f"announcement-{announcement.pk}")


def queue_announcement(announcement) -> int:
    """Push an announcement to its audience in the background. Returns users queued.

    The audience is the one the in-app fan-out used (so the ``announcements``
    switch already applies), less anyone with push switched off. Starts only
    once the publishing transaction has committed.
    """
    try:
        if not is_configured():
            return 0
        user_ids = announcement_recipients(announcement)
        if not user_ids:
            return 0
        transaction.on_commit(lambda: _spawn(send_announcement, announcement.pk, user_ids))
        return len(user_ids)
    except Exception:
        logger.exception("Could not queue announcement %s for browsers", getattr(announcement, "pk", None))
        return 0


def send_announcement(announcement_id: int, user_ids: list[int] | None = None) -> dict:
    """Deliver one announcement to each recipient's devices, paced.

    Re-checks each student's switches at send time and stops if the
    announcement is retracted part-way through.
    """
    from AI.models import Announcement

    counts = {"sent": 0, "skipped": 0, "users": 0}
    announcement = Announcement.objects.filter(pk=announcement_id).first()
    if announcement is None or not is_configured():
        return counts
    if user_ids is None:
        user_ids = announcement_recipients(announcement)
    message = announcement_payload(announcement)
    data = json.dumps(message, ensure_ascii=False)
    topic = _topic(message["tag"])
    for index, user_id in enumerate(user_ids):
        if index and index % 50 == 0 and Announcement.objects.filter(
            pk=announcement.pk, retracted_at__isnull=False
        ).exists():
            break
        prefs = NotificationPreference.objects.filter(user_id=user_id).first()
        if prefs is not None and not (prefs.push_enabled and prefs.announcements):
            counts["skipped"] += 1
            continue
        counts["users"] += 1
        for device in PushSubscription.objects.filter(user_id=user_id).order_by("pk"):
            if _deliver(device, data, topic=topic):
                counts["sent"] += 1
            time.sleep(BROADCAST_PACE_SECONDS)
    logger.info("Announcement %s pushed to browsers: %s", announcement.pk, counts)
    return counts
