"""The referral campaign form, and the rules a campaign has to obey."""
from __future__ import annotations

from decimal import Decimal

from django import forms

from AI.models import AIModel, Plan, ReferralCampaign, RewardType


class ReferralCampaignForm(forms.ModelForm):
    class Meta:
        model = ReferralCampaign
        fields = [
            "code", "name", "description", "is_active",
            "starts_at", "expires_at", "usage_limit",
            "ai_model", "eligible_plans", "minimum_purchase",
            "reward_who", "reward_trigger",
            "referrer_reward_type", "referrer_reward_value",
            "referred_reward_type", "referred_reward_value",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "expires_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "description": forms.TextInput(),
            "eligible_plans": forms.SelectMultiple(attrs={"size": 8}),
        }
        help_texts = {
            "usage_limit": "Maximum signups. 0 means unlimited.",
            "minimum_purchase": "Smallest qualifying purchase. 0 means any.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["ai_model"].queryset = AIModel.objects.order_by("sort_order", "name")
        self.fields["ai_model"].empty_label = "All models"
        self.fields["ai_model"].required = False
        # Django would title-case the field name into "Ai model".
        self.fields["ai_model"].label = "AI model"
        self.fields["eligible_plans"].label = "Eligible plans"
        self.fields["eligible_plans"].queryset = (
            Plan.objects.select_related("ai_model").order_by("display_order", "price")
        )
        self.fields["eligible_plans"].required = False
        self.fields["expires_at"].required = False
        for name in ("starts_at", "expires_at"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"]

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip().upper()
        if len(code) < 4:
            raise forms.ValidationError("A code needs at least four characters to be worth sharing.")
        if not code.isalnum():
            raise forms.ValidationError("Use letters and digits only — codes get retyped from paper.")
        clash = ReferralCampaign.objects.filter(code=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("Another campaign already uses that code.")
        return code

    def _check_reward(self, side: str, cleaned) -> None:
        """A reward has to be a possible amount of the kind it claims to be."""
        kind = cleaned.get(f"{side}_reward_type")
        value = cleaned.get(f"{side}_reward_value")
        if kind is None or value is None:
            return
        label = "referrer" if side == "referrer" else "new student"

        if kind == RewardType.NONE:
            return
        if value <= 0:
            self.add_error(
                f"{side}_reward_value",
                f"Set an amount, or choose “No reward” for the {label}.",
            )
            return
        if kind == RewardType.PERCENT and value > 100:
            self.add_error(
                f"{side}_reward_value",
                "A discount over 100% would pay the student to buy. Use 100 or less.",
            )
        if kind == RewardType.TOKENS and value != value.to_integral_value():
            self.add_error(f"{side}_reward_value", "Tokens are whole numbers.")

    def clean(self):
        cleaned = super().clean()
        self._check_reward("referrer", cleaned)
        self._check_reward("referred", cleaned)

        starts, expires = cleaned.get("starts_at"), cleaned.get("expires_at")
        if starts and expires and expires <= starts:
            self.add_error("expires_at", "The end has to come after the start.")

        # A discount can only be taken off a purchase, so a campaign offering
        # one has to wait for a purchase before it can pay out.
        discount_kinds = {RewardType.PERCENT, RewardType.FIXED}
        referred_kind = cleaned.get("referred_reward_type")
        if referred_kind in discount_kinds and cleaned.get("reward_trigger") == "signup":
            # Not an error: the discount is applied at checkout regardless, and
            # forcing the administrator to change an unrelated field would be
            # obtuse. Correct it quietly and say so on the page.
            cleaned["reward_trigger"] = "purchase"
            self.add_error(
                None,
                "A discount can only be applied to a purchase, so this campaign "
                "now pays out on purchase rather than at signup.",
            )

        model = cleaned.get("ai_model")
        plans = cleaned.get("eligible_plans")
        if model and plans:
            wrong = [p.name for p in plans if p.ai_model_id != model.pk]
            if wrong:
                self.add_error(
                    "eligible_plans",
                    f"These plans do not belong to {model.name}: {', '.join(wrong)}. "
                    "A code limited to one model can only apply to that model's plans.",
                )

        if cleaned.get("minimum_purchase") and cleaned["minimum_purchase"] < Decimal("0"):
            self.add_error("minimum_purchase", "A minimum cannot be negative.")
        return cleaned
