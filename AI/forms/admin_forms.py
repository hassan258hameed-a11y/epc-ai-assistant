"""Admin console forms."""
from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.utils import timezone

from AI.models import AccountSource, AccountStatus, AIModel, Announcement, Plan, PromoCode, Role, User


class AdminUserForm(forms.ModelForm):
    """Create or edit an account from the console."""

    password = forms.CharField(
        required=False,
        max_length=128,
        label="Password",
        help_text="Leave blank to generate a secure password automatically.",
        widget=forms.PasswordInput(render_value=False),
    )
    initial_tokens = forms.IntegerField(
        required=False, min_value=0, initial=0, label="Starting tokens",
        help_text="Credited as an admin grant when the account is created.",
    )
    # The four things the Create Accounts page asks that an edit does not.
    initial_model = forms.ModelChoiceField(
        queryset=AIModel.objects.filter(is_active=True).order_by("sort_order", "name"),
        required=False, label="Draw from", empty_label=None,
    )
    plan = forms.ModelChoiceField(
        queryset=Plan.objects.filter(is_active=True).order_by("price", "name"),
        required=False, label="Plan", empty_label="No plan",
    )
    email_credentials = forms.BooleanField(required=False, label="Email the credentials")
    force_password_change = forms.BooleanField(required=False, label="Require a password change")
    # Editing only: why an account is being disabled, kept with the account the
    # same way the Disable button on the student's page keeps it.
    disable_reason = forms.CharField(
        required=False, max_length=255, label="Reason for disabling",
        help_text="Only used when the status changes to Disabled. Shown on the student's page.",
    )

    class Meta:
        model = User
        fields = ["username", "full_name", "email", "phone", "source", "role", "status", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}
        labels = {
            "source": "Customer source",
            "notes": "Internal notes",
        }

    def __init__(self, *args, **kwargs):
        self.is_create = kwargs.pop("is_create", False)
        super().__init__(*args, **kwargs)
        if self.is_create:
            self.fields.pop("disable_reason", None)
        else:
            for name in ("initial_tokens", "initial_model", "plan", "email_credentials", "force_password_change"):
                self.fields.pop(name, None)
            self.fields["password"].help_text = "Leave blank to keep the current password."

    def clean(self):
        cleaned = super().clean()
        # Sending a login somewhere needs a somewhere.
        if cleaned.get("email_credentials") and not (cleaned.get("email") or "").strip():
            self.add_error("email", "Add an e-mail address, or switch off emailing the credentials.")
        return cleaned

    def clean_username(self):
        username = (self.cleaned_data["username"] or "").strip()
        if not all(char.isalnum() or char in "._-@+" for char in username):
            raise ValidationError("Use letters, numbers, dots, underscores, hyphens, @ or + only.")
        query = User.objects.filter(username__iexact=username)
        if self.instance and self.instance.pk:
            query = query.exclude(pk=self.instance.pk)
        if query.exists():
            raise ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if not email:
            return ""
        query = User.objects.filter(email__iexact=email)
        if self.instance and self.instance.pk:
            query = query.exclude(pk=self.instance.pk)
        if query.exists():
            raise ValidationError("Another account already uses this e-mail address.")
        return email

    def clean_password(self):
        password = self.cleaned_data.get("password") or ""
        if password:
            try:
                validate_password(password)
            except ValidationError as exc:
                raise ValidationError(exc.messages)
        return password


class TokenGrantForm(forms.Form):
    """Move a student's balance: add to it, take from it, or set it outright.

    ``set`` is the "assign 50,000 tokens to Student A" case — the student ends
    up holding exactly that number, whatever they held before.
    """

    ACTION_CHOICES = [
        ("set", "Assign (set balance to)"),
        ("add", "Add tokens"),
        ("remove", "Remove tokens"),
    ]

    user_id = forms.IntegerField(widget=forms.HiddenInput)
    action = forms.ChoiceField(choices=ACTION_CHOICES, initial="set", label="Action")
    amount = forms.IntegerField(min_value=0, max_value=100_000_000, label="Token amount")
    reason = forms.CharField(max_length=255, required=False, label="Reason / note")

    def clean_user_id(self):
        user_id = self.cleaned_data["user_id"]
        if not User.objects.filter(pk=user_id).exists():
            raise ValidationError("That student no longer exists.")
        return user_id

    def clean(self):
        cleaned = super().clean()
        # Only "set" is meaningful at zero — it empties the balance. Adding or
        # removing nothing is a mistake worth catching.
        if cleaned.get("action") in {"add", "remove"} and not cleaned.get("amount"):
            self.add_error("amount", "Enter an amount greater than zero.")
        return cleaned


