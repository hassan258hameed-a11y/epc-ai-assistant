"""Template context available on every page."""
from __future__ import annotations

from django.conf import settings

from AI.models import Notification, OPEN_STATUSES, SupportTicket
from AI.services import settings_service


def branding(request) -> dict:
    app_settings = settings_service.all_settings()
    brand = dict(settings.BRAND)
    brand["full_name"] = app_settings.get("app_name") or brand["full_name"]
    brand["support_email"] = app_settings.get("support_email") or brand["support_email"]
    brand["support_telegram"] = app_settings.get("support_telegram") or brand["support_telegram"]
    return {
        "BRAND": brand,
        "APP_SETTINGS": app_settings,
        "SITE_URL": settings.SITE_URL,
    }


def user_context(request) -> dict:
    """Balance and unread badge for the signed-in student."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"token_balance": 0, "unread_notifications": 0}

    # Scheduled announcements go out even where the hourly command is not set
    # up: checked here at most once a minute, whoever happens to be browsing.
    from AI.services import notification_service

    notification_service.publish_due_throttled()

    # Likewise, open payments are re-checked and stale ones expired (off-thread).
    from AI.services import payment_service

    payment_service.sweep_throttled()

    account = getattr(user, "token_account", None)
    balance = account.balance if account else 0
    context = {
        "token_balance": balance,
        "token_allocated": account.allocation if account else 0,
        "token_used": account.used_in_cycle if account else 0,
        "token_percent": account.remaining_percent if account else 0,
        "unread_notifications": Notification.objects.filter(user=user, is_read=False).count(),
        "low_token_threshold": int(settings_service.get("low_token_threshold", 5000)),
    }
    context["tokens_are_low"] = 0 < balance <= context["low_token_threshold"]
    context["tokens_are_empty"] = balance <= 0
    # Read through settings_service so an unsaved default counts: APP_SETTINGS
    # only holds rows an administrator has actually stored.
    context["referral_enabled_flag"] = bool(settings_service.get("referral_enabled", True))
    context["maintenance_note"] = (settings_service.get("maintenance_note", "") or "").strip()

    # Drives the "Help & support" badge for students and the console badge for
    # administrators — the same idea, different scope.
    try:
        tickets = SupportTicket.objects.filter(status__in=OPEN_STATUSES)
        if user.is_admin_user:
            context["open_ticket_count"] = tickets.count()
        else:
            context["open_ticket_count"] = tickets.filter(user=user).count()
    except Exception:  # pragma: no cover — never break a page over a badge
        context["open_ticket_count"] = 0

    # Administrators carry a health badge into every console page. The count is
    # cached, so this costs nothing on a normal request.
    if user.is_admin_user:
        from AI.services import health_service, token_alert_service

        context["health_alert_count"] = health_service.alert_count()
        # One indexed count, so the badge costs the same on every page rather
        # than growing with the number of wallets.
        try:
            context["low_token_count"] = token_alert_service.at_risk_count()
        except Exception:  # pragma: no cover — never break a page over a badge
            context["low_token_count"] = 0
        # Paused/degraded models and the site-wide pause: one tiny query.
        try:
            from AI.services import model_status_service

            context["model_status_alert"] = model_status_service.attention()
        except Exception:  # pragma: no cover — never break a page over a badge
            context["model_status_alert"] = None

        # Which console areas this administrator's staff role reaches — for the
        # navigation, the bell and every button that leads somewhere.
        context.update(staff_access(request))

    return context


def staff_access(request) -> dict:
    """The signed-in administrator's staff role, for templates.

    ``console_can.finance`` and friends are what the navigation, the bell, the
    dashboard and shared pages test before drawing a link or a button, so a
    role is only shown what it can open. They are a courtesy — the rule itself
    is enforced by ``AI.middleware.StaffAreaMiddleware``. No queries: the role
    is a field on the user already loaded.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_admin_user:
        return {}
    from AI.models import StaffRole
    from AI.services import staff_permissions

    role = staff_permissions.role_of(user)
    areas = staff_permissions.areas_for(user)
    return {
        "console_role": role,
        "console_role_label": staff_permissions.role_label(role),
        "console_areas": areas,
        "console_can": {area.key: area.key in areas for area in staff_permissions.AREAS},
        "console_is_owner": role == StaffRole.OWNER,
        # For links that arrive as data: {% if console_reach|get_item:url %}.
        "console_reach": staff_permissions.Reach(user),
    }


def console_scope(request) -> dict:
    """The selected AI model, for any console page that is model-specific.

    Resolved here rather than in each view so that every page reads the same
    session value and renders the same selector — the alternative was thirty
    views each deciding for themselves, which is how a selector ends up
    meaning something different on two pages that sit next to each other.

    Views still call ``model_scope.resolve`` themselves to *filter*; this only
    supplies what the template needs to draw the control.
    """
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_admin_user:
        return {}
    if not request.path.startswith("/admin/"):
        return {}

    from AI.services import model_scope as scope_service

    try:
        scope = scope_service.resolve(request)
    except Exception:  # pragma: no cover — never break a page over a control
        return {}
    return {
        "model_scope": scope,
        "model_scope_options": scope.options(),
        "show_model_scope": scope_service.is_model_aware(request),
    }
