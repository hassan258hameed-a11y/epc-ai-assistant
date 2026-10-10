"""CSV extracts of the operational record.

An administrator eventually needs the data *outside* the console — a finance
spreadsheet, a term report, a regulator's request, a backup before a migration.
This module is the one place that knows how to produce those files.

Design notes:

* **Streamed, not built in memory.** Every dataset is written row by row
  through :class:`django.http.StreamingHttpResponse`, so exporting a hundred
  thousand usage rows costs a constant amount of memory and starts downloading
  immediately.
* **Explicit column lists.** Nothing is serialised by reflection. A field only
  leaves the system because it is named here, which is what stops a future
  model change from quietly adding a password hash or a session key to a file
  someone e-mails around.
* **Every export is audited.** Bulk personal data leaving the platform is a
  consequential act; the caller records who did it, which dataset, and how
  many rows.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Callable, Iterable, Iterator

from django.db.models import QuerySet
from django.utils import timezone

from AI.models import (
    AIUsageLog,
    AuditLog,
    ModelPoolTransaction,
    Payment,
    Role,
    Subscription,
    SupportTicket,
    TokenTransaction,
    User,
)

logger = logging.getLogger("AI.export")

# Rows are pulled from the database in batches of this size while streaming.
CHUNK = 500


class _Echo:
    """A file-like object whose ``write`` returns the line instead of storing it.

    This is what lets ``csv.writer`` feed a generator directly.
    """

    def write(self, value: str) -> str:
        return value


@dataclass
class Dataset:
    key: str
    label: str
    description: str
    icon: str
    date_field: str
    headers: list[str]
    #: ``() -> QuerySet`` — the unfiltered, ordered, prefetched source.
    source: Callable[[], QuerySet]
    #: ``(obj) -> list`` — one CSV row.
    row: Callable[[object], list]
    #: Shown on the card so an administrator knows what they are about to send.
    sensitivity: str = ""


def _dt(value) -> str:
    """Timestamps as local ISO-ish text — readable in a spreadsheet, still sortable."""
    if not value:
        return ""
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _name(user) -> str:
    return getattr(user, "display_name", "") or getattr(user, "username", "") or ""


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
def _students_source() -> QuerySet:
    return (
        User.objects.filter(role=Role.STUDENT)
        .select_related("token_account", "referred_by")
        .order_by("-date_joined")
    )


def _students_row(user: User) -> list:
    account = getattr(user, "token_account", None)
    return [
        user.pk,
        user.username,
        user.full_name,
        user.email,
        user.phone,
        user.get_status_display(),
        user.get_source_display(),
        "yes" if user.is_verified else "no",
        account.balance if account else 0,
        account.total_allocated if account else 0,
        account.total_consumed if account else 0,
        user.referral_code,
        _name(user.referred_by) if user.referred_by_id else "",
        _dt(user.date_joined),
        _dt(user.last_login),
        _dt(user.last_seen_at),
    ]


def _payments_source() -> QuerySet:
    return Payment.objects.select_related("user", "plan").order_by("-created_at")


def _payments_row(payment: Payment) -> list:
    return [
        payment.reference,
        _name(payment.user),
        payment.user.username if payment.user_id else "",
        payment.plan.name if payment.plan_id else "",
        payment.amount,
        payment.currency,
        payment.get_method_display(),
        payment.get_status_display(),
        payment.tokens_to_grant,
        "yes" if payment.tokens_credited else "no",
        payment.provider_txn_id,
        _dt(payment.created_at),
        _dt(payment.paid_at),
    ]


def _token_ledger_source() -> QuerySet:
    return TokenTransaction.objects.select_related("user", "created_by").order_by("-created_at")


def _token_ledger_row(entry: TokenTransaction) -> list:
    return [
        entry.pk,
        _name(entry.user),
        entry.user.username if entry.user_id else "",
        entry.get_transaction_type_display(),
        entry.source,
        entry.amount,
        entry.balance_after,
        entry.description,
        _name(entry.created_by) if entry.created_by_id else "system",
        _dt(entry.created_at),
    ]


def _usage_source() -> QuerySet:
    return AIUsageLog.objects.select_related("user").order_by("-created_at")


def _usage_row(log: AIUsageLog) -> list:
    return [
        log.pk,
        _name(log.user),
        log.user.username if log.user_id else "",
        log.model,
        log.model_key,
        log.provider,
        log.input_tokens,
        log.output_tokens,
        log.reasoning_tokens,
        log.total_tokens,
        log.tokens_charged,
        log.latency_ms,
        "yes" if log.success else "no",
        log.error_code,
        "yes" if log.had_image else "no",
        _dt(log.created_at),
    ]


def _audit_source() -> QuerySet:
    return AuditLog.objects.select_related("actor").order_by("-created_at")


def _audit_row(entry: AuditLog) -> list:
    return [
        entry.pk,
        entry.get_action_display(),
        _name(entry.actor) if entry.actor_id else "system",
        entry.target_type,
        entry.target_label,
        entry.description,
        entry.old_value[:500],
        entry.new_value[:500],
        entry.ip_address or "",
        _dt(entry.created_at),
    ]


def _tickets_source() -> QuerySet:
    return SupportTicket.objects.select_related("user", "assigned_to").order_by("-created_at")


def _tickets_row(ticket: SupportTicket) -> list:
    return [
        ticket.reference,
        _name(ticket.user),
        ticket.get_category_display(),
        ticket.get_priority_display(),
        ticket.get_status_display(),
        ticket.subject,
        _name(ticket.assigned_to) if ticket.assigned_to_id else "",
        _dt(ticket.created_at),
        _dt(ticket.resolved_at),
    ]


def _subscriptions_source() -> QuerySet:
    return Subscription.objects.select_related("user", "plan", "payment").order_by("-created_at")


def _subscriptions_row(sub: Subscription) -> list:
    return [
        sub.pk,
        _name(sub.user),
        sub.user.username if sub.user_id else "",
        sub.plan.name if sub.plan_id else "",
        sub.get_status_display(),
        sub.tokens_granted,
        sub.payment.reference if sub.payment_id else "",
        _dt(sub.starts_at),
        _dt(sub.ends_at),
        _dt(sub.created_at),
    ]


def _pool_source() -> QuerySet:
    # The per-model ledger. The old single pool stopped moving when tokens
    # became a currency per model, so exporting it would hand over a file that
    # silently ends on the day that happened.
    return ModelPoolTransaction.objects.select_related(
        "ai_model", "student", "created_by"
    ).order_by("-created_at", "-id")


def _pool_row(entry: ModelPoolTransaction) -> list:
    return [
        entry.pk,
        entry.ai_model.name,
        entry.get_action_display(),
        entry.amount,
        entry.balance_after,
        _name(entry.student) if entry.student_id else "",
        _name(entry.created_by) if entry.created_by_id else "system",
        entry.description,
        _dt(entry.created_at),
    ]


DATASETS: dict[str, Dataset] = {
    d.key: d
    for d in [
        Dataset(
            key="students",
            label="Students",
            description="Every student account with its balance, origin and activity dates.",
            icon="i-users",
            date_field="date_joined",
            headers=[
                "id", "username", "full name", "email", "phone", "status", "source", "verified",
                "balance", "total allocated", "total consumed", "referral code", "referred by",
                "joined", "last login", "last seen",
            ],
            source=_students_source,
            row=_students_row,
            sensitivity="Contains names, e-mail addresses and phone numbers.",
        ),
        Dataset(
            key="payments",
            label="Payments",
            description="Every payment attempt, its method, state and the tokens it released.",
            icon="i-receipt",
            date_field="created_at",
            headers=[
                "reference", "student", "username", "plan", "amount", "currency", "method",
                "status", "tokens", "credited", "provider reference", "created", "paid",
            ],
            source=_payments_source,
            row=_payments_row,
            sensitivity="Financial records tied to named students.",
        ),
        Dataset(
            key="token_ledger",
            label="Token ledger",
            description="Every movement on a student's balance, with the running total after it.",
            icon="i-coin",
            date_field="created_at",
            headers=[
                "id", "student", "username", "type", "source", "amount", "balance after", "description",
                "by", "when",
            ],
            source=_token_ledger_source,
            row=_token_ledger_row,
        ),
        Dataset(
            key="pool",
            label="Pool movements",
            description="Top-ups, distributions and reclaims against each model's supply.",
            icon="i-bolt",
            date_field="created_at",
            headers=[
                "id", "model", "action", "amount", "balance after", "student", "by", "description", "when",
            ],
            source=_pool_source,
            row=_pool_row,
        ),
        Dataset(
            key="usage",
            label="AI usage log",
            description="One row per provider request — tokens, latency and any error.",
            icon="i-chart",
            date_field="created_at",
            headers=[
                "id", "student", "username", "model", "model key", "provider", "input tokens",
                "output tokens", "reasoning tokens", "total tokens", "charged", "latency ms",
                "success", "error code", "had image", "when",
            ],
            source=_usage_source,
            row=_usage_row,
            sensitivity="No message content — metadata only.",
        ),
        Dataset(
            key="subscriptions",
            label="Subscriptions",
            description="Plan grants with their start and end dates.",
            icon="i-check-circle",
            date_field="created_at",
            headers=[
                "id", "student", "username", "plan", "status", "tokens granted", "payment",
                "starts", "ends", "created",
            ],
            source=_subscriptions_source,
            row=_subscriptions_row,
        ),
        Dataset(
            key="tickets",
            label="Support tickets",
            description="Ticket headers and their resolution state. Replies are not included.",
            icon="i-info",
            date_field="created_at",
            headers=[
                "reference", "student", "category", "priority", "status", "subject",
                "assigned to", "created", "resolved",
            ],
            source=_tickets_source,
            row=_tickets_row,
        ),
        Dataset(
            key="audit",
            label="Audit trail",
            description="Every consequential action, with the actor and the address it came from.",
            icon="i-history",
            date_field="created_at",
            headers=[
                "id", "action", "actor", "target type", "target", "description", "before",
                "after", "ip address", "when",
            ],
            source=_audit_source,
            row=_audit_row,
            sensitivity="Contains IP addresses.",
        ),
    ]
}


# ---------------------------------------------------------------------------
# Building an export
# ---------------------------------------------------------------------------
def parse_date(raw: str | None) -> date | None:
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date() if raw else None
    except ValueError:
        return None


def apply_range(queryset: QuerySet, field: str, start: date | None, end: date | None) -> QuerySet:
    """Filter to a closed date range, in the server's own timezone.

    The end date is inclusive of its whole day — an administrator asking for
    "up to the 31st" means the 31st, not midnight at its start.
    """
    def moment(day: date, edge: time):
        stamp = datetime.combine(day, edge)
        return timezone.make_aware(stamp) if timezone.is_naive(stamp) else stamp

    if start:
        queryset = queryset.filter(**{f"{field}__gte": moment(start, time.min)})
    if end:
        queryset = queryset.filter(**{f"{field}__lte": moment(end, time.max)})
    return queryset


def build_queryset(dataset: Dataset, start: date | None, end: date | None) -> QuerySet:
    return apply_range(dataset.source(), dataset.date_field, start, end)


def rows(dataset: Dataset, queryset: QuerySet) -> Iterator[list]:
    """Header first, then one row per record, pulled in batches."""
    yield dataset.headers
    for obj in queryset.iterator(chunk_size=CHUNK):
        try:
            yield dataset.row(obj)
        except Exception:  # pragma: no cover — one bad row must not kill the file
            logger.exception("Skipped a row in the %s export", dataset.key)


def stream(dataset: Dataset, queryset: QuerySet) -> Iterable[str]:
    """CSV lines, ready for a streaming response."""
    writer = csv.writer(_Echo())
    for row in rows(dataset, queryset):
        yield writer.writerow(row)


def filename(dataset: Dataset, start: date | None, end: date | None) -> str:
    span = ""
    if start and end:
        span = f"_{start:%Y%m%d}-{end:%Y%m%d}"
    elif start:
        span = f"_from-{start:%Y%m%d}"
    elif end:
        span = f"_to-{end:%Y%m%d}"
    return f"epc_{dataset.key}{span}_{timezone.localtime():%Y%m%d-%H%M}.csv"


def catalogue() -> list[Dataset]:
    return list(DATASETS.values())