class AIModelForm(forms.ModelForm):
    """Create or edit one row of the model catalogue."""

    class Meta:
        model = AIModel
        fields = [
            "key", "name", "description", "best_for", "provider_ref", "is_active", "supports_vision",
            "thinking_mode", "max_output_tokens", "temperature", "token_allocation",
            "input_cost_per_1m", "output_cost_per_1m", "warning_thresholds", "sort_order",
            "default_daily_limit", "default_weekly_limit", "default_monthly_limit",
        ]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}
        labels = {
            "key": "Provider model id",
            "name": "Display name",
            "provider_ref": "Provider",
            "thinking_mode": "Reasoning",
            "is_active": "Available to students",
            "supports_vision": "Accepts images",
            "max_output_tokens": "Output budget per reply",
            "token_allocation": "Token allocation (0 = unlimited)",
            "input_cost_per_1m": "Input cost / 1M tokens (USD)",
            "output_cost_per_1m": "Output cost / 1M tokens (USD)",
            "warning_thresholds": "Low-token warning points (%)",
            "sort_order": "Display order",
        }
        help_texts = {
            "key": "Exactly as the provider expects it, e.g. claude-sonnet-5.",
            "provider_ref": "Which vendor answers. Disabling the vendor disables this model.",
            "thinking_mode": "Thinking is billed to the student as output tokens.",
            "token_allocation": "Once this many tokens are spent the model stops answering.",
            "warning_thresholds": (
                "Percentages of a student's allocation at which to e-mail them "
                "about this model, e.g. 50,25,10. Leave blank to use the "
                "platform default from Settings."
            ),
        }

    LIMIT_FIELDS = ("default_daily_limit", "default_weekly_limit", "default_monthly_limit")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Optional in the form: a blank box means "no limit for this window",
        # and a form posted by an older page without them keeps the model's
        # current values rather than failing.
        for name in self.LIMIT_FIELDS:
            self.fields[name].required = False

    def _clean_limit(self, name):
        value = self.cleaned_data.get(name)
        if value in (None, ""):
            if name not in self.data and self.instance is not None and self.instance.pk:
                return getattr(self.instance, name)
            return 0
        return value

    def clean_best_for(self):
        # One line in the picker: collapse whatever spacing was typed.
        return " ".join((self.cleaned_data.get("best_for") or "").split())

    def clean_default_daily_limit(self):
        return self._clean_limit("default_daily_limit")

    def clean_default_weekly_limit(self):
        return self._clean_limit("default_weekly_limit")

    def clean_default_monthly_limit(self):
        return self._clean_limit("default_monthly_limit")

    def clean(self):
        cleaned = super().clean()
        daily = cleaned.get("default_daily_limit") or 0
        weekly = cleaned.get("default_weekly_limit") or 0
        monthly = cleaned.get("default_monthly_limit") or 0
        # A shorter window larger than a longer one can never be reached; say
        # so instead of saving a limit that silently does nothing.
        if daily and weekly and daily > weekly:
            self.add_error("default_daily_limit", "The daily limit cannot be larger than the weekly limit.")
        if weekly and monthly and weekly > monthly:
            self.add_error("default_weekly_limit", "The weekly limit cannot be larger than the monthly limit.")
        return cleaned

    def clean_warning_thresholds(self):
        """Accept it only if it parses into usable percentages.

        The field is free text so "50, 25, 10" works, but silently ignoring a
        typo would leave an administrator believing they had changed something
        they had not.
        """
        raw = (self.cleaned_data.get("warning_thresholds") or "").strip()
        if not raw:
            return ""
        parsed = AIModel(warning_thresholds=raw).warning_points
        if not parsed:
            raise ValidationError(
                "Enter one or more percentages between 1 and 100, separated by "
                "commas — for example 50,25,10."
            )
        return ",".join(str(point) for point in parsed)

    def clean_provider_ref(self):
        """Refuse a vendor with no implementation behind it.

        Saving a model that names an unregistered provider would produce a row
        that looks fine in the console and fails at the first message.
        """
        provider = self.cleaned_data.get("provider_ref")
        if provider is None:
            raise ValidationError("Pick the provider that serves this model.")

        from AI.services.providers import available_slugs

        if provider.slug not in available_slugs():
            raise ValidationError(
                f"No provider implementation is registered for '{provider.slug}'."
            )
        return provider

    def save(self, commit=True):
        model = super().save(commit=False)
        # Keep the routing slug in step with the vendor row the admin picked.
        if model.provider_ref_id:
            model.provider = model.provider_ref.slug
        if commit:
            model.save()
        return model

    def clean_key(self):
        key = (self.cleaned_data["key"] or "").strip()
        query = AIModel.objects.filter(key__iexact=key)
        if self.instance and self.instance.pk:
            query = query.exclude(pk=self.instance.pk)
        if query.exists():
            raise ValidationError("A model with that id already exists.")
        return key


