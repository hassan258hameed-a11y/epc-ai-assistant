"""Everything that is not the conversation, rendered inside the chat shell.

The chat *is* the application, so Usage, Pricing, Help, Profile, Settings,
Billing and Notifications are regions of it rather than separate pages.  Each
one is a fragment; the shell around it — sidebar, header, token meter, theme —
is rendered once and never torn down.

Two ways in, one template each:

* **soft navigation** — the client asks with ``X-App-Panel: 1`` and gets the
  fragment alone, which it swaps into the panel host and pushes onto history;
* **a cold load or a refresh** — the same URL asks for a whole document, so the
  shell is rendered with the fragment already inside it.

That is what makes a bookmark, a refresh and the back button all land in the
same place as a click, without a second set of templates that could drift.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from django.contrib import messages
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from AI.forms import (
    ChangePasswordForm,
    NotificationPreferenceForm,
    ProfileForm,
    SupportRequestForm,
)
from AI.models import (
    AIModel,
    MessageFeedback,
    Notification,
    OTPPurpose,
    Payment,
    PaymentStatus,
    Plan,
    SavedPrompt,
    SupportTicket,
    TicketCategory,
    TokenTransaction,
    TransactionType,
)
from AI.services import (
    analytics_service,
    library_service,
    organise_service,
    preference_service,
    contact_service,
    model_router,
    notification_service,
    otp_service,
    settings_service,
    support_service,
    telegram_service,
    token_service,
    usage_limit_service,
    wallet_service,
)
from AI.utils import ratelimit
from AI.utils.decorators import login_required_student
from AI.utils.pagination import page_range, paginate
from AI.utils.ratelimit import RateLimited
from AI.views.console_ai import tone_for
from AI.views import chat as chat_views
from AI.views.support import FAQ

logger = logging.getLogger("AI.views.panels")

#: The header a soft navigation sets. Anything else gets a whole document, so a
#: refresh, a bookmark and a search-engine crawler all still work.
PANEL_HEADER = "HTTP_X_APP_PANEL"

#: name -> (template, page title). One row per sidebar destination.
PANELS = {
    "usage": ("app/panels/usage.html", "Usage"),
    "pricing": ("app/panels/pricing.html", "Pricing"),
    "help": ("app/panels/help.html", "Help"),
    "profile": ("app/panels/profile.html", "Profile"),
    "settings": ("app/panels/settings.html", "Settings"),
    "billing": ("app/panels/billing.html", "Billing"),
    "notifications": ("app/panels/notifications.html", "Notifications"),
    "prompts": ("app/panels/prompts.html", "Prompt library"),
    "saved": ("app/panels/saved.html", "Saved answers"),
    "archived": ("app/panels/archived.html", "Archived chats"),
    "trash": ("app/panels/trash.html", "Recently deleted"),
    "models": ("app/panels/models.html", "Compare models"),
    "study": ("app/panels/study.html", "Study"),
}


def wants_fragment(request) -> bool:
    return request.META.get(PANEL_HEADER) == "1"


def render_panel(request, name: str, context: dict, *, status: int = 200) -> HttpResponse:
    """One panel, as a fragment or wrapped in the shell — never as its own page."""
    template, title = PANELS[name]
    context = {**context, "panel": name, "panel_title": title, "nav_active": name}

    if wants_fragment(request):
        return render(request, template, context, status=status)

    shell = chat_views.shell_context(request)
    shell.update(context)
    shell["initial_panel"] = render_to_string(template, context, request=request)
    shell["active_conversation"] = None
    shell["chat_messages"] = []
    shell["is_new_user"] = False
    return render(request, "chat/chat.html", shell, status=status)


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------
def _wallet_rows(user) -> list[dict]:
    """Per-model balances, which is the only view of tokens that means anything.

    A single combined figure would read as healthy while the model the student
    is actually using has nothing left.
    """
    threshold = int(settings_service.get("low_token_threshold", 50))
    rows = []
    for index, row in enumerate(wallet_service.overview(user)):
        model = row["model"]
        if not model.is_active:
            continue
        balance = row["balance"]
        rows.append(
            {
                "key": model.key,
                "name": model.name,
                "provider": model.provider_name,
                "balance": balance,
                "allocated": row["allocated"],
                "used": row["used"],
                "percent_used": row["percent_used"],
                "percent_left": row["percent_left"],
                "usable": row["usable"],
                "reason": row["reason"],
                "is_empty": balance <= 0,
                "is_low": 0 < balance <= threshold,
                # The model's own colour, so a balance card, its meter and the
                # pill on the pricing switch all agree about which model this
                # is. Same palette the console uses.
                "tone": tone_for(model),
                "delay": f"{index * 0.06:.2f}s",
            }
        )
    return rows


#: What each kind of ledger entry looks like on the activity list: the tone
#: it wears and the glyph in its tile. A movement the student did not cause —
#: a grant, a refund — should not look like one they did.
ACTIVITY_TONES = {
    TransactionType.USAGE: ("spend", "M8 2.4 9.4 6.6 13.6 8 9.4 9.4 8 13.6 6.6 9.4 2.4 8 6.6 6.6z"),
    TransactionType.SUBSCRIPTION: ("credit", "M2.6 4.6h10.8v6.8H2.6zM2.6 7.4h10.8"),
    TransactionType.ADMIN_GRANT: ("credit", "M8 3.4v9.2M3.4 8h9.2"),
    TransactionType.FREE_TRIAL: ("credit", "M2.8 7.2h10.4v5.6H2.8zM8 7.2v5.6M5.6 7.2a1.7 1.7 0 1 1 2.4-1.3 1.7 1.7 0 1 1 2.4 1.3"),
    TransactionType.REFERRAL_BONUS: ("credit", "M2.8 6.6h10.4v6.2H2.8zM8 6.6v6.2"),
    TransactionType.REFUND: ("credit", "M13.4 8a5.4 5.4 0 1 1-1.6-3.8M13.4 3.2v3.1h-3.1"),
    TransactionType.EXPIRY: ("spend", "M13 8a5 5 0 1 1-1.5-3.6M13 3.4v3h-3M8 5.2V8l2 1.4"),
}
DEFAULT_ACTIVITY_TONE = ("neutral", "M3.2 12.8V7.5M8 12.8V3.6M12.8 12.8V9")


def _activity_rows(entries, wallets) -> list[dict]:
    """The ledger as the panel draws it: what happened, to which model, when.

    ``source`` on a usage entry is the model key that spent the tokens, so the
    row can name the model rather than leaving a number without a subject.
    """
    names = {row["key"]: row["name"] for row in wallets}
    rows = []
    for index, entry in enumerate(entries):
        kind, glyph = ACTIVITY_TONES.get(entry.transaction_type, DEFAULT_ACTIVITY_TONE)
        rows.append({
            "entry": entry,
            "title": entry.get_transaction_type_display(),
            "detail": entry.description,
            "model": names.get(entry.source, ""),
            "kind": kind,
            "glyph": glyph,
            "amount": f"{'+' if entry.amount > 0 else '−'}{abs(entry.amount):,}",
            "delay": f"{index * 0.04:.2f}s",
        })
    return rows


USAGE_RANGES = (7, 30, 90)


def _usage_filters(request) -> tuple[int, str]:
    try:
        days = int(request.GET.get("days") or 30)
    except ValueError:
        days = 30
    days = days if days in USAGE_RANGES else 30
    model_key = (request.GET.get("model") or "").strip()[:64]
    if model_key and not AIModel.objects.filter(key=model_key).exists():
        model_key = ""
    return days, model_key


@login_required_student
def usage(request):
    user = request.user
    days, model_key = _usage_filters(request)
    stats = analytics_service.student_usage(user, days=days, model_key=model_key)
    by_model = analytics_service.student_usage_by_model(user, days=days)
    wallets = _wallet_rows(user)
    transactions = TokenTransaction.objects.filter(user=user).select_related("payment", "created_by")
    page = paginate(request, transactions, per_page=12)
    activity = _activity_rows(page.object_list, wallets)

    # One block per model the student actually holds something for. The chat
    # sidebar shows only the model in use; this is the whole picture, which is
    # what somebody comes to this screen for.
    limits = []
    for row in wallet_service.overview(user):
        model = row["model"]
        if not model.is_active:
            continue
        snapshot = usage_limit_service.snapshot(user, model)
        if not snapshot.metered:
            continue
        limits.append(
            {
                "model": model,
                "balance": snapshot.balance,
                "spendable": snapshot.spendable,
                "blocked": snapshot.blocked,
                "windows": snapshot.as_payload()["windows_list"],
                "packages": snapshot.packages,
            }
        )

    return render_panel(
        request,
        "usage",
        {
            "wallets": wallets,
            "empty_wallets": sum(1 for row in wallets if row["is_empty"]),
            "limits": limits,
            "summary": token_service.usage_summary(user),
            "stats": stats,
            "days": days,
            "usage_ranges": USAGE_RANGES,
            "model_filter": model_key,
            "model_filter_name": next((row["name"] for row in by_model if row["key"] == model_key), model_key),
            "by_model": by_model,
            "activity": activity,
            "usage_chart": json.dumps(
                {"labels": stats["labels"], "series": [{"name": "Tokens", "values": stats["tokens"]}]}
            ),
            "page": page,
            "page_numbers": page_range(page),
            "subscription": user.active_subscription,
            "consumption": TokenTransaction.objects.filter(
                user=user, transaction_type=TransactionType.USAGE
            )[:8],
        },
    )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------
@login_required_student
def pricing(request):
    """Token packages, grouped by the model they buy tokens for.

    Tokens are per model, so the grouping is not cosmetic: buying the wrong
    card would credit a balance the student was not trying to top up.
    """
    user = request.user
    wallets = {row["key"]: row for row in _wallet_rows(user)}

    plans = (
        Plan.objects.filter(is_active=True, ai_model__isnull=False)
        .select_related("ai_model")
        .order_by("display_order", "tokens", "price")
    )

    groups: dict[str, dict] = {}
    for plan in plans:
        model = plan.ai_model
        group = groups.setdefault(
            model.key,
            {
                "key": model.key,
                "name": model.name,
                "provider": model.provider_name,
                "description": plan.description,
                "wallet": wallets.get(model.key),
                "tone": tone_for(model),
                "plans": [],
            },
        )
        group["plans"].append(plan)

    # Keep the picker's order, so the cards line up with the model list the
    # student already knows from the composer.
    ordered = [groups[row["key"]] for row in _wallet_rows(user) if row["key"] in groups]

    # Which model the switch opens on. A ?model= link wins — that is how the
    # chat sends somebody here to top up the balance they just ran down — then
    # the model they can actually use, then simply the first one.
    requested = (request.GET.get("model") or "").strip()[:64]
    available = [group["key"] for group in ordered]
    selected = requested if requested in available else ""
    if not selected:
        preferred = model_router.preferred_model(user)
        if preferred is not None and preferred.key in available:
            selected = preferred.key
    if not selected and available:
        selected = available[0]

    return render_panel(
        request,
        "pricing",
        {
            "groups": ordered,
            "wallets": _wallet_rows(user),
            "subscription": user.active_subscription,
            "recent_payments": Payment.objects.filter(user=user).select_related("plan")[:5],
            "selected_model": selected,
            "focus_model": requested,
        },
    )


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------
#: A colour and a glyph per question group, so the help desk reads as
#: sections rather than as one long list. Keyed by the group's own name;
#: anything new falls back to the accent.
FAQ_TONES = {
    "Getting started": ("var(--accent-soft)", "var(--accent)",
                        "M8 2.4 9.4 6.6 13.6 8 9.4 9.4 8 13.6 6.6 9.4 2.4 8 6.6 6.6z"),
    "Tokens and usage": ("var(--surface-3)", "var(--accent-2)",
                         "M8 2.6c2.9 0 5.2 1 5.2 2.2S10.9 7 8 7 2.8 6 2.8 4.8 5.1 2.6 8 2.6M2.8 4.8v6.4c0 1.2 2.3 2.2 5.2 2.2s5.2-1 5.2-2.2V4.8"),
    "Usage limits": ("var(--warn-soft)", "var(--warn)", "M2.6 11.4a5.4 5.4 0 0 1 10.8 0M8 11.4 10.6 7"),
    "Images": ("var(--danger-soft)", "var(--danger)", "M2.6 3.8h10.8v8.4H2.6zM2.6 10l3-2.8 2.4 1.8 2.6-3.2 2.8 3"),
    "Billing and account": ("var(--ok-soft)", "var(--ok)", "M2.6 4.6h10.8v6.8H2.6zM2.6 7.4h10.8"),
    "Files and voice": ("var(--surface-3)", "var(--ink-2)",
                        "M4.4 2.6h5l2.2 2.2v8.6H4.4zM9.4 2.6v2.4h2.2"),
}
DEFAULT_FAQ_TONE = ("var(--accent-soft)", "var(--accent)", "M8 4.4v4.4M8 11.2v.4")


def _faq_groups() -> list[dict]:
    groups = []
    for section in FAQ:
        background, colour, glyph = FAQ_TONES.get(section["group"], DEFAULT_FAQ_TONE)
        groups.append({
            "group": section["group"],
            "items": section["items"],
            "count": len(section["items"]),
            "bg": background,
            "fg": colour,
            "glyph": glyph,
        })
    return groups


def _help_context(user, form=None) -> dict:
    contact = contact_service.support()
    return {
        "faq": _faq_groups(),
        "form": form if form is not None else SupportRequestForm(user=user),
        "tickets": SupportTicket.objects.filter(user=user)[:8],
        "open_tickets": SupportTicket.objects.filter(
            user=user, status__in=["new", "open", "in_progress", "waiting_user"]
        ).count(),
        "categories": TicketCategory.choices,
        # The four the design offers as chips, in its order, each with the
        # colour it wears when chosen. Everything else stays in the select.
        "topics": [
            {"value": TicketCategory.TECHNICAL, "label": "Technical issue", "from": "#FB7185", "to": "#E11D48"},
            {"value": TicketCategory.PAYMENT, "label": "Payment or tokens", "from": "#60A5FA", "to": "#2563EB"},
            {"value": TicketCategory.ACCOUNT, "label": "Account access", "from": "#A78BFA", "to": "#7C3AED"},
            {"value": TicketCategory.OTHER, "label": "Something else", "from": "#34D399", "to": "#0A7F5C"},
        ],
        # Contact routes are configuration, so the page renders whatever the
        # administrator has actually filled in and silently omits the rest.
        "whatsapp": contact["whatsapp"],
        "support_email": contact["email"],
        "support_telegram": contact["telegram"],
        "support_hours": contact["hours"],
        # The bot, the channel and the group, for the Help page's Telegram tiles.
        "telegram_bot_url": telegram_service.bot_url() if telegram_service.is_configured() else "",
        "telegram_channel_url": telegram_service.channel_url(),
        "telegram_channel_title": telegram_service.channel_title(),
        "telegram_group_url": telegram_service.group_url(),
        "telegram_group_title": telegram_service.group_title(),
    }


@login_required_student
def help_panel(request):
    return render_panel(request, "help", _help_context(request.user))


@login_required_student
@require_POST
def help_submit(request):
    """Open a support ticket without leaving the shell."""
    user = request.user
    try:
        ratelimit.enforce("support_request", str(user.pk), limit=6, window=3600)
    except RateLimited as exc:
        messages.error(request, exc.friendly_message)
        return render_panel(request, "help", _help_context(user), status=429)

    form = SupportRequestForm(request.POST, request.FILES, user=user)
    if not form.is_valid():
        messages.error(request, "Please check the highlighted fields.")
        return render_panel(request, "help", _help_context(user, form), status=400)

    try:
        support_service.create_ticket(
            user, data=form.cleaned_data, attachment=form.cleaned_data.get("attachment")
        )
    except Exception:  # pragma: no cover — never show a traceback
        logger.exception("Support ticket creation failed for user=%s", user.pk)
        messages.error(request, "We could not open that request. Please try again.")
        return render_panel(request, "help", _help_context(user, form), status=500)

    messages.success(request, "Your request has been sent. We will reply by email.")
    return render_panel(request, "help", _help_context(user))


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------
def _profile_context(user, *, profile_form=None, password_form=None) -> dict:
    return {
        "profile_form": profile_form or ProfileForm(instance=user),
        "password_form": password_form or ChangePasswordForm(user=user),
        "subscription": user.active_subscription,
        "wallets": _wallet_rows(user),
    }


@login_required_student
def profile(request):
    user = request.user
    if request.method != "POST":
        return render_panel(request, "profile", _profile_context(user))

    action = request.POST.get("action")

    if action == "password":
        form = ChangePasswordForm(request.POST, user=user)
        if not form.is_valid():
            messages.error(request, "Please check the highlighted fields.")
            return render_panel(request, "profile", _profile_context(user, password_form=form), status=400)

        from django.contrib.auth import update_session_auth_hash

        user.set_password(form.cleaned_data["new_password"])
        user.must_change_password = False
        user.save(update_fields=["password", "must_change_password"])
        # Keep the current session alive across the hash change, and keep the
        # single-session lock pointing at the key that is still valid.
        update_session_auth_hash(request, user)
        type(user).objects.filter(pk=user.pk).update(active_session_key=request.session.session_key or "")
        messages.success(request, "Your password has been changed.")
        return render_panel(request, "profile", _profile_context(user))

    form = ProfileForm(request.POST, instance=user)
    if not form.is_valid():
        messages.error(request, "Please check the highlighted fields.")
        return render_panel(request, "profile", _profile_context(user, profile_form=form), status=400)
    form.save()
    messages.success(request, "Your profile has been updated.")
    return render_panel(request, "profile", _profile_context(user))


@login_required_student
def usage_export(request):
    """Daily usage as CSV: one row per day and model, for the chosen range."""
    import csv
    import io
    from datetime import timedelta

    from django.db.models import Count, Sum
    from django.db.models.functions import TruncDate

    from AI.models import AIUsageLog

    days, model_key = _usage_filters(request)
    start = timezone.now() - timedelta(days=days - 1)
    logs = AIUsageLog.objects.filter(user=request.user, created_at__gte=start, success=True)
    if model_key:
        logs = logs.filter(model_key=model_key)
    rows = (
        logs.annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "model_key")
        .annotate(requests=Count("id"), input_tokens=Sum("input_tokens"), output_tokens=Sum("output_tokens"),
                  tokens=Sum("total_tokens"))
        .order_by("day", "model_key")
    )
    names = dict(AIModel.objects.values_list("key", "name"))
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Model", "Requests", "Input tokens", "Output tokens", "Total tokens"])
    for row in rows:
        writer.writerow([row["day"].isoformat(), names.get(row["model_key"], row["model_key"]), row["requests"],
                         row["input_tokens"] or 0, row["output_tokens"] or 0, row["tokens"] or 0])
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="my-usage-{days}-days-{timezone.localdate().isoformat()}.csv"'
    return response


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
#: The six colour themes, as the header's picker offers them.
PALETTES = [
    ("indigo", "#4F5BD5", "#8B5CF6", "Indigo"),
    ("teal", "#0E8F83", "#3A9FD6", "Teal"),
    ("graphite", "#414C5E", "#7C8899", "Graphite"),
    ("plum", "#7C4DBF", "#C2489B", "Plum"),
    ("ember", "#B1542F", "#D69235", "Ember"),
    ("forest", "#2E7D55", "#7BA33C", "Forest"),
]
GOAL_CHOICES = [(0, "No goal"), (3, "3 answers"), (5, "5 answers"), (10, "10 answers"),
                (20, "20 answers"), (30, "30 answers"), (50, "50 answers")]


def _settings_context(user, *, preference_form=None, personal=None) -> dict:
    from AI.models import ChatPreference

    return {
        "palettes": PALETTES,
        "goal_choices": GOAL_CHOICES,
        # Models this student can actually start a chat on.
        "default_models": [row["model"] for row in wallet_service.overview(user)
                           if row["model"].is_active and row["usable"]],
        "personal": personal or preference_service.get(user),
        "personal_enabled": preference_service.enabled(),
        "styles": ChatPreference.Style.choices,
        "max_about": preference_service.MAX_ABOUT,
        "max_instructions": preference_service.MAX_INSTRUCTIONS,
        "preference_form": preference_form or NotificationPreferenceForm(
            instance=notification_service.preferences(user)
        ),
        **_telegram_context(user),
        **_security_context(user),
        **_push_context(user),
    }


def _push_context(user) -> dict:
    """The "Notifications on this device" card. Whether *this* browser is on
    is only known in the browser (static/js/push.js); the server supplies the
    key, whether push is set up at all, and the account's switches."""
    from AI.services import push_service

    prefs = notification_service.preferences(user)
    return {
        "push_configured": push_service.is_configured(),
        "push_public_key": push_service.public_key(),
        "push_prefs": prefs,
        # What a device that is turned on will be sent, per the switches.
        "push_kinds": [
            ("Answers ready", prefs.push_answers),
            ("Payment updates", prefs.payment_alerts),
            ("Support replies", True),
            ("Low tokens", prefs.low_token_alerts),
            ("Plan reminders", prefs.expiry_reminders),
            ("Announcements", prefs.announcements),
        ],
    }


