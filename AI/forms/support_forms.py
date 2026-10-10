"""Support request forms."""
from __future__ import annotations

from django import forms
from django.core.exceptions import ValidationError

from AI.models import TicketCategory, TicketPriority, TicketStatus
from AI.utils.uploads import InvalidUpload, validate_document


class SupportRequestForm(forms.Form):
    """The "submit a request" form a student fills in."""

    full_name = forms.CharField(max_length=150, label="Full name")
    email = forms.EmailField(label="E-mail address")
    phone = forms.CharField(max_length=32, required=False, label="Contact number")
    category = forms.ChoiceField(choices=TicketCategory.choices, label="What is this about?")
    subject = forms.CharField(
        max_length=160, label="Subject", help_text="One line summarising the problem."
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 7}),
        label="Describe the problem",
        help_text="What happened, what you expected, and anything you have already tried.",
    )
    attachment = forms.FileField(
        required=False,
        label="Attachment",
        help_text="Optional. A screenshot or document that shows the problem.",
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-fill from the account, but leave every field editable: a student
        # may want to be reached somewhere other than their sign-in address.
        if user is not None and not self.is_bound:
            self.fields["full_name"].initial = user.display_name
            self.fields["email"].initial = user.email

    def clean_subject(self):
        subject = (self.cleaned_data["subject"] or "").strip()
        if len(subject) < 5:
            raise ValidationError("Please give the subject a little more detail.")
        return subject

    def clean_description(self):
        description = (self.cleaned_data["description"] or "").strip()
        if len(description) < 20:
            raise ValidationError(
                "Please describe the problem in a bit more detail so we can help properly."
            )
        if len(description) > 8000:
            raise ValidationError("That description is very long. Please shorten it to 8,000 characters.")
        return description

    def clean_attachment(self):
        """Validate by content, never by filename."""
        uploaded = self.cleaned_data.get("attachment")
        if not uploaded:
            return None
        try:
            return validate_document(uploaded)
        except InvalidUpload as exc:
            raise ValidationError(str(exc))


class TicketReplyForm(forms.Form):
    body = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Add to this ticket…"}),
        label="Message",
        max_length=8000,
        required=False,
    )
    attachment = forms.FileField(
        required=False, label="Attachment",
        help_text="Optional. A screenshot or document.",
    )

    def clean_body(self):
        return (self.cleaned_data.get("body") or "").strip()

    def clean_attachment(self):
        """Validate by content, never by filename."""
        uploaded = self.cleaned_data.get("attachment")
        if not uploaded:
            return None
        try:
            return validate_document(uploaded)
        except InvalidUpload as exc:
            raise ValidationError(str(exc))

    def clean(self):
        cleaned = super().clean()
        # A file on its own is a reply ("here is the screenshot"); nothing at
        # all is not.
        if not cleaned.get("body") and not cleaned.get("attachment") and not self.errors:
            raise ValidationError("Write a message before sending.")
        return cleaned


class AdminReplyForm(TicketReplyForm):
    """A staff reply, which may instead be an internal note."""

    is_internal = forms.BooleanField(
        required=False,
        label="Internal note",
        help_text="Only administrators can see this. The student is not notified.",
    )


class TicketFilterForm(forms.Form):
    """Non-blocking filters for the admin queue — every field is optional."""

    q = forms.CharField(required=False, label="Search")
    status = forms.ChoiceField(
        required=False, label="Status",
        choices=[("", "All statuses"), ("open", "Needs attention")] + list(TicketStatus.choices),
    )
    category = forms.ChoiceField(
        required=False, label="Category",
        choices=[("", "All categories")] + list(TicketCategory.choices),
    )
    priority = forms.ChoiceField(
        required=False, label="Priority",
        choices=[("", "Any priority")] + list(TicketPriority.choices),
    )
    days = forms.ChoiceField(
        required=False, label="Raised",
        choices=[("", "Any time"), ("1", "Last 24 hours"), ("7", "Last 7 days"), ("30", "Last 30 days")],
    )
    assignee = forms.ChoiceField(
        required=False, label="Assigned",
        choices=[("", "Anyone"), ("me", "Assigned to me"), ("unassigned", "Unassigned")],
    )