def _model_choices():
    """Callable so the picker reflects the catalogue at render time, not import."""
    return [
        (model.pk, f"{model.provider_name} — {model.name}")
        for model in AIModel.objects.select_related("provider_ref").order_by("sort_order", "name")
    ]


class ModelTokenGrantForm(forms.Form):
    """Move one student's balance **for one model**.

    Every field that could make this ambiguous is required and validated here:
    which student, which model, how much, and what kind of move. Nothing falls
    back to a default, because a distribution that silently picked a model would
    be a billing error nobody could see.
    """

    ACTION_CHOICES = [
        ("add", "Add tokens"),
        ("set", "Assign (set balance to)"),
        ("remove", "Take tokens back"),
    ]

    user_id = forms.IntegerField(widget=forms.HiddenInput)
    model_id = forms.TypedChoiceField(
        coerce=int, choices=_model_choices, label="AI model",
        help_text="Tokens are separate per model. Only this model's balance changes.",
    )
    action = forms.ChoiceField(choices=ACTION_CHOICES, initial="add", label="Action")
    amount = forms.IntegerField(min_value=0, max_value=100_000_000, label="Token amount")
    reason = forms.CharField(max_length=255, required=False, label="Reason / note")

    def clean_user_id(self):
        user_id = self.cleaned_data["user_id"]
        if not User.objects.filter(pk=user_id).exists():
            raise ValidationError("That student no longer exists.")
        return user_id

    def clean_model_id(self):
        model_id = self.cleaned_data["model_id"]
        if not AIModel.objects.filter(pk=model_id).exists():
            raise ValidationError("That AI model no longer exists.")
        return model_id

    def clean(self):
        cleaned = super().clean()
        # Only "set" is meaningful at zero — it empties that model's balance.
        if cleaned.get("action") in {"add", "remove"} and not cleaned.get("amount"):
            self.add_error("amount", "Enter an amount greater than zero.")
        return cleaned

    @property
    def ai_model(self):
        return AIModel.objects.filter(pk=self.cleaned_data.get("model_id")).first()

    @property
    def student(self):
        return User.objects.filter(pk=self.cleaned_data.get("user_id")).first()


class ModelPoolTopUpForm(forms.Form):
    """Add to (or correct) the administrator's supply for one model."""

    model_id = forms.TypedChoiceField(coerce=int, choices=_model_choices, label="AI model")
    amount = forms.IntegerField(
        min_value=-100_000_000, max_value=100_000_000, label="Token amount",
        help_text="Positive adds to the pool. Negative corrects it downwards.",
    )
    note = forms.CharField(max_length=255, required=False, label="Note")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount == 0:
            raise ValidationError("Enter a token amount.")
        return amount

    def clean_model_id(self):
        model_id = self.cleaned_data["model_id"]
        if not AIModel.objects.filter(pk=model_id).exists():
            raise ValidationError("That AI model no longer exists.")
        return model_id

    @property
    def ai_model(self):
        return AIModel.objects.filter(pk=self.cleaned_data.get("model_id")).first()


