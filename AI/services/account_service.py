from __future__ import annotations
"""Account provisioning: single accounts, bulk import and enable/disable."""

from django.conf import settings

import csv
import io
import logging

from django.contrib.auth.password_validation import ValidationError, validate_password
from django.db import transaction
from django.utils import timezone

from AI.models import AccountSource, AccountStatus, AuditAction, Role, TokenAccount, User, generate_password
from datetime import timedelta

from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

from AI.models.billing import Plan, Subscription
from AI.models.comms import EmailLog, EmailStatus, EmailType
from AI.services import (
    audit_service,
    model_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.utils.uploads import InvalidUpload, validate_spreadsheet

logger = logging.getLogger("AI.accounts")

IMPORT_COLUMNS = ["username", "full_name", "email", "password", "source", "tokens", "phone", "notes"]
SOURCE_VALUES = {choice[0] for choice in AccountSource.choices}


@transaction.atomic
def create_account(
    *,
    username: str,
    password: str = "",
    full_name: str = "",
    email: str = "",
    source: str = AccountSource.ADMIN,
    phone: str = "",
    notes: str = "",
    tokens: int = 0,
    role: str = Role.STUDENT,
    status: str = AccountStatus.ACTIVE,
    is_verified: bool = True,
    created_by: User | None = None,
    request=None,
    ai_model: AIModel | None = None,
    plan: Plan | None = None,
    force_password_change: bool = False,
    send_credentials: bool = False,
) -> tuple[User, str]:
    """Create one account. Returns ``(user, plaintext_password)``.

    The password is returned so an admin can hand it over once; only the hash is
    stored.

    ``ai_model`` names the pool an opening balance is drawn from; without it
    the default model is used, as before. ``plan`` puts the account straight
    onto a plan with no payment behind it. ``force_password_change`` makes the
    student choose their own password at first sign-in even when the admin set
    one. ``send_credentials`` e-mails the login - only when there is an address
    to send it to, and always recorded in the e-mail log.
    """
    username = (username or "").strip()
    generated = ""
    if not password:
        password = generate_password()
        generated = password

    user = User(
        username=username,
        email=(email or "").strip(),
        full_name=(full_name or "").strip(),
        role=role,
        status=status,
        source=source if source in SOURCE_VALUES else AccountSource.ADMIN,
        phone=(phone or "").strip()[:32],
        notes=notes or "",
        is_verified=is_verified,
        # A generated password always has to be changed; an admin can also
        # require it for one they typed themselves.
        must_change_password=bool(generated) or bool(force_password_change),
        created_by=created_by,
    )
    user.set_password(password)
    user.save()
    TokenAccount.objects.get_or_create(user=user)

    # Tokens belong to a model, so an opening allocation has to name one. Both
    # paths land in the default model's wallet; every other model starts at zero,
    # which is the intended state for a new account.
    landing = ai_model or model_service.default_model()
    if landing is not None:
        if tokens and int(tokens) > 0:
            wallet_service.credit(
                user,
                landing,
                int(tokens),
                reason=f"Initial {landing.name} allocation at account creation",
                performed_by=created_by,
            )
        else:
            # No opening amount: the student gets the ordinary free trial, and
            # that is always in the default model, whichever model was picked.
            trial_model = model_service.default_model()
            if (
                role == Role.STUDENT
                and trial_model is not None
                and trial_model.auto_grant
                and settings_service.get("free_trial_enabled", True)
            ):
                amount = int(settings_service.get("free_trial_tokens", 0))
                if amount > 0:
                    wallet_service.grant_free_trial(user, trial_model, amount, performed_by=created_by)

    if plan is not None:
        grant_plan(user, plan, performed_by=created_by)

    audit_service.record(
        AuditAction.USER_CREATE,
        actor=created_by,
        request=request,
        target=user,
        description=f"Created {role} account from {user.get_source_display()}",
        new_value=(
            f"username={user.username}, tokens={tokens or 0}"
            + (f", model={landing.key}" if landing is not None and tokens else "")
            + (f", plan={plan.slug or plan.name}" if plan is not None else "")
        ),
    )

    plaintext = generated or password
    if send_credentials:
        email_credentials(user, plaintext)
    return user, plaintext


def grant_plan(user: User, plan: Plan, *, performed_by: User | None = None, reason: str = "") -> Subscription:
    """Put an account on a plan without a payment.

    The same shape a paid subscription has - stacked on the current one if
    there is one, the plan's tokens credited into the plan's own model - so a
    plan granted by hand and a plan that was paid for look identical
    everywhere downstream. ``payment`` stays empty, which is the one honest
    difference and the one the finance pages need to see.

    Every model the plan includes is credited — a bundle's extra models as
    well as its main one, and the catalogue default when the plan names no
    model — exactly as ``payment_service.settle`` credits a paid order, from
    the same ``plan.grant_rows``. What was credited is left on the returned
    subscription as ``credited``: ``[(model, tokens)]``, so a caller describes
    what happened rather than what the plan's headline figure says.
    """
    with transaction.atomic():
        now = timezone.now()
        current = (
            Subscription.objects.filter(
                user=user, status=Subscription.Status.ACTIVE, ends_at__gt=now
            )
            .order_by("-ends_at")
            .first()
        )
        starts_at = current.ends_at if current else now
        rows = [row for row in plan.grant_rows if int(row["tokens"] or 0) > 0]
        primary = next((row for row in rows if row["is_primary"]), None)
        subscription = Subscription.objects.create(
            user=user,
            plan=plan,
            payment=None,
            tokens_granted=int(primary["tokens"]) if primary else 0,
            starts_at=starts_at,
            ends_at=starts_at + timedelta(days=plan.duration_days or 30),
        )
        credited = []
        for row in rows:
            wallet_service.credit(
                user,
                row["model"],
                int(row["tokens"]),
                reason=reason or f"{plan.name} plan granted at account creation",
                performed_by=performed_by,
                plan=plan,
            )
            credited.append((row["model"], int(row["tokens"])))
    subscription.credited = credited
    return subscription


def describe_credit(credited) -> str:
    """``"50,000 GPT-5 Mini and 20,000 Claude tokens"`` — or "" for nothing."""
    parts = [f"{tokens:,} {model.name}" for model, tokens in credited or [] if tokens > 0]
    if not parts:
        return ""
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return f"{joined} tokens"


def email_credentials(user: User, password: str) -> EmailLog | None:
    """Send a new account its login, and record that it was sent.

    Same mechanics as every other message this platform sends: Django's mail
    backend, a text part and an HTML part, and an EmailLog row whatever the
    outcome - because "no e-mail arrived" and "we never tried" are different
    problems. A failure is logged, not raised: the account exists either way
    and the administrator still has the password on screen to hand over.
    """
    if not (user.email or "").strip():
        return None

    brand = settings.BRAND
    brand_name = (brand.get("name") if isinstance(brand, dict) else getattr(brand, "name", "")) or "AI Assistant"
    site = settings.SITE_URL.rstrip("/")
    context = {
        "user": user,
        "brand": brand,
        "brand_name": brand_name,
        "username": user.username,
        "password": password,
        "login_url": f"{site}/login/",
        "site_url": site,
    }
    subject = f"Your {brand_name} login"

    log = EmailLog(
        user=user,
        recipient=user.email,
        email_type=EmailType.CREDENTIALS,
        subject=subject[:190],
        trigger="account created by an administrator",
    )
    try:
        message = EmailMultiAlternatives(
            subject,
            render_to_string("emails/credentials.txt", context),
            settings.DEFAULT_FROM_EMAIL,
            [user.email],
        )
        message.attach_alternative(render_to_string("emails/credentials.html", context), "text/html")
        message.send(fail_silently=False)
        log.status = EmailStatus.SENT
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        logger.warning("credentials e-mail to %s failed: %s", user.email, exc)
        log.status = EmailStatus.FAILED
        log.error = str(exc)[:255]
    log.save()
    return log


def set_enabled(user: User, enabled: bool, *, actor: User | None = None, reason: str = "", request=None) -> User:
    """Enable or disable an account.

    Disabling also clears the active session so the user is signed out
    immediately and cannot spend tokens.
    """
    user.status = AccountStatus.ACTIVE if enabled else AccountStatus.DISABLED
    user.is_active = enabled
    user.disabled_at = None if enabled else timezone.now()
    user.disabled_reason = "" if enabled else (reason or "")[:255]
    if not enabled:
        user.active_session_key = ""
    user.save(
        update_fields=["status", "is_active", "disabled_at", "disabled_reason", "active_session_key"]
    )

    if not enabled:
        _drop_sessions(user)

    audit_service.record(
        AuditAction.USER_ENABLE if enabled else AuditAction.USER_DISABLE,
        actor=actor,
        request=request,
        target=user,
        description=reason or ("Account enabled" if enabled else "Account disabled"),
        old_value="active" if not enabled else "disabled",
        new_value="active" if enabled else "disabled",
    )
    return user


def sign_out_everywhere(user: User) -> None:
    """End every session the account has, without disabling it."""
    User.objects.filter(pk=user.pk).update(active_session_key="")
    _drop_sessions(user)


def _drop_sessions(user: User) -> None:
    """Delete any live session rows belonging to a user."""
    from django.contrib.sessions.models import Session

    now = timezone.now()
    for session in Session.objects.filter(expire_date__gte=now).iterator():
        try:
            if str(session.get_decoded().get("_auth_user_id")) == str(user.pk):
                session.delete()
        except Exception:  # pragma: no cover — corrupt session payloads
            continue


def reset_password(user: User, *, actor: User | None = None, new_password: str = "", request=None) -> str:
    password = new_password or generate_password()
    user.set_password(password)
    user.must_change_password = not new_password
    user.active_session_key = ""
    user.save(update_fields=["password", "must_change_password", "active_session_key"])
    _drop_sessions(user)

    audit_service.record(
        AuditAction.PASSWORD_RESET,
        actor=actor,
        request=request,
        target=user,
        description="Password reset by administrator",
    )
    return password


# ---------------------------------------------------------------------------
# Bulk import
# ---------------------------------------------------------------------------
class ImportRow:
    def __init__(self, index: int, data: dict):
        self.index = index
        self.data = data
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "data": self.data,
            "errors": self.errors,
            "warnings": self.warnings,
            "valid": self.is_valid,
        }


