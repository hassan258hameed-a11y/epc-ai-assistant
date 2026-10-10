"""What an administrator does *to* students, one or many at a time.

Messages, tags and notes. Kept out of the views so the single-student page and
the bulk bar on the students list do exactly the same thing — one audit
format, one notification shape, one set of limits.
"""
from __future__ import annotations

import re

from django.db import transaction

from AI.models import AuditAction, EmailType, NotificationLevel, StudentNote, User
from AI.services import audit_service, mail_service, notification_service

MAX_TAGS = 12
TAG_MAX = 32
NOTE_MAX = 2000
TITLE_MAX, BODY_MAX = 140, 2000


class AdminToolError(Exception):
    def __init__(self, message: str):
        self.friendly_message = message
        super().__init__(message)


# -- messages -----------------------------------------------------------------------
def message_students(
    admin, students, *, title: str, body: str, level: str = NotificationLevel.INFO,
    email: bool = False, request=None,
) -> dict:
    """An in-app notification to each student, and optionally an e-mail."""
    title = " ".join((title or "").split())[:TITLE_MAX]
    body = (body or "").strip()[:BODY_MAX]
    if not title:
        raise AdminToolError("Give the message a title.")
    if not body:
        raise AdminToolError("Write the message.")
    if level not in NotificationLevel.values:
        level = NotificationLevel.INFO

    students = list(students)
    mailed = 0
    for student in students:
        notification_service.notify(student, title, body, level=level, telegram=True)
        if email:
            log = mail_service.send_notice(
                student, email_type=EmailType.OTHER, subject=title, heading=title,
                intro="here is a message from the team.", body=body,
                button_label="Open the app", button_path="/chat/",
                reason="because the team sent you a message.", trigger="admin message",
            )
            mailed += log.status == "sent"
    audit_service.record(
        AuditAction.USER_UPDATE, actor=admin, request=request,
        target=students[0] if len(students) == 1 else None,
        description=f"Sent “{title}” to {len(students)} student{'s' if len(students) != 1 else ''}"
                    + (f", {mailed} by e-mail" if email else ""),
    )
    return {"sent": len(students), "mailed": mailed}


# -- tags ------------------------------------------------------------------------------
def clean_tag(raw: str) -> str:
    tag = re.sub(r"\s+", " ", (raw or "").strip().lower())
    tag = re.sub(r"[^\w\s:./+-]", "", tag)
    return tag[:TAG_MAX].strip()


def all_tags() -> list[str]:
    """Every tag in use, for suggestions — a small table, read whole."""
    seen: set[str] = set()
    for tags in User.objects.exclude(tags=[]).values_list("tags", flat=True):
        seen.update(tags or [])
    return sorted(seen)


def tag_students(admin, students, *, add: str = "", remove: str = "", request=None) -> int:
    add_tag, remove_tag = clean_tag(add), clean_tag(remove)
    if not add_tag and not remove_tag:
        raise AdminToolError("Enter a tag.")
    changed = 0
    with transaction.atomic():
        for student in students:
            tags = [t for t in (student.tags or []) if t]
            before = list(tags)
            if remove_tag and remove_tag in tags:
                tags.remove(remove_tag)
            if add_tag and add_tag not in tags:
                if len(tags) >= MAX_TAGS:
                    continue
                tags.append(add_tag)
            if tags != before:
                User.objects.filter(pk=student.pk).update(tags=tags)
                student.tags = tags
                changed += 1
    verb = f"tagged “{add_tag}”" if add_tag else f"removed tag “{remove_tag}” from"
    audit_service.record(AuditAction.USER_UPDATE, actor=admin, request=request,
                         description=f"{verb.capitalize()} {changed} student{'s' if changed != 1 else ''}")
    return changed


# -- notes -------------------------------------------------------------------------------
def add_note(admin, student, body: str) -> StudentNote:
    body = (body or "").strip()
    if not body:
        raise AdminToolError("Write the note first.")
    return StudentNote.objects.create(student=student, author=admin, body=body[:NOTE_MAX])


def toggle_pin(note: StudentNote) -> StudentNote:
    note.is_pinned = not note.is_pinned
    note.save(update_fields=["is_pinned"])
    return note
