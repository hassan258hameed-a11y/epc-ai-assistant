"""Staff roles: which parts of the admin console each administrator may use.

This module is the single source of truth. The middleware
(``AI.middleware.StaffAreaMiddleware``) asks it before every console view runs,
the navigation asks it which links to draw, and the Staff page prints its
matrix — so what a role can open, what it is shown and what the owner reads on
the Staff page can never disagree.

How it fits together
--------------------
* The console is split into **areas** (``AREAS``). Every console route belongs
  to exactly one (``ROUTE_AREAS``); a test fails if a route is added without
  one, so a new page cannot slip in unprotected. A route that is somehow not
  listed is treated as owner-only at runtime.
* Each **role** reaches a set of areas (``ROLE_AREAS``).
* A few routes do different jobs depending on what is posted — the bulk bar on
  the student list gives tokens *or* sends a message — so those are refined by
  the posted ``action`` (``ACTION_AREAS``) or a URL argument (``KWARG_AREAS``).
* Two rules sit on top of the matrix, because the student pages can reach
  staff accounts too:
    - only an owner may open or act on an **administrator's** account through
      the student pages (``USER_TARGET_ROUTES``) — otherwise Support could
      reset an owner's password and sign in as them;
    - only an owner may make somebody an administrator through the student
      edit / create forms (``ROLE_FIELD_ROUTES``).

Why the roles look the way they do
----------------------------------
* **Owner** — everything, and the only role that manages staff, platform
  settings and the audit log.
* **Support** — talks to students: reads profiles, keeps notes and tags, sends
  messages, fixes account details, resets passwords, signs people out, answers
  tickets and reads conversations to do so. It does not touch money or tokens
  (a help desk that can mint tokens is a leak) and does not create accounts or
  run bulk token actions.
* **Finance** — money: payments, gateways, subscriptions, plans, promo codes,
  revenue, cost & margin, referral rewards, and granting or extending a plan.
  It reads student profiles to reconcile a payment but cannot edit or message
  students.
* **Content** — what students read: announcements (and the Telegram channel
  posts they go out as), the prompt library and answer-quality feedback. It
  has no access to student records.
* **Operations** — keeps the service running: model status, health, the
  e-mail log, data exports and Telegram setup — plus the token supply (models,
  pools, distribution, usage limits). It reads student profiles because the
  "Manage tokens" dialog lives on the student's page.

An administrator with **no role** (``staff_role`` blank — the migration made
every existing administrator an owner, so this only happens to an account
promoted later through the old student form) gets the areas everyone has and
nothing else until an owner picks a role on the Staff page. Failing closed is
deliberate: a forgotten role should cost a message to an owner, not hand out
the whole console. A superuser is always an owner.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from django.urls import Resolver404, resolve

from AI.models import AccountStatus, Role, StaffRole, User

#: The label shown for an administrator whose role has not been chosen yet.
UNASSIGNED = ""
UNASSIGNED_LABEL = "No role yet"


@dataclass(frozen=True)
class Area:
    key: str
    label: str
    covers: str


#: Every area of the console, in the order the Staff page lists them.
AREAS: tuple[Area, ...] = (
    Area("dashboard", "Dashboard",
         "The overview. Figures and links from areas a role cannot open are left off."),
    Area("search", "Search",
         "The ⌘K search. Results only list records the role can open."),
    Area("personal", "Own sign-in security",
         "Your own sign-in codes and Telegram link, on the Telegram page."),
    Area("students", "Students",
         "The student list and profiles, Running low and Expiring access — to read."),
    Area("student_care", "Student care",
         "Notes, tags and messages; editing details, password resets, signing out, "
         "enabling and disabling; low-balance warnings; bulk messages and tags."),
    Area("accounts", "Account creation",
         "Creating accounts, CSV import, exporting selected students."),
    Area("support", "Support",
         "Support tickets, saved replies and the conversations browser."),
    Area("finance", "Finance",
         "Payments, gateways, subscriptions, plans, promo codes, revenue and growth, "
         "cost & margin, referrals and campaigns, granting and extending plans."),
    Area("content", "Content",
         "Announcements and Telegram channel posts, the prompt library, answer quality."),
    Area("tokens", "Tokens & models",
         "AI tokens and distribution, token management and the model catalogue, "
         "usage limits, usage analytics."),
    Area("operations", "Operations",
         "Model status, system health, the e-mail log, data exports, Telegram setup."),
    Area("staff", "Staff & audit log",
         "This Staff page — roles, new staff accounts, disabling staff — and the audit log."),
    Area("settings", "Settings",
         "Platform settings."),
)
AREA_BY_KEY = {area.key: area for area in AREAS}
ALL_AREAS = frozenset(AREA_BY_KEY)

#: What every administrator can open, whatever their role.
EVERYONE = frozenset({"dashboard", "search", "personal"})

#: The matrix. Each role's reasoning is in the module docstring.
ROLE_AREAS: dict[str, frozenset[str]] = {
    StaffRole.OWNER: ALL_AREAS,
    StaffRole.SUPPORT: EVERYONE | {"students", "student_care", "support"},
    StaffRole.FINANCE: EVERYONE | {"students", "finance"},
    StaffRole.CONTENT: EVERYONE | {"content"},
    StaffRole.OPERATIONS: EVERYONE | {"students", "tokens", "operations"},
}
UNASSIGNED_AREAS = EVERYONE

#: The roles in the order the Staff page shows them.
ROLES: tuple[str, ...] = (
    StaffRole.OWNER, StaffRole.SUPPORT, StaffRole.FINANCE, StaffRole.CONTENT, StaffRole.OPERATIONS,
)

ROLE_SUMMARIES = {
    StaffRole.OWNER: "Everything, including staff, settings and the audit log.",
    StaffRole.SUPPORT: "Students, notes and messages, tickets and conversations.",
    StaffRole.FINANCE: "Payments, plans, promos, subscriptions and revenue; reads student profiles.",
    StaffRole.CONTENT: "Announcements, the prompt library and answer quality.",
    StaffRole.OPERATIONS: "Models, tokens, health, e-mail, exports and Telegram setup.",
}

# ---------------------------------------------------------------------------
# Every console route, by area.
# ---------------------------------------------------------------------------
ROUTE_AREAS: dict[str, str] = {
    # Everyone
    "console:dashboard": "dashboard",
    "console:search": "search",
    "console:telegram": "personal",          # the page; its setup buttons are refined below
    # Send a message: the page shows each role only the destinations it may
    # use; what is posted is refined by its target below.
    "console:telegram_send": "personal",

    # Students — reading
    "console:users": "students",
    "console:user_detail": "students",
    "console:low_tokens": "students",
    "console:expiring_tokens": "students",

    # Student care — changing a student, one at a time
    "console:users_bulk": "student_care",     # refined by action below
    "console:student_note_add": "student_care",
    "console:student_note_action": "student_care",
    "console:student_tags": "student_care",
    "console:student_message": "student_care",
    "console:student_sign_out": "student_care",
    "console:low_token_warn": "student_care",
    "console:expiring_action": "student_care",  # "extend" is refined to finance
    "console:user_edit": "student_care",
    "console:user_toggle": "student_care",
    "console:user_reset_password": "student_care",

    # Account creation
    "console:accounts": "accounts",
    "console:accounts_sample": "accounts",
    "console:accounts_check_username": "accounts",
    "console:accounts_import_preview": "accounts",
    "console:accounts_import_confirm": "accounts",
    "console:accounts_import_clear": "accounts",
    "console:accounts_import_result": "accounts",

    # Support
    "console:tickets": "support",
    "console:tickets_bulk": "support",
    "console:ticket_detail": "support",
    "console:ticket_reply": "support",
    "console:ticket_update": "support",
    "console:canned_replies": "support",
    "console:canned_create": "support",
    "console:canned_save": "support",
    "console:canned_delete": "support",
    "console:conversations": "support",
    "console:conversation_detail": "support",
    "console:conversation_delete": "support",
    "console:conversation_unshare": "support",

    # Finance
    "console:student_grant_plan": "finance",
    "console:payments": "finance",
    "console:payment_detail": "finance",
    "console:payment_action": "finance",
    "console:gateways": "finance",
    "console:gateway_test": "finance",
    "console:subscriptions": "finance",
    "console:subscription_cancel": "finance",
    "console:subscription_extend": "finance",
    "console:plans": "finance",
    "console:plan_create": "finance",
    "console:plan_edit": "finance",
    "console:plan_toggle": "finance",
    "console:promos": "finance",
    "console:promo_create": "finance",
    "console:promo_detail": "finance",
    "console:promo_edit": "finance",
    "console:promo_toggle": "finance",
    "console:reports": "finance",
    "console:reports_export": "finance",
    "console:model_costs": "finance",
    "console:model_costs_export": "finance",
    "console:referrals": "finance",
    "console:referral_campaigns": "finance",
    "console:referral_campaign_create": "finance",
    "console:referral_campaign_detail": "finance",
    "console:referral_campaign_edit": "finance",
    "console:referral_campaign_toggle": "finance",

    # Content
    "console:notifications": "content",
    "console:announcement_action": "content",
    "console:telegram_message_delete": "content",   # taking back a channel or group post
    "console:prompt_library": "content",
    "console:prompt_create": "content",
    "console:prompt_save": "content",
    "console:prompt_toggle": "content",
    "console:prompt_delete": "content",
    "console:quality": "content",
    "console:feedback_review": "content",

    # Tokens & models
    "console:ai_tokens": "tokens",
    "console:ai_distribute": "tokens",
    "console:ai_distribute_preview": "tokens",
    "console:ai_distribute_apply": "tokens",
    "console:ai_distribute_bulk": "tokens",
    "console:ai_pool_topup": "tokens",
    "console:ai_adjust": "tokens",
    "console:ai_student": "tokens",
    "console:ai_toggle_access": "tokens",
    "console:ai_recalculate": "tokens",
    "console:ai_provider_toggle": "tokens",
    "console:tokens": "tokens",
    "console:tokens_assign": "tokens",
    "console:tokens_bulk_assign": "tokens",
    "console:tokens_pool_topup": "tokens",
    "console:tokens_student": "tokens",
    "console:token_history": "tokens",
    "console:model_create": "tokens",
    "console:model_save": "tokens",
    "console:model_toggle": "tokens",
    "console:model_default": "tokens",
    "console:model_allocate": "tokens",
    "console:models_recalculate": "tokens",
    "console:usage_limits": "tokens",
    "console:usage": "tokens",

    # Operations
    "console:model_status": "operations",
    "console:model_status_set": "operations",
    "console:model_status_pause": "operations",
    "console:model_status_digest": "operations",
    "console:health": "operations",
    "console:health_data": "operations",
    "console:emails": "operations",
    "console:email_retry": "operations",
    "console:exports": "operations",
    "console:export_download": "operations",  # the audit dataset is refined to staff
    "console:telegram_action": "operations",  # personal buttons are refined below

    # Owner only
    "console:staff": "staff",
    "console:staff_create": "staff",
    "console:staff_role": "staff",
    "console:staff_toggle": "staff",
    "console:audit_logs": "staff",
    "console:settings": "settings",
}

#: Routes whose area depends on the posted ``action``: {route: (field, {value: area})}.
#: Anything not listed keeps the route's own area.
ACTION_AREAS: dict[str, tuple[str, dict[str, str]]] = {
    # The student list's bulk bar: messages, tags and enabling are student
    # care; giving tokens is a token action; a CSV of personal data leaving
    # the platform is account administration.
    "console:users_bulk": ("action", {"tokens": "tokens", "export": "accounts"}),
    # A reminder is student care; moving a plan's end date is a money decision.
    "console:expiring_action": ("action", {"extend": "finance"}),
    # Setting up the bot is operations — the webhook, the command menu, test
    # posts to the channel and the group, the status check and which
    # automatic alerts reach Telegram; linking your own Telegram and turning
    # on your own sign-in codes is something every administrator must be able
    # to do for themselves.
    "console:telegram_action": ("action", {
        "register_webhook": "operations", "set_commands": "operations",
        "test_channel": "operations", "test_group": "operations",
        "check": "operations", "alert_settings": "operations",
        "login_otp": "personal", "connect_me": "personal",
        "unlink_me": "personal", "test_me": "personal",
    }),
    # Posting where everyone reads is Content's; writing to students is
    # Student care's. A missing target falls back to the page's own area and
    # the view refuses it.
    "console:telegram_send": ("target", {
        "channel": "content", "group": "content",
        "student": "student_care", "students": "student_care",
    }),
}

#: Routes whose area depends on a URL argument: {route: (kwarg, {value: area})}.
KWARG_AREAS: dict[str, tuple[str, dict[str, str]]] = {
    # Who did what in the console is the owner's to export.
    "console:export_download": ("key", {"audit": "staff"}),
}

#: Student-page routes whose ``pk`` is a user. Only owners may point them at
#: an administrator's account.
USER_TARGET_ROUTES = frozenset({
    "console:user_detail", "console:user_edit", "console:user_toggle", "console:user_reset_password",
    "console:student_note_add", "console:student_note_action", "console:student_tags",
    "console:student_message", "console:student_grant_plan", "console:student_sign_out",
    "console:ai_student", "console:ai_toggle_access", "console:ai_recalculate", "console:tokens_student",
})

#: Forms that carry a ``role`` field. Only owners may post ``role=admin`` to them.
ROLE_FIELD_ROUTES = frozenset({"console:user_edit", "console:accounts"})

SEARCH_ROUTE = "console:search"

DENIED_AREA = "You don't have access to this area — ask an owner."
DENIED_STAFF_TARGET = "Staff accounts are managed by owners, on the Staff page."
DENIED_ESCALATION = "Only an owner can make someone an administrator — ask an owner to use the Staff page."


@dataclass(frozen=True)
class Decision:
    allowed: bool
    area: str = ""
    reason: str = ""      # "", "area", "staff_target", "role_escalation"
    message: str = ""


ALLOW = Decision(True)


# ---------------------------------------------------------------------------
# Who is this?
# ---------------------------------------------------------------------------
def role_of(user) -> str | None:
    """The staff role that governs ``user``.

    ``None`` for anybody who is not an administrator; ``UNASSIGNED`` ("") for
    an administrator whose role has not been chosen; otherwise a ``StaffRole``
    value. A superuser is always an owner.
    """
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_admin_user", False):
        return None
    if user.is_superuser:
        return StaffRole.OWNER
    role = user.staff_role or UNASSIGNED
    return role if role in ROLE_AREAS else UNASSIGNED


def role_label(role: str | None) -> str:
    if role is None:
        return ""
    if role == UNASSIGNED:
        return UNASSIGNED_LABEL
    return StaffRole(role).label


def areas_for(user) -> frozenset[str]:
    role = role_of(user)
    if role is None:
        return frozenset()
    if role == UNASSIGNED:
        return UNASSIGNED_AREAS
    return ROLE_AREAS[role]


def can(user, area: str) -> bool:
    return area in areas_for(user)


def is_owner(user) -> bool:
    return role_of(user) == StaffRole.OWNER


def area_label(area: str) -> str:
    found = AREA_BY_KEY.get(area)
    return found.label if found else area


def is_staff_account(user) -> bool:
    return bool(user is not None and (user.role == Role.ADMIN or user.is_superuser))


# ---------------------------------------------------------------------------
# May this request go ahead?
# ---------------------------------------------------------------------------
def reads_form(view_name: str) -> bool:
    """Does deciding ``view_name`` need the posted form?"""
    return view_name in ACTION_AREAS or view_name in ROLE_FIELD_ROUTES


def area_of(view_name: str, *, post=None, kwargs=None) -> str:
    """The area a request to ``view_name`` needs. Unmapped routes are owner-only."""
    area = ROUTE_AREAS.get(view_name, "staff")
    if post is not None and view_name in ACTION_AREAS:
        field, mapping = ACTION_AREAS[view_name]
        area = mapping.get((post.get(field) or "").strip(), area)
    if kwargs and view_name in KWARG_AREAS:
        name, mapping = KWARG_AREAS[view_name]
        area = mapping.get(str(kwargs.get(name, "")), area)
    return area


def _targets_staff(kwargs) -> bool:
    pk = (kwargs or {}).get("pk")
    if pk is None:
        return False
    row = User.objects.filter(pk=pk).values_list("role", "is_superuser").first()
    return bool(row and (row[0] == Role.ADMIN or row[1]))


def check(user, view_name: str, *, post=None, kwargs=None) -> Decision:
    """Decide one console request. ``post`` is the submitted form, if any."""
    area = area_of(view_name, post=post, kwargs=kwargs)
    role = role_of(user)
    if role == StaffRole.OWNER:
        return ALLOW
    if area not in areas_for(user):
        return Decision(False, area, "area", DENIED_AREA)
    if view_name in USER_TARGET_ROUTES and _targets_staff(kwargs):
        return Decision(False, "staff", "staff_target", DENIED_STAFF_TARGET)
    if post is not None and view_name in ROLE_FIELD_ROUTES and (post.get("role") or "") == Role.ADMIN:
        return Decision(False, "staff", "role_escalation", DENIED_ESCALATION)
    return ALLOW


def can_url(user, url: str) -> bool:
    """Could ``user`` open ``url`` (a GET)? Anything outside the console: yes."""
    try:
        match = resolve(urlsplit(url).path)
    except Resolver404:
        return True
    if match.namespace != "console":
        return True
    return check(user, match.view_name, kwargs=match.kwargs).allowed


class Reach:
    """``{% if console_reach|get_item:some_url %}`` — can this user open that link?

    For links a page receives as data (a health check's "fix it here", a
    notification's target) rather than writes with ``{% url %}``, where the
    area is not known when the template is written. Answers are cached for
    the request; owners are answered without looking.
    """

    def __init__(self, user):
        self.user = user
        self.owner = is_owner(user)
        self._seen: dict[str, bool] = {}

    def get(self, url, default=None) -> bool:
        if self.owner:
            return True
        url = str(url or "")
        if url not in self._seen:
            self._seen[url] = can_url(self.user, url)
        return self._seen[url]

    __getitem__ = get


def filter_search_payload(user, payload: dict) -> dict:
    """Drop the ⌘K search results ``user`` could not open, and emptied groups."""
    if is_owner(user) or not isinstance(payload, dict):
        return payload
    groups = []
    for group in payload.get("groups") or []:
        items = [item for item in group.get("items") or [] if can_url(user, item.get("url") or "")]
        if items:
            groups.append({**group, "items": items})
    return {**payload, "groups": groups}


# ---------------------------------------------------------------------------
# For the Staff page
# ---------------------------------------------------------------------------
def staff_accounts():
    """Every administrator account, students never included."""
    from django.db.models import Q

    return User.objects.filter(Q(role=Role.ADMIN) | Q(is_superuser=True))


def active_owners():
    """Staff who are owners and can currently sign in."""
    from django.db.models import Q

    return staff_accounts().filter(
        Q(is_superuser=True) | Q(staff_role=StaffRole.OWNER),
        status=AccountStatus.ACTIVE, is_active=True,
    )


def matrix() -> list[dict]:
    """One row per area: which roles reach it, in ``ROLES`` order."""
    return [
        {"area": area, "cells": [area.key in ROLE_AREAS[role] for role in ROLES]}
        for area in AREAS
    ]


def role_choices() -> list[tuple[str, str]]:
    return [(role, StaffRole(role).label) for role in ROLES]