def _push_action(request, user) -> None:
    prefs = notification_service.preferences(user)
    prefs.push_answers = request.POST.get("push_answers") == "on"
    prefs.save(update_fields=["push_answers"])
    messages.success(
        request,
        "Saved. We'll tell you when an answer is ready." if prefs.push_answers
        else "Saved. No more “answer ready” notifications.",
    )


def _telegram_context(user) -> dict:
    """The Telegram card's state, decided here so the template only draws it.

    ``linked`` — a private chat is connected; ``blocked`` — connected, but the
    student blocked the bot; ``pending`` — a fresh connect link is live;
    ``idle`` — nothing yet (or the last link expired). Opening Settings never
    mints a link: only pressing Connect does.
    """
    from AI.utils import qr

    account = telegram_service.get_or_create_link(user)
    linked = account.is_linked and telegram_service.is_private_account(account)
    if linked and account.blocked_at:
        state = "blocked"
    elif linked:
        state = "linked"
    elif telegram_service.link_is_valid(account) and telegram_service.bot_username():
        state = "pending"
    else:
        state = "idle"

    link = telegram_service.deep_link(account) if state == "pending" else ""
    ttl_seconds = telegram_service.link_ttl_minutes() * 60
    seconds_left = min(ttl_seconds, telegram_service.link_seconds_left(account)) if link else 0
    return {
        "telegram": account,
        "telegram_configured": telegram_service.is_configured(),
        "telegram_state": state,
        "telegram_link": link,
        "telegram_qr": qr.svg(link, dark="currentColor") if link else "",
        # The countdown bar: it runs for the link's whole life and starts
        # part-way through, so it empties exactly when the link dies.
        "telegram_seconds_left": seconds_left,
        "telegram_elapsed": ttl_seconds - seconds_left if link else 0,
        "telegram_ttl_seconds": ttl_seconds,
        "telegram_minutes_left": -(-seconds_left // 60),
        "telegram_ttl": telegram_service.link_ttl_minutes(),
        "telegram_bot_url": telegram_service.bot_url(),
        "telegram_bot_name": telegram_service.bot_username(),
        "telegram_channel_url": telegram_service.channel_url(),
        "telegram_channel_title": telegram_service.channel_title(),
        "telegram_group_url": telegram_service.group_url(),
        "telegram_group_title": telegram_service.group_title(),
        "telegram_prefs": notification_service.preferences(user),
    }


def _login_code_route(user) -> str:
    """Where a sign-in code would go right now: "telegram", "email" or ""."""
    if otp_service.telegram_target(user) is not None:
        return "telegram"
    if user.email and user.is_verified:
        return "email"
    return ""


def _security_context(user) -> dict:
    from AI.utils.request import mask_email

    route = _login_code_route(user)
    return {
        "login_otp_enabled": user.login_otp_enabled,
        "login_otp_route": route,
        "login_otp_email": mask_email(user.email) if user.email else "",
        # The administrator's rule: on for every student, not theirs to change.
        "login_otp_required": user.is_student and otp_service.students_must_use_codes(),
        # Telegram and e-mail both get the same code.
        "login_otp_both": (route == "telegram" and bool(user.email)
                           and otp_service.sends_everywhere(OTPPurpose.LOGIN)),
    }


def _telegram_action(request, user, action: str) -> None:
    """Every Telegram button on the card. Messages say what happened."""
    if not telegram_service.is_configured():
        messages.error(request, "Telegram isn't available on this platform right now.")
        return

    if action in {"telegram_connect", "telegram_refresh"}:
        try:
            ratelimit.enforce("otp_send", f"tg-link:{user.pk}", limit=10, window=600)
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return
        account = telegram_service.get_or_create_link(user)
        if account.is_linked and telegram_service.is_private_account(account):
            messages.info(request, "Telegram is already connected. Disconnect it first to use a different chat.")
            return
        telegram_service.issue_link_token(user)
        messages.success(
            request,
            f"Your link is ready. It works once and expires in {telegram_service.link_ttl_minutes()} minutes.",
        )

    elif action == "telegram_check":
        account = telegram_service.get_or_create_link(user)
        if account.is_linked:
            handle = f" as @{account.username}" if account.username else ""
            messages.success(request, f"Connected to Telegram{handle}.")
        else:
            messages.info(request, "Not connected yet. Press Start in the Telegram chat, then check again.")

    elif action == "telegram_unlink":
        account = telegram_service.linked_account(user)
        if account is None:
            messages.info(request, "Telegram was not connected.")
            return
        changes = telegram_service.unlink(account)
        messages.success(request, "Telegram has been disconnected.")
        if changes["login_otp_disabled"]:
            messages.warning(
                request,
                "Sign-in codes are now off: Telegram was the only place they could go. "
                "Verify an e-mail address or reconnect Telegram to turn them back on.",
            )

    elif action == "telegram_test":
        try:
            ratelimit.enforce("otp_send", f"tg-test:{user.pk}", limit=3, window=600)
        except RateLimited as exc:
            messages.error(request, exc.friendly_message)
            return
        account = telegram_service.linked_account(user)
        if not telegram_service.can_receive_private(account):
            messages.error(
                request,
                "You blocked the bot. Open it in Telegram and press Start, then try again."
                if account and account.blocked_at else "Connect Telegram first.",
            )
            return
        delivery = telegram_service.deliver(
            account.telegram_id,
            "<b>Test message</b>\n\nTelegram alerts are working. You can switch them off any time in "
            "Settings → Telegram.",
            user=user, account=account, kind="test",
        )
        if delivery.status == "sent":
            messages.success(request, "Test message sent — check Telegram.")
        elif delivery.status == "blocked":
            messages.error(request, "Telegram says you blocked the bot. Open it, press Start, then try again.")
        else:
            messages.error(request, "Telegram didn't accept the message. Please try again in a minute.")

    elif action == "telegram_prefs":
        prefs = notification_service.preferences(user)
        prefs.telegram_enabled = request.POST.get("telegram_enabled") == "on"
        prefs.announcements = request.POST.get("announcements") == "on"
        prefs.save(update_fields=["telegram_enabled", "announcements"])
        messages.success(request, "Saved your Telegram preferences.")


def _security_action(request, user) -> None:
    if user.is_student and otp_service.students_must_use_codes():
        messages.info(request, "Sign-in codes are required for every student, so this can't be switched off.")
        return
    wanted = request.POST.get("login_otp_enabled") == "on"
    if wanted and not user.login_otp_enabled and not _login_code_route(user):
        messages.error(
            request,
            "Sign-in codes need somewhere to go: verify your e-mail address or connect Telegram first.",
        )
        return
    if wanted != user.login_otp_enabled:
        user.login_otp_enabled = wanted
        user.save(update_fields=["login_otp_enabled"])
    messages.success(
        request,
        "Sign-in codes are on. You'll be asked for one after your password next time."
        if wanted else "Sign-in codes are off.",
    )


@login_required_student
def account_settings(request):
    user = request.user
    if request.method != "POST":
        context = _settings_context(user)
        # Back from a failed "Delete my account": show that section open, next
        # to its error, instead of collapsed at the bottom of the page.
        context["open_close_account"] = request.GET.get("close_error") == "1"
        return render_panel(request, "settings", context)

    action = request.POST.get("action")

    if action == "personalise":
        if not preference_service.enabled():
            messages.error(request, "Custom instructions are turned off on this platform.")
            return render_panel(request, "settings", _settings_context(user))
        try:
            preference_service.save(
                user,
                about_me=request.POST.get("about_me", ""),
                instructions=request.POST.get("instructions", ""),
                style=request.POST.get("style", ""),
                language=request.POST.get("language", ""),
                enabled=request.POST.get("enabled") == "on",
            )
            messages.success(request, "Saved. Your next answers will follow these preferences.")
        except preference_service.PreferenceError as exc:
            messages.error(request, exc.friendly_message)
        return render_panel(request, "settings", _settings_context(user))

    if action == "defaults":
        try:
            goal = int(request.POST.get("daily_goal") or 0)
        except ValueError:
            goal = 0
        try:
            preference_service.save_defaults(
                user, default_model_key=request.POST.get("default_model_key", ""), daily_goal=goal,
            )
            messages.success(request, "Saved. New chats start the way you chose, on every device.")
        except preference_service.PreferenceError as exc:
            messages.error(request, exc.friendly_message)
        return render_panel(request, "settings", _settings_context(user))

    if action and action.startswith("telegram_"):
        _telegram_action(request, user, action)

    elif action == "security":
        _security_action(request, user)

    elif action == "push_prefs":
        _push_action(request, user)

    else:
        form = NotificationPreferenceForm(request.POST, instance=notification_service.preferences(user))
        if not form.is_valid():
            messages.error(request, "Please check the highlighted fields.")
            return render_panel(request, "settings", _settings_context(user, preference_form=form), status=400)
        form.save()
        messages.success(request, "Your preferences have been saved.")

    return render_panel(request, "settings", _settings_context(user))


@login_required_student
def account_export(request):
    """Everything of the student's, as one JSON download."""
    from AI.services import privacy_service

    data = privacy_service.export_data(request.user)
    response = HttpResponse(
        json.dumps(data, ensure_ascii=False, indent=2), content_type="application/json; charset=utf-8",
    )
    stamp = timezone.localdate().isoformat()
    response["Content-Disposition"] = f'attachment; filename="my-data-{stamp}.json"'
    return response


@login_required_student
@require_POST
def account_close(request):
    """Close the account for good. A real form post: success leaves the app."""
    from django.contrib.auth import logout

    from AI.services import privacy_service

    try:
        privacy_service.close_account(
            request.user, password=request.POST.get("password", ""),
            confirm=request.POST.get("confirm", ""), request=request,
        )
    except privacy_service.PrivacyError as exc:
        messages.error(request, exc.friendly_message)
        return redirect(f"{reverse('student:settings')}?close_error=1#privacy")
    logout(request)
    messages.success(request, "Your account has been closed and your personal data deleted.")
    return redirect("public:home")


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------
@login_required_student
def billing(request):
    user = request.user
    payments = Payment.objects.filter(user=user).select_related("plan").order_by("-created_at")
    page = paginate(request, payments, per_page=12)
    paid = payments.filter(status=PaymentStatus.PAID)

    return render_panel(
        request,
        "billing",
        {
            "account": token_service.get_account(user),
            "subscription": user.active_subscription,
            "page": page,
            "page_numbers": page_range(page),
            "wallets": _wallet_rows(user),
            "totals": {
                "spent": paid.aggregate(total=Sum("amount"))["total"] or Decimal("0"),
                "invoices": paid.count(),
                "pending": payments.filter(status=PaymentStatus.PENDING).count(),
                "tokens_bought": paid.aggregate(total=Sum("tokens_to_grant"))["total"] or 0,
            },
        },
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
#: What each level looks like: the colour pair the card's edge, icon and
#: button wear, and the glyph in the tile. An alert must not look like a
#: receipt.
NOTIFICATION_TONES = {
    "info": {"from": "#60A5FA", "to": "#2563EB", "line": "rgba(37,99,235,.22)", "glyph": "M8 7.2v4.2M8 4.9v.2"},
    "success": {"from": "#34D399", "to": "#059669", "line": "rgba(5,150,105,.22)", "glyph": "M3.6 8.4 6.6 11.4l5.8-6.8"},
    "warning": {"from": "#FBBF24", "to": "#B07908", "line": "rgba(176,121,8,.22)", "glyph": "M8 2.6 14 13H2zM8 6.4v3M8 10.8v.4"},
    "danger": {"from": "#FB7185", "to": "#E11D48", "line": "rgba(225,29,72,.22)", "glyph": "M8 2.6 14 13H2zM8 6.4v3M8 10.8v.4"},
}


@login_required_student
def notifications(request):
    user = request.user
    rows = notification_service.inbox(user)
    # "Unread" is a real filter rather than a client-side hide: a student with
    # a hundred read alerts should not have to download them to see the two
    # that still need them.
    unread_only = request.GET.get("filter") == "unread"
    if unread_only:
        rows = rows.filter(is_read=False)
    page = paginate(request, rows, per_page=20)

    cards = []
    for index, note in enumerate(page.object_list):
        cards.append({
            "note": note,
            "tone": NOTIFICATION_TONES.get(note.level, NOTIFICATION_TONES["info"]),
            "delay": f"{index * 0.06:.2f}s",
        })

    return render_panel(
        request,
        "notifications",
        {
            "page": page,
            "page_numbers": page_range(page),
            "cards": cards,
            "unread": notification_service.unread_count(user),
            "unread_only": unread_only,
        },
    )


@login_required_student
@require_POST
def notifications_read_all(request):
    Notification.objects.filter(user=request.user, is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    return notifications(request)


# ---------------------------------------------------------------------------
# Prompt library
# ---------------------------------------------------------------------------
@login_required_student
def prompts(request):
    """Featured prompts and the student's own, with a form to add one.

    POST carries ``action`` = ``create`` or ``delete`` and answers with the
    re-rendered panel, the way every other panel form does.
    """
    user = request.user
    draft = {"title": "", "body": "", "category": ""}
    editing = None
    edit_id = request.GET.get("edit", "")
    if request.method == "GET" and edit_id.isdigit():
        editing = SavedPrompt.objects.filter(pk=edit_id, user=user).first()
        if editing:
            draft = {"title": editing.title, "body": editing.body, "category": editing.category}
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "delete":
                library_service.delete_prompt(user, request.POST.get("prompt_id"))
                messages.success(request, "Prompt deleted.")
            elif action == "update":
                draft = {key: request.POST.get(key, "") for key in draft}
                try:
                    prompt = library_service.update_prompt(user, request.POST.get("prompt_id"), **draft)
                except library_service.LibraryError:
                    editing = SavedPrompt.objects.filter(pk=request.POST.get("prompt_id") or 0, user=user).first()
                    raise
                messages.success(request, f"Updated “{prompt.title}”.")
                draft = {"title": "", "body": "", "category": ""}
            else:
                draft = {key: request.POST.get(key, "") for key in draft}
                prompt = library_service.create_prompt(user, **draft)
                messages.success(request, f"Saved “{prompt.title}” to your library.")
                draft = {"title": "", "body": "", "category": ""}
        except library_service.LibraryError as exc:
            messages.error(request, exc.friendly_message)

    library = library_service.prompts_for(user)
    categories = sorted({p.category for p in library["featured"] if p.category})
    return render_panel(
        request,
        "prompts",
        {
            "featured": library["featured"],
            "mine": library["mine"],
            "categories": categories,
            "draft": draft,
            "editing": editing,
            "max_prompts": library_service.MAX_PROMPTS_PER_STUDENT,
            "max_body": library_service.MAX_BODY,
        },
    )


# ---------------------------------------------------------------------------
# Saved answers
# ---------------------------------------------------------------------------
@login_required_student
def saved_answers(request):
    """Every answer the student bookmarked, newest first, searchable.

    POST ``remove`` takes one off the list and re-renders the panel.
    """
    user = request.user
    if request.method == "POST" and request.POST.get("remove", "").isdigit():
        organise_service.remove_bookmark(user, int(request.POST["remove"]))
        messages.success(request, "Removed from saved answers.")
    elif request.method == "POST" and request.POST.get("note_for", "").isdigit():
        # A few words on why it was kept — "use for essay 2" — searchable below.
        from AI.models import MessageBookmark

        note = " ".join((request.POST.get("note") or "").split())[:200]
        updated = MessageBookmark.objects.filter(user=user, message_id=int(request.POST["note_for"])).update(note=note)
        if updated:
            messages.success(request, "Note saved." if note else "Note removed.")
    query = (request.GET.get("q") or request.POST.get("q") or "").strip()[:100]
    page = paginate(request, organise_service.bookmarks_for(user, query=query), per_page=20)
    names = dict(AIModel.objects.values_list("key", "name"))
    for item in page.object_list:
        item.model_name = names.get(item.message.model_key, item.message.model_key)
    return render_panel(
        request, "saved",
        {"page": page, "page_numbers": page_range(page), "items": page.object_list, "query": query},
    )


# ---------------------------------------------------------------------------
# Archived chats
# ---------------------------------------------------------------------------
@login_required_student
def archived_chats(request):
    """Chats the student archived: restore them to the sidebar, or delete them.

    POST ``action`` = ``restore`` | ``delete`` with ``conversation_id``. Only the
    student's own archived chats are ever touched. Delete moves the chat to
    Recently deleted, like everywhere else, so it can still be restored.
    """
    from AI.models import Conversation
    from AI.services import trash_service

    user = request.user
    changed = False
    if request.method == "POST":
        raw_id = request.POST.get("conversation_id") or ""
        conversation = Conversation.objects.filter(
            pk=int(raw_id) if raw_id.isdigit() else 0, user=user, is_archived=True,
        ).first()
        action = request.POST.get("action")
        if conversation is None:
            messages.error(request, "That chat could not be found. It may have been moved already.")
        elif action == "restore":
            conversation.is_archived = False
            conversation.save(update_fields=["is_archived", "updated_at"])
            messages.success(request, f"\u201c{conversation.title}\u201d is back in your chats.")
            changed = True
        elif action == "delete":
            trash_service.trash(user, conversation.pk)
            messages.success(
                request,
                f"\u201c{conversation.title}\u201d moved to Recently deleted. "
                f"You can restore it for {trash_service.TRASH_DAYS} days.",
            )
            changed = True

    query = (request.GET.get("q") or request.POST.get("q") or "").strip()[:100]
    rows = Conversation.objects.filter(user=user, is_archived=True).select_related("folder").order_by("-updated_at")
    if query:
        rows = rows.filter(title__icontains=query)
    page = paginate(request, rows, per_page=25)
    return render_panel(
        request, "archived",
        {"page": page, "items": page.object_list, "query": query,
         "total": Conversation.objects.filter(user=user, is_archived=True).count(),
         "refresh_sidebar": changed},
    )


@login_required_student
def trash(request):
    """Recently deleted: restore a chat, delete one for good, or empty it.

    POST ``action`` = ``restore`` | ``purge`` (with ``conversation_id``) or
    ``empty``. Chats here are purged automatically after
    ``trash_service.TRASH_DAYS`` days.
    """
    from AI.services import trash_service

    user = request.user
    changed = False
    if request.method == "POST":
        action = request.POST.get("action")
        raw_id = request.POST.get("conversation_id") or ""
        conversation_id = int(raw_id) if raw_id.isdigit() else 0
        if action == "restore":
            conversation = trash_service.restore(user, conversation_id)
            if conversation is None:
                messages.error(request, "That chat is no longer in Recently deleted.")
            else:
                where = "Archived chats" if conversation.is_archived else "your chats"
                messages.success(request, f"\u201c{conversation.title}\u201d is back in {where}.")
                changed = True
        elif action == "purge":
            if trash_service.purge_for(user, conversation_id):
                messages.success(request, "Deleted for good.")
                changed = True
            else:
                messages.error(request, "That chat is no longer in Recently deleted.")
        elif action == "empty":
            count = trash_service.empty(user)
            messages.success(request, f"Deleted {count} chat{'s' if count != 1 else ''} for good.")
            changed = True
        else:
            messages.error(request, "Unknown action.")

    rows = trash_service.items(user)
    return render_panel(
        request, "trash",
        {"items": rows, "total": len(rows), "trash_days": trash_service.TRASH_DAYS, "refresh_sidebar": changed},
    )


# ---------------------------------------------------------------------------
# Compare models
# ---------------------------------------------------------------------------
def _thinks_first(model) -> bool:
    """Whether the vendor client treats this model as a reasoning model.

    Asked of the provider rather than guessed from the name here, so this page
    and the request that is actually sent can never disagree.
    """
    from AI.services import providers

    try:
        client = providers.for_model(model)
    except Exception:
        return False
    return bool(
        getattr(client, "is_reasoning_model", False)
        or getattr(client, "uses_adaptive_thinking", False)
    )


@login_required_student
def models_compare(request):
    """Every model side by side: what it costs, what it can do, what you hold.

    Prices are the live packages, so this page can never disagree with Pricing.
    The satisfaction figure is what students actually said about each model's
    answers — shown only once enough people have rated it to mean something.
    """
    from datetime import timedelta
    from django.db.models import Count, Q

    user = request.user
    wallets = {row["key"]: row for row in _wallet_rows(user)}
    models = list(
        AIModel.objects.filter(is_active=True)
        .select_related("provider_ref")
        .order_by("sort_order", "name")
    )

    standard = {}
    cheapest = {}
    for plan in Plan.objects.filter(is_active=True, ai_model__in=models).order_by("price"):
        cheapest.setdefault(plan.ai_model_id, plan)
        if plan.tokens == 100_000:
            standard.setdefault(plan.ai_model_id, plan)

    since = timezone.now() - timedelta(days=90)
    votes = {
        row["model_key"]: row
        for row in MessageFeedback.objects.filter(updated_at__gte=since)
        .values("model_key")
        .annotate(up=Count("id", filter=Q(rating__gt=0)), total=Count("id"))
    }

    rows = []
    for index, model in enumerate(models):
        plan = standard.get(model.pk) or cheapest.get(model.pk)
        per_100k = (
            (plan.price / Decimal(plan.tokens) * Decimal(100_000)).quantize(Decimal("0.01"))
            if plan and plan.tokens else None
        )
        vote = votes.get(model.key, {"up": 0, "total": 0})
        wallet = wallets.get(model.key)
        rows.append({
            "model": model,
            "tone": tone_for(model),
            "wallet": wallet,
            "per_100k": per_100k,
            "plan": plan,
            "reasoning": _thinks_first(model),
            "vision": model.supports_vision,
            "reply_budget": model.max_output_tokens,
            "satisfaction": round(vote["up"] / vote["total"] * 100) if vote["total"] >= 5 else None,
            "ratings": vote["total"],
            "delay": f"{index * 0.05:.2f}s",
        })

    prices = [row["per_100k"] for row in rows if row["per_100k"] is not None]
    top = max(prices) if prices else None
    low = min(prices) if prices else None
    for row in rows:
        row["cost_percent"] = (
            max(6, round(row["per_100k"] / top * 100)) if row["per_100k"] is not None and top else 0
        )
        row["is_best_value"] = row["per_100k"] is not None and row["per_100k"] == low
        row["is_most_capable"] = row["per_100k"] is not None and row["per_100k"] == top

    return render_panel(request, "models", {"rows": rows, "selected": request.GET.get("model", "")})


# ---------------------------------------------------------------------------
# Legacy standalone pages
# ---------------------------------------------------------------------------
def legacy_redirect(panel: str):
    """Send an old bookmark into the shell instead of to a page of its own.

    The old URLs stay valid — they simply resolve to the same content in the
    place it now lives, which is the one thing a bookmark is for.
    """

    @login_required_student
    def view(request, *args, **kwargs):
        query = request.META.get("QUERY_STRING", "")
        target = f"/app/{panel}/"
        return redirect(f"{target}?{query}" if query else target)

    view.__name__ = f"legacy_{panel}"
    return view