class ModelAdjustmentForm(forms.Form):
    """A signed correction to one wallet that does not touch the pool.

    The reason is mandatory: an adjustment without one is indistinguishable from
    a mistake when someone reads the audit log six months later.
    """

    user_id = forms.IntegerField(widget=forms.HiddenInput)
    model_id = forms.TypedChoiceField(coerce=int, choices=_model_choices, label="AI model")
    delta = forms.IntegerField(
        min_value=-100_000_000, max_value=100_000_000, label="Adjustment",
        help_text="Positive adds tokens, negative removes them.",
    )
    reason = forms.CharField(max_length=255, label="Reason (required)")

    def clean_delta(self):
        delta = self.cleaned_data["delta"]
        if delta == 0:
            raise ValidationError("Enter a non-zero adjustment.")
        return delta

    def clean_reason(self):
        reason = (self.cleaned_data["reason"] or "").strip()
        if not reason:
            raise ValidationError("Every manual adjustment needs a reason.")
        return reason


class PlanForm(forms.ModelForm):
    """Everything about one package: what it holds, what it costs, how fast it spends.

    The three ceilings are ordinary fields with ordinary validation. There is no
    percentage anywhere: an administrator types the number of tokens a day, a
    week and a month may take, and that is exactly what gets enforced.
    """

    class Meta:
        model = Plan
        fields = [
            "name", "description", "ai_model", "tokens", "price", "currency",
            "duration_days", "daily_limit", "weekly_limit", "monthly_limit",
            "features", "is_active", "is_featured", "display_order",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
            "features": forms.Textarea(attrs={"rows": 5, "placeholder": "One feature per line"}),
        }
        labels = {
            "ai_model": "Model these tokens buy",
            "tokens": "Tokens included",
            "duration_days": "Duration (days)",
            "daily_limit": "Daily limit",
            "weekly_limit": "Weekly limit",
            "monthly_limit": "Monthly limit",
            "is_featured": "Highlight as most popular",
            "display_order": "Display order",
        }
        help_texts = {
            "daily_limit": "Tokens spendable per rolling 24 hours. 0 = no daily ceiling.",
            "weekly_limit": "Tokens spendable per rolling 7 days. 0 = no weekly ceiling.",
            "monthly_limit": "Tokens spendable per monthly cycle. 0 = no monthly ceiling.",
        }

    #: Prefix for the per-model bundle amounts, one field per other model.
    BUNDLE_PREFIX = "bundle_"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A plan is sold for one model — that is what its price is booked
        # against — but it may hand out others alongside. Rather than a
        # formset, each model gets a plain integer field, so the whole plan
        # including its bundle is one form that saves in one step.
        from AI.models import AIModel

        existing = {}
        if self.instance.pk:
            existing = {
                grant.ai_model_id: grant.tokens
                for grant in self.instance.model_grants.all()
            }

        self.bundle_fields = []
        for model in AIModel.objects.order_by("sort_order", "name"):
            name = f"{self.BUNDLE_PREFIX}{model.pk}"
            self.fields[name] = forms.IntegerField(
                required=False, min_value=0, initial=existing.get(model.pk, 0),
                label=model.name,
                help_text=f"Extra {model.name} tokens included. 0 = not included.",
            )
            self.bundle_fields.append(self[name])

    def bundle_amounts(self) -> dict[int, int]:
        """``{model_pk: tokens}`` for every additional model with an amount."""
        amounts = {}
        for key, value in self.cleaned_data.items():
            if not key.startswith(self.BUNDLE_PREFIX):
                continue
            tokens = int(value or 0)
            if tokens > 0:
                amounts[int(key[len(self.BUNDLE_PREFIX):])] = tokens
        return amounts

    def save(self, commit: bool = True):
        plan = super().save(commit=commit)
        if commit:
            self.save_bundle(plan)
        return plan

    def save_bundle(self, plan) -> None:
        """Bring the plan's extra-model rows in line with what was submitted.

        The plan's own model is skipped: its amount lives in ``tokens``, and a
        grant row for it as well would credit that model twice.
        """
        from AI.models import PlanModelGrant

        amounts = self.bundle_amounts()
        amounts.pop(plan.ai_model_id, None)

        PlanModelGrant.objects.filter(plan=plan).exclude(
            ai_model_id__in=amounts.keys()
        ).delete()
        for model_id, tokens in amounts.items():
            PlanModelGrant.objects.update_or_create(
                plan=plan, ai_model_id=model_id, defaults={"tokens": tokens},
            )

    def clean_tokens(self):
        tokens = self.cleaned_data["tokens"]
        if tokens <= 0:
            raise ValidationError("A package must include at least one token.")
        return tokens

    def clean_price(self):
        price = self.cleaned_data["price"]
        if price < 0:
            raise ValidationError("Price cannot be negative.")
        return price

    def _clean_limit(self, field: str) -> int:
        value = int(self.cleaned_data.get(field) or 0)
        if value < 0:
            raise ValidationError("A limit cannot be negative. Use 0 for no ceiling.")
        return value

    def clean_daily_limit(self):
        return self._clean_limit("daily_limit")

    def clean_weekly_limit(self):
        return self._clean_limit("weekly_limit")

    def clean_monthly_limit(self):
        return self._clean_limit("monthly_limit")

    def clean(self):
        """Reject ceilings that contradict each other.

        A weekly limit below the daily one, or a monthly below the weekly, is
        never what somebody meant — the smaller window would be unreachable and
        the larger one would be the only real constraint. Catching it here beats
        having a student discover it.
        """
        data = super().clean()
        tokens = data.get("tokens") or 0
        daily = data.get("daily_limit") or 0
        weekly = data.get("weekly_limit") or 0
        monthly = data.get("monthly_limit") or 0

        if daily and weekly and weekly < daily:
            self.add_error("weekly_limit", "The weekly limit cannot be below the daily one.")
        if weekly and monthly and monthly < weekly:
            self.add_error("monthly_limit", "The monthly limit cannot be below the weekly one.")
        if daily and not weekly and monthly and monthly < daily:
            self.add_error("monthly_limit", "The monthly limit cannot be below the daily one.")

        for field, value in (("daily_limit", daily), ("weekly_limit", weekly), ("monthly_limit", monthly)):
            if tokens and value > tokens:
                self.add_error(
                    field,
                    "A ceiling above the package size has no effect — lower it, or set it to 0.",
                )
        return data


