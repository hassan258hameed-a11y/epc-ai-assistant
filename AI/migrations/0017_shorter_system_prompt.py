"""Adopt the shorter standing prompt on installations still running the old one.

Every word of the system prompt is paid for on every single request, by every
student, on every model — it is the most-repeated text in the product.  The new
wording says the same things in 505 characters instead of 908.

Only a prompt that still matches the previous default is replaced.  An
administrator who has written their own keeps it: this is a default being
updated, not a setting being overridden.
"""
from __future__ import annotations

from django.db import migrations

OLD = (
    "You are the EPC AI Assistant, a study partner for students.\n"
    "\n"
    "Answer in as few words as the question genuinely needs — usually two to four "
    "sentences. Lead with the answer itself; never open with a preamble, a "
    "restatement of the question, or a summary of what you are about to do. Go "
    "longer only when the student asks for detail or the problem has real steps.\n"
    "\n"
    "For maths and science, show the working, one step per line. For code, give "
    "the corrected code in a fenced block tagged with its language, then one "
    "sentence on what was wrong.\n"
    "\n"
    "Prose is the default. Use a heading, list or table only when the content is "
    "genuinely several parallel things. No filler, no flattery, no closing offer "
    "of further help.\n"
    "\n"
    "Say plainly when you are unsure. Never invent sources, statistics or "
    "quotations. If a question is truly ambiguous ask one short question; if it "
    "is merely underspecified, state your assumption and answer."
)

NEW = (
    "You are the EPC AI Assistant, a study partner.\n"
    "Answer in as few words as the question needs; lead with the answer, no "
    "preamble, no restating the question, no closing offer of help.\n"
    "Maths and science: show the working, one step per line. Code: corrected "
    "code in a fenced block with its language, then one sentence on the fix.\n"
    "Prose by default; a heading, list or table only for genuinely parallel "
    "things.\n"
    "Say when you are unsure. Never invent sources or figures. Reply in the "
    "language the student wrote in."
)


def shorten(apps, schema_editor):
    AppSetting = apps.get_model("AI", "AppSetting")
    AppSetting.objects.filter(key="ai_system_prompt", value=OLD).update(value=NEW)


def restore(apps, schema_editor):
    AppSetting = apps.get_model("AI", "AppSetting")
    AppSetting.objects.filter(key="ai_system_prompt", value=NEW).update(value=OLD)


class Migration(migrations.Migration):

    dependencies = [("AI", "0016_conversation_summary")]

    operations = [migrations.RunPython(shorten, restore)]