def _read_rows(raw: bytes, kind: str) -> list[dict]:
    if kind == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(cell or "").strip().lower().replace(" ", "_") for cell in rows[0]]
        out = []
        for row in rows[1:]:
            if all(cell in (None, "") for cell in row):
                continue
            out.append({headers[i]: ("" if v is None else str(v).strip()) for i, v in enumerate(row) if i < len(headers)})
        return out

    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover
        raise InvalidUpload("The file encoding is not supported.")

    reader = csv.DictReader(io.StringIO(text))
    normalised = []
    for row in reader:
        cleaned = {
            (key or "").strip().lower().replace(" ", "_"): (str(value).strip() if value is not None else "")
            for key, value in row.items()
        }
        if any(cleaned.values()):
            normalised.append(cleaned)
    return normalised


def parse_import(uploaded) -> list[ImportRow]:
    """Validate an upload without writing anything — powers the preview step."""
    raw, kind = validate_spreadsheet(uploaded)
    records = _read_rows(raw, kind)
    if not records:
        raise InvalidUpload("The file has no data rows.")
    if len(records) > 1000:
        raise InvalidUpload("Please import at most 1,000 accounts at a time.")

    existing_usernames = set(
        User.objects.filter(username__in=[r.get("username", "") for r in records]).values_list("username", flat=True)
    )
    seen_in_file: set[str] = set()
    rows: list[ImportRow] = []

    for index, record in enumerate(records, start=2):  # row 1 is the header
        row = ImportRow(index, record)
        username = (record.get("username") or "").strip()
        email = (record.get("email") or "").strip()

        if not username:
            row.errors.append("Username is required.")
        elif len(username) > 150:
            row.errors.append("Username is too long.")
        elif not all(c.isalnum() or c in "._-@+" for c in username):
            row.errors.append("Username contains invalid characters.")
        elif username in existing_usernames:
            row.errors.append("An account with this username already exists.")
        elif username in seen_in_file:
            row.errors.append("Duplicate username inside this file.")
        else:
            seen_in_file.add(username)

        if email and "@" not in email:
            row.errors.append("E-mail address is not valid.")

        password = (record.get("password") or "").strip()
        if password:
            try:
                validate_password(password)
            except ValidationError as exc:
                row.errors.append(f"Weak password: {exc.messages[0]}")
        else:
            row.warnings.append("A secure password will be generated.")

        source = (record.get("source") or "").strip().lower()
        if source and source not in SOURCE_VALUES:
            row.warnings.append(f"Unknown source '{source}' — will use 'Created by admin'.")

        tokens = (record.get("tokens") or "").strip()
        if tokens:
            try:
                if int(float(tokens)) < 0:
                    row.errors.append("Token amount cannot be negative.")
            except ValueError:
                row.errors.append("Token amount must be a number.")

        rows.append(row)

    return rows


