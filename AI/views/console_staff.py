"""The Staff page: who runs the console, and what each of them may do.

Owners only. It lists every administrator — never a student — and lets an
owner change a role, create a staff account and disable or re-enable one.
The rules that keep the console from locking itself out live here:

* nobody changes their own account's status from this page;
* the last owner who can sign in stays an owner;
* a superuser is always an owner, so their role is not offered as a choice.

The area check itself is ``StaffAreaMiddleware``; each view repeats it so the
page stays owner-only even if the middleware were ever taken out of settings.
Every change is audit-logged.
"""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from AI.models import (
    AccountSource,
    AccountStatus,
    AuditAction,
    Payment,
    PaymentStatus,
    Role,
    StaffRole,
    User,
    generate_password,
)
from AI.services import account_service, audit_service, staff_permissions
from AI.utils.decorators import admin_required

#: A new account's one-time password, kept only until the page has shown it.
HANDOVER_SESSION_KEY = "staff_handover"


class StaffCreateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Username")
    full_name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(max_length=254, label="E-mail",
                             help_text="Where sign-in codes and password resets go.")
    staff_role = forms.ChoiceField(choices=staff_permissions.role_choices(), label="Role",
                                   initial=StaffRole.SUPPORT)

    def clean_username(self):
        username = (self.cleaned_data["username"] or "").strip()
        if not all(char.isalnum() or char in "._-@+" for char in username):
            raise ValidationError("Use letters, numbers, dots, underscores, hyphens, @ or + only.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("That username is already taken.")
        return username

    def clean_full_name(self):
        return (self.cleaned_data["full_name"] or "").strip()

    def clean_email(self):
        email = (self.cleaned_data["email"] or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("Another account already uses this e-mail address.")
        return email


def _deny_unless_owner(request):
    if not staff_permissions.can(request.user, "staff"):
        raise Http404("Not found")


def _staff_member(pk: int) -> User:
    """A staff account by id; students are not found here."""
    member = get_object_or_404(User, pk=pk)
    if not staff_permissions.is_staff_account(member):
        raise Http404("Not a staff account")
    return member


def _is_last_owner(member: User) -> bool:
    owners = staff_permissions.active_owners()
    return owners.filter(pk=member.pk).exists() and owners.count() <= 1


@admin_required
def staff(request):
    _deny_unless_owner(request)
    members = list(
        staff_permissions.staff_accounts()
        .select_related("telegram", "created_by")
        .order_by("-is_active", "full_name", "username")
    )
    owners = staff_permissions.active_owners().count()
    counts = {role: 0 for role in staff_permissions.ROLES}
    unassigned = 0
    for member in members:
        member.effective_role = staff_permissions.role_of(member) or ""
        member.role_label = staff_permissions.role_label(member.effective_role)
        member.is_self = member.pk == request.user.pk
        member.is_last_owner = (
            member.effective_role == StaffRole.OWNER and member.is_active_account and owners <= 1
        )
        try:
            link = member.telegram
        except Exception:  # no TelegramAccount row yet
            link = None
        member.telegram_linked = bool(link and link.is_linked)
        if member.effective_role:
            counts[member.effective_role] += 1
        else:
            unassigned += 1

    return render(
        request,
        "console/staff.html",
        {
            "nav_active": "staff",
            "pending_payment_count": Payment.objects.filter(status=PaymentStatus.PENDING).count(),
            "members": members,
            "form": StaffCreateForm(),
            "roles": [
                {"value": role, "label": StaffRole(role).label,
                 "summary": staff_permissions.ROLE_SUMMARIES[role], "count": counts[role]}
                for role in staff_permissions.ROLES
            ],
            "role_choices": staff_permissions.role_choices(),
            "matrix": staff_permissions.matrix(),
            "unassigned": unassigned,
            "owner_count": owners,
            "handover": request.session.pop(HANDOVER_SESSION_KEY, None),
        },
    )


@admin_required
@require_POST
def staff_create(request):
    _deny_unless_owner(request)
    form = StaffCreateForm(request.POST)
    if not form.is_valid():
        for field, errors in form.errors.items():
            label = form.fields[field].label if field in form.fields else ""
            messages.error(request, f"{label}: {errors[0]}" if label else errors[0])
        return redirect("console:staff")

    data = form.cleaned_data
    role = data["staff_role"]
    try:
        with transaction.atomic():
            member, password = account_service.create_account(
                username=data["username"],
                password=generate_password(16),
                full_name=data["full_name"],
                email=data["email"],
                source=AccountSource.ADMIN,
                role=Role.ADMIN,
                status=AccountStatus.ACTIVE,
                created_by=request.user,
                request=request,
                force_password_change=True,
            )
            member.staff_role = role
            member.save(update_fields=["staff_role"])
    except IntegrityError:
        messages.error(request, "That username or e-mail was taken a moment ago. Try another.")
        return redirect("console:staff")

    audit_service.record(
        AuditAction.USER_UPDATE, actor=request.user, request=request, target=member,
        description=f"Created a staff account with the {StaffRole(role).label} role",
        new_value=f"staff_role={role}",
    )
    request.session[HANDOVER_SESSION_KEY] = {
        "name": member.display_name,
        "username": member.username,
        "email": member.email,
        "password": password,
        "role": StaffRole(role).label,
    }
    return redirect("console:staff")


@admin_required
@require_POST
def staff_role(request, pk: int):
    _deny_unless_owner(request)
    member = _staff_member(pk)
    new_role = (request.POST.get("staff_role") or "").strip()
    if new_role not in staff_permissions.ROLE_AREAS:
        messages.error(request, "Choose one of the roles.")
        return redirect("console:staff")

    old_role = StaffRole.OWNER if member.is_superuser else (member.staff_role or "")
    if new_role == old_role:
        messages.info(request, f"{member.display_name} is already {StaffRole(new_role).label}.")
        return redirect("console:staff")
    if member.is_superuser and new_role != StaffRole.OWNER:
        messages.error(request, f"{member.display_name} is a superuser, and a superuser is always an owner.")
        return redirect("console:staff")
    if old_role == StaffRole.OWNER and new_role != StaffRole.OWNER and _is_last_owner(member):
        who = "You are" if member.pk == request.user.pk else f"{member.display_name} is"
        messages.error(request, f"{who} the last owner. Make someone else an owner first, "
                                "so the console always has one.")
        return redirect("console:staff")

    member.staff_role = new_role
    member.save(update_fields=["staff_role"])
    audit_service.record(
        AuditAction.USER_UPDATE, actor=request.user, request=request, target=member,
        description=f"Changed staff role to {StaffRole(new_role).label}",
        old_value=f"staff_role={old_role or 'none'}", new_value=f"staff_role={new_role}",
    )
    if member.pk == request.user.pk:
        # Their own page is owner-only, so there is nowhere left here to land.
        messages.success(request, f"Your role is now {StaffRole(new_role).label}.")
        return redirect("console:dashboard")
    messages.success(request, f"{member.display_name} is now {StaffRole(new_role).label}.")
    return redirect("console:staff")


@admin_required
@require_POST
def staff_toggle(request, pk: int):
    _deny_unless_owner(request)
    member = _staff_member(pk)
    if member.pk == request.user.pk:
        messages.error(request, "You cannot disable your own account. Ask another owner.")
        return redirect("console:staff")

    enable = member.status != AccountStatus.ACTIVE or not member.is_active
    if not enable and _is_last_owner(member):
        messages.error(request, f"{member.display_name} is the last owner who can sign in, so they stay enabled.")
        return redirect("console:staff")

    account_service.set_enabled(
        member, enable, actor=request.user, request=request,
        reason="" if enable else ((request.POST.get("reason") or "").strip()[:255] or "Staff access removed"),
    )
    messages.success(request, f"{member.display_name} has been {'enabled' if enable else 'disabled'}.")
    return redirect("console:staff")
