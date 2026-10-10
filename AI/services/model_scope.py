"""One answer to "which model am I looking at?", shared by the whole console.

Every figure in the admin console belongs to exactly one model — a wallet, a
pool movement, a package, a plan, a payment for that plan, a usage log row. The
database has said so since per-model currencies were introduced: ``ai_model`` is
a real foreign key on all of them. What the console never had was a way to *ask*
that question, so an administrator saw four models' numbers added together and
had to work out which was which from context.

This module is that question, asked once. A view resolves the scope, narrows its
querysets through the helpers here, and renders the same selector as every other
page. Because the narrowing lives here rather than in each view, "all models"
and "one model" cannot drift apart: the aggregate is the same query without the
filter, never a second query that might count something twice.

The choice is remembered in the session, so moving from tokens to usage to
payments keeps you looking at the model you were looking at. It is validated
against the live catalogue on the way in and on the way out, so a model that is
deleted or renamed degrades to "all models" instead of to an error page.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.db.models import QuerySet

from AI.models import AIModel

#: The query parameter and session key. One name, so a link built anywhere in
#: the console selects the same thing when it is followed.
PARAM = "model"
SESSION_KEY = "console_model_scope"
ALL = "all"
ALL_LABEL = "All models"


@dataclass(frozen=True)
class ModelScope:
    """The selected model, or ``None`` meaning every model at once."""

    model: AIModel | None
    catalogue: tuple[AIModel, ...]

    # -- identity ----------------------------------------------------------
    @property
    def key(self) -> str:
        return self.model.key if self.model else ALL

    @property
    def is_all(self) -> bool:
        return self.model is None

    @property
    def label(self) -> str:
        return self.model.name if self.model else ALL_LABEL

    @property
    def token_type(self) -> str:
        return self.model.token_type if self.model else ""

    @property
    def model_id(self) -> int | None:
        return self.model.pk if self.model else None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return self.key

    # -- link building -----------------------------------------------------
    def querystring(self, **extra) -> str:
        """``?model=gpt-4o&page=2`` — for links that must keep the scope."""
        parts = [] if self.is_all else [f"{PARAM}={self.key}"]
        parts += [f"{k}={v}" for k, v in extra.items() if v not in (None, "")]
        return ("?" + "&".join(parts)) if parts else ""

    def options(self) -> list[dict]:
        """Rows for the selector: All models first, then the catalogue."""
        rows = [{
            "key": ALL,
            "label": ALL_LABEL,
            "is_active": self.is_all,
            "provider": "",
        }]
        for model in self.catalogue:
            rows.append({
                "key": model.key,
                "label": model.name,
                "is_active": (not self.is_all) and model.pk == self.model.pk,
                "provider": model.provider_name,
                "model": model,
            })
        return rows

    # -- narrowing ---------------------------------------------------------
    # Each helper takes a queryset and returns it filtered to the scope. On
    # "all models" every one of them returns the queryset untouched, which is
    # what makes the aggregate view provably the sum of the individual ones
    # rather than a separately written query that could disagree with them.
    def by(self, qs: QuerySet, field: str = "ai_model") -> QuerySet:
        """Narrow on a direct or traversed ``AIModel`` relation."""
        if self.is_all:
            return qs
        return qs.filter(**{field: self.model})

    def wallets(self, qs: QuerySet) -> QuerySet:
        return self.by(qs)

    def transactions(self, qs: QuerySet) -> QuerySet:
        return self.by(qs)

    def allocations(self, qs: QuerySet) -> QuerySet:
        return self.by(qs)

    def plans(self, qs: QuerySet) -> QuerySet:
        return self.by(qs)

    def subscriptions(self, qs: QuerySet) -> QuerySet:
        return self.by(qs, "plan__ai_model")

    def payments(self, qs: QuerySet) -> QuerySet:
        return self.by(qs, "plan__ai_model")

    def usage(self, qs: QuerySet) -> QuerySet:
        """Usage and messages record the model as its wire key, not a row."""
        if self.is_all:
            return qs
        return qs.filter(model_key=self.model.key)

    def conversations(self, qs: QuerySet) -> QuerySet:
        """A conversation can span models, so it counts if it ever used this one.

        ``distinct`` matters: without it a conversation with twelve GPT-4o
        messages would be counted twelve times, and the per-model totals would
        add up to more than the "all models" total.
        """
        if self.is_all:
            return qs
        return qs.filter(messages__model_key=self.model.key).distinct()

    def students(self, qs: QuerySet) -> QuerySet:
        """Students who hold or have held this model's tokens."""
        if self.is_all:
            return qs
        return qs.filter(ai_wallets__ai_model=self.model).distinct()


#: The console pages whose content is model-specific, by URL name. Kept here
#: rather than as a flag set by each view so the selector cannot appear on one
#: page and not on its sibling, and so the answer to "is this page filterable?"
#: has exactly one place to look. A page absent from this set is genuinely not
#: model-specific — settings, support tickets, audit logs, exports, health —
#: and showing it a model selector that changed nothing would be a lie.
MODEL_AWARE_VIEWS = frozenset({
    "console:dashboard",
    "console:users",
    "console:ai_tokens",
    "console:tokens",
    "console:token_history",
    "console:usage",
    "console:usage_limits",
    "console:plans",
    "console:payments",
    "console:subscriptions",
    "console:conversations",
    "console:referrals",
    "console:low_tokens",
    "console:expiring_tokens",
    "console:model_costs",
    "console:emails",
    "console:referral_campaigns",
})


def is_model_aware(request) -> bool:
    """Whether the page being rendered offers the model selector."""
    match = getattr(request, "resolver_match", None)
    return bool(match and match.view_name in MODEL_AWARE_VIEWS)


def catalogue() -> list[AIModel]:
    """Every model an administrator can manage, inactive ones included.

    Inactive models stay in the selector on purpose: their tokens, payments and
    history do not disappear when a model is switched off, and that history is
    exactly what an administrator needs to look at after switching it off.
    """
    return list(AIModel.objects.select_related("provider_ref").order_by("sort_order", "name"))


def resolve(request, *, remember: bool = True) -> ModelScope:
    """Work out which model this request is looking at.

    Precedence is explicit request, then remembered choice, then all models. An
    unknown key is not an error — it resolves to all models, so a stale
    bookmark or a deleted model shows a full page rather than a stack trace.
    """
    models = catalogue()
    by_key = {m.key: m for m in models}

    requested = (request.GET.get(PARAM) or "").strip()
    if requested:
        chosen = by_key.get(requested)
        if remember and hasattr(request, "session"):
            # Remember the resolved value, not the requested one, so a bad key
            # clears the memory instead of being stored and re-applied.
            request.session[SESSION_KEY] = chosen.key if chosen else ALL
        return ModelScope(chosen, tuple(models))

    remembered = ""
    if hasattr(request, "session"):
        remembered = request.session.get(SESSION_KEY) or ""
    return ModelScope(by_key.get(remembered), tuple(models))
