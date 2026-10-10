"""Public authentication forms."""
from __future__ import annotations

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from AI.models import User
from AI.services import referral_service


class LoginForm(forms.Form):
    identifier = forms.CharField(
        max_length=190,
        label="Username or e-mail",
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": "autofocus"}),
    )
    password = forms.CharField(
        max_length=128, label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"})
    )
    remember = forms.BooleanField(required=False, initial=True, label="Keep me signed in")

    def clean_identifier(self):
        return (self.cleaned_data["identifier"] or "").strip()


class SignupForm(forms.Form):
    full_name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(max_length=190, label="E-mail address")
    username = forms.CharField(max_length=40, label="Username")
    password = forms.CharField(min_length=8, max_length=128, label="Password", widget=forms.PasswordInput)
    referral_code = forms.CharField(max_length=16, required=False, label="Referral code (optional)")
    accept_terms = forms.BooleanField(label="I agree to the Terms of Service and Privacy Policy")

    def clean_username(self):
        username = (self.cleaned_data["username"] or "").strip()
        if not all(char.isalnum() or char in "._-" for char in username):
            raise ValidationError("Use letters, numbers, dots, underscores or hyphens only.")
        if len(username) < 3:
            raise ValidationError("Username must be at least 3 characters.")
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("That username is already taken.")
        return username

    def clean_email(self):
        email = (self.cleaned_data["email"] or "").strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this e-mail already exists.")
        return email

    def clean_referral_code(self):
        code = (self.cleaned_data.get("referral_code") or "").strip().upper()
        if not code:
            return ""
        referrer, campaign, error = referral_service.resolve_code(
            code, email=self.data.get("email", "")
        )
        if error:
            raise ValidationError(error)
        # One field, two kinds of code. The view reads whichever was found.
        self.referrer = referrer
        self.campaign = campaign
        return code

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        if password:
            user = User(
                username=cleaned.get("username") or "",
                email=cleaned.get("email") or "",
                full_name=cleaned.get("full_name") or "",
            )
            try:
                validate_password(password, user)
            except ValidationError as exc:
                self.add_error("password", exc)
        return cleaned


class OTPForm(forms.Form):
    code = forms.CharField(
        max_length=8,
        label="Verification code",
        widget=forms.TextInput(
            attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "pattern": "[0-9]*"}
        ),
    )

    def clean_code(self):
        code = (self.cleaned_data["code"] or "").strip()
        if not code.isdigit():
            raise ValidationError("Enter the numeric code we sent you.")
        return code


class ForgotPasswordForm(forms.Form):
    identifier = forms.CharField(max_length=190, label="Username or e-mail")

    def clean_identifier(self):
        return (self.cleaned_data["identifier"] or "").strip()


class ResetPasswordForm(forms.Form):
    password = forms.CharField(min_length=8, max_length=128, label="New password", widget=forms.PasswordInput)
    confirm_password = forms.CharField(max_length=128, label="Confirm new password", widget=forms.PasswordInput)

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirm = cleaned.get("confirm_password")
        if password and confirm and password != confirm:
            self.add_error("confirm_password", "The two passwords do not match.")
        if password:
            try:
                validate_password(password, self.user)
            except ValidationError as exc:
                self.add_error("password", exc)
        return cleaned