class BulkImportForm(forms.Form):
    file = forms.FileField(
        label="CSV or XLSX file",
        help_text="Columns: username, full_name, email, password, source, tokens, phone, notes",
    )


class AnnouncementForm(forms.ModelForm):
    """A broadcast: what it says, who gets it, an optional button, and when.

    "On a plan" and "with a model" need the plan or model named; a button needs
    both its words and a link; a send time has to be in the future.
    """

    scheduled_for = forms.DateTimeField(
        required=False, input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )

    class Meta:
        model = Announcement
        fields = ["title", "body", "level", "audience", "is_pinned", "audience_plan", "audience_model",
                  "action_label", "action_url", "scheduled_for", "telegram_channel", "telegram_group", "telegram_private"]
        widgets = {"body": forms.Textarea(attrs={"rows": 5})}
        labels = {
            "telegram_channel": "Post to the Telegram channel",
            "telegram_group": "Post to the Telegram group",
            "telegram_private": "Also send to students' Telegram chats",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audience_model"].queryset = AIModel.objects.filter(is_active=True)

    def clean_telegram_channel(self):
        # Nothing to post to without a configured channel; saying "no" here
        # keeps the announcement's record honest about what was attempted.
        from AI.services import telegram_service

        return bool(self.cleaned_data.get("telegram_channel")) and telegram_service.channel_configured()

    def clean_telegram_group(self):
        from AI.services import telegram_service

        return bool(self.cleaned_data.get("telegram_group")) and telegram_service.group_configured()

    def clean_telegram_private(self):
        from AI.services import telegram_service

        return bool(self.cleaned_data.get("telegram_private")) and telegram_service.is_configured()

    def clean_action_url(self):
        url = (self.cleaned_data.get("action_url") or "").strip()
        # An in-site path, or an https link — never javascript: or data:.
        if url and not (url.startswith("/") and not url.startswith("//") or url.startswith("https://")):
            raise ValidationError("Use a path on this site, like /app/pricing/, or an https:// link.")
        return url

    def clean_scheduled_for(self):
        when = self.cleaned_data.get("scheduled_for")
        if when and when <= timezone.now():
            raise ValidationError("Pick a time in the future, or leave it empty to send now.")
        return when

    def clean(self):
        cleaned = super().clean()
        audience = cleaned.get("audience")
        if audience == "plan" and not cleaned.get("audience_plan"):
            self.add_error("audience_plan", "Choose which plan's students get it.")
        if audience == "model" and not cleaned.get("audience_model"):
            self.add_error("audience_model", "Choose which model's students get it.")
        if audience != "plan":
            cleaned["audience_plan"] = None
        if audience != "model":
            cleaned["audience_model"] = None
        label, url = (cleaned.get("action_label") or "").strip(), cleaned.get("action_url") or ""
        if bool(label) != bool(url):
            self.add_error("action_label" if url else "action_url", "A button needs both its words and a link.")
        return cleaned


class UserFilterForm(forms.Form):
    """Non-blocking filter form — every field is optional."""

    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False, choices=[("", "All statuses")] + list(AccountStatus.choices), label="Status"
    )
    source = forms.ChoiceField(
        required=False, choices=[("", "All sources")] + list(AccountSource.choices), label="Source"
    )
    role = forms.ChoiceField(required=False, choices=[("", "All roles")] + list(Role.choices), label="Role")
    tokens = forms.ChoiceField(
        required=False,
        choices=[("", "Any balance"), ("empty", "No tokens"), ("low", "Low balance"), ("healthy", "Healthy")],
        label="Tokens",
    )
    plan = forms.ChoiceField(required=False, choices=[], label="Plan")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].choices = [("", "All plans")] + [
            (str(plan.pk), plan.name) for plan in Plan.objects.all().only("id", "name")
        ]


