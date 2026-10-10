"""Tell the assistant, in its system prompt, that this platform can draw.

A model asked for a picture answers from what it knows about itself: that it is
a language model and cannot generate images. That is true of the model and
false of the product — pictures are drawn by a separate image endpoint, which
the request detector routes to before the model is ever consulted.

So the only way a student sees "I am a text-based model and cannot create
images" is when the detector did not recognise the phrasing. Widening the
detector fixes most of those; this fixes the rest, by making the fallback reply
useful — how to ask again — instead of a denial that is not true.

Only rewritten when the stored prompt is still the previous default. An
administrator who has written their own prompt keeps it untouched; the new
guidance is theirs to add or not.
"""
from __future__ import annotations

from django.db import migrations

#: The default exactly as it stood before this migration. Compared literally,
#: so any edit at all — even a single character — counts as customised and is
#: left alone.
PREVIOUS_DEFAULT = (
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

ADDITION = (
    "\nThis platform can generate images. If a student asks for a picture and "
    "you are answering as text, do not tell them images are impossible — tell "
    'them to ask for it directly, for example "create an image of a red '
    'bicycle", and their request will be drawn.'
)


def forwards(apps, schema_editor):
    AppSetting = apps.get_model("AI", "AppSetting")
    row = AppSetting.objects.filter(key="ai_system_prompt").first()
    if row is None:
        return
    if row.value.strip() != PREVIOUS_DEFAULT.strip():
        return                      # customised; not ours to rewrite
    row.value = PREVIOUS_DEFAULT + ADDITION
    row.save(update_fields=["value"])


def backwards(apps, schema_editor):
    AppSetting = apps.get_model("AI", "AppSetting")
    row = AppSetting.objects.filter(key="ai_system_prompt").first()
    if row is None:
        return
    if row.value.strip() != (PREVIOUS_DEFAULT + ADDITION).strip():
        return
    row.value = PREVIOUS_DEFAULT
    row.save(update_fields=["value"])


class Migration(migrations.Migration):

    dependencies = [("AI", "0030_add_fable_and_opus")]

    operations = [migrations.RunPython(forwards, backwards)]
