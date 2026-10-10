"""Student-facing forms."""
from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from AI.models import NotificationPreference, User


class ProfileForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["full_name", "email", "phone"]
        labels = {"full_name": "Full name", "email": "E-mail address", "phone": "Phone number"}

    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip().lower()
        if email and User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Another account already uses this e-mail address.")
        return email


class ChangePasswordForm(forms.Form):
    current_password = forms.CharField(max_length=128, label="Current password", widget=forms.PasswordInput)
    new_password = forms.CharField(min_length=8, max_length=128, label="New password", widget=forms.PasswordInput)
    confirm_password = forms.CharField(max_length=128, label="Confirm new password", widget=forms.PasswordInput)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_current_password(self):
        password = self.cleaned_data["current_password"]
        if not self.user.check_password(password):
            raise ValidationError("That is not your current password.")
        return password

    def clean(self):
        cleaned = super().clean()
        new_password = cleaned.get("new_password")
        confirm = cleaned.get("confirm_password")
        if new_password and confirm and new_password != confirm:
            self.add_error("confirm_password", "The two passwords do not match.")
        if new_password:
            try:
                validate_password(new_password, self.user)
            except ValidationError as exc:
                self.add_error("new_password", exc)
        return cleaned


class NotificationPreferenceForm(forms.ModelForm):
    class Meta:
        model = NotificationPreference
        fields = [
            "email_enabled",
            "telegram_enabled",
            "low_token_alerts",
            "expiry_reminders",
            "payment_alerts",
            "announcements",
        ]
        labels = {
            "email_enabled": "E-mail notifications",
            "telegram_enabled": "Telegram notifications",
            "low_token_alerts": "Low token alerts",
            "expiry_reminders": "Plan expiry reminders",
            "payment_alerts": "Payment confirmations",
            "announcements": "Product announcements",
        }


class CheckoutForm(forms.Form):
    METHOD_CHOICES = [
        ("aba_payway", "ABA PayWay"),
        ("acleda_xpay", "ACLEDA Bank"),
        ("acleda_khqr", "ACLEDA KHQR"),
        ("binance", "Binance Pay"),
    ]
    method = forms.ChoiceField(choices=METHOD_CHOICES, label="Payment method")
    note = forms.CharField(max_length=255, required=False, label="Note (optional)")
    promo_code = forms.CharField(max_length=32, required=False, label="Promo code")


class PaymentProofForm(forms.Form):
    proof = forms.ImageField(label="Payment screenshot")
    note = forms.CharField(max_length=255, required=False, label="Reference or note")