class PromoCodeForm(forms.ModelForm):
    """Create or edit a promo code."""

    class Meta:
        model = PromoCode
        fields = [
            "code", "description", "kind", "value", "max_uses", "per_user_limit",
            "min_purchase", "ai_model", "eligible_plans", "starts_at", "expires_at", "is_active",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "eligible_plans": forms.SelectMultiple(attrs={"size": 6}),
        }
        labels = {
            "kind": "Discount type",
            "value": "Amount",
            "max_uses": "Total uses (0 = unlimited)",
            "per_user_limit": "Uses per student (0 = unlimited)",
            "min_purchase": "Minimum package price (USD)",
            "ai_model": "Only for this model",
            "eligible_plans": "Only for these packages",
            "starts_at": "Starts",
            "expires_at": "Expires",
            "is_active": "Active",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["eligible_plans"].queryset = Plan.objects.filter(is_active=True).select_related("ai_model").order_by("display_order", "price")
        self.fields["ai_model"].required = False
        self.fields["ai_model"].empty_label = "Any model"
        self.fields["eligible_plans"].required = False
        for name in ("starts_at", "expires_at"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
            # A datetime-local input only accepts "YYYY-MM-DDTHH:MM" in local time.
            moment = getattr(self.instance, name, None) if self.instance and self.instance.pk else None
            if moment and not self.is_bound:
                self.initial[name] = timezone.localtime(moment).strftime("%Y-%m-%dT%H:%M")

    def clean_code(self):
        return "".join((self.cleaned_data.get("code") or "").split()).upper()

    def clean(self):
        cleaned = super().clean()
        kind, value = cleaned.get("kind"), cleaned.get("value")
        if kind == "percent" and value is not None and value > 100:
            self.add_error("value", "A percentage cannot be more than 100.")
        starts, expires = cleaned.get("starts_at"), cleaned.get("expires_at")
        if starts and expires and expires <= starts:
            self.add_error("expires_at", "The code must expire after it starts.")
        return cleaned