def run_import(rows: list[ImportRow], *, actor: User, request=None) -> dict:
    """Create every valid row. Invalid rows are skipped, never half-created."""
    created: list[dict] = []
    failed = 0

    for row in rows:
        if not row.is_valid:
            failed += 1
            continue
        data = row.data
        try:
            tokens_raw = (data.get("tokens") or "").strip()
            tokens = int(float(tokens_raw)) if tokens_raw else 0
            user, password = create_account(
                username=data.get("username", ""),
                password=(data.get("password") or "").strip(),
                full_name=data.get("full_name") or data.get("name") or "",
                email=data.get("email", ""),
                source=(data.get("source") or "").strip().lower() or AccountSource.IMPORT,
                phone=data.get("phone", ""),
                notes=data.get("notes", ""),
                tokens=tokens,
                created_by=actor,
                request=None,  # a single bulk audit entry is written below
            )
            created.append({"username": user.username, "password": password, "full_name": user.full_name})
        except Exception as exc:  # pragma: no cover
            logger.exception("Bulk import row failed")
            row.errors.append(str(exc))
            failed += 1

    audit_service.record(
        AuditAction.BULK_IMPORT,
        actor=actor,
        request=request,
        description=f"Imported {len(created)} account(s), {failed} skipped",
        new_value=", ".join(item["username"] for item in created[:50]),
    )
    return {"created": created, "created_count": len(created), "failed": failed}


def sample_csv() -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(IMPORT_COLUMNS)
    writer.writerow(["sokha.ny", "Sokha Ny", "sokha@example.com", "", "telegram", "20000", "", "From Telegram group"])
    writer.writerow(["dara.kim", "Dara Kim", "dara@example.com", "", "tiktok", "50000", "", ""])
    return buffer.getvalue()
