"""A student's standing instructions: about them, and how they like answers.

Added to the system prompt for every reply they ask for, *after* the platform's
own prompt and marked as the student's preferences, so the platform's rules
still come first — a preference can change the tone and the length of an
answer, not what the assistant is allowed to do.
"""
from __future__ import annotations

from AI.models import ChatPreference
from AI.services import settings_service

MAX_ABOUT = 600
MAX_INSTRUCTIONS = 900

STYLE_TEXT = {
    ChatPreference.Style.CONCISE: (
        "Keep answers short: lead with the answer, then at most a few sentences "
        "or bullet points. No preamble and no recap."
    ),
    ChatPreference.Style.DETAILED: (
        "Give thorough, well-structured answers: explain the reasoning, include "
        "an example where it helps, and use headings for longer answers."
    ),
    ChatPreference.Style.STEP_BY_STEP: (
        "Explain step by step, numbering each step, so the student can follow "
        "the reasoning and repeat it on their own."
    ),
}


class PreferenceError(Exception):
    def __init__(self, message: str):
        self.friendly_message = message
        super().__init__(message)


def enabled() -> bool:
    return bool(settings_service.get("custom_instructions_enabled", True))


def get(user) -> ChatPreference:
    """The student's preferences, or an unsaved blank set."""
    return ChatPreference.objects.filter(user=user).first() or ChatPreference(user=user)


def save(user, *, about_me: str, instructions: str, style: str, language: str, enabled: bool) -> ChatPreference:
    about_me = (about_me or "").strip()
    instructions = (instructions or "").strip()
    language = " ".join((language or "").split())[:40]
    if len(about_me) > MAX_ABOUT:
        raise PreferenceError(f"Keep “about you” under {MAX_ABOUT} characters.")
    if len(instructions) > MAX_INSTRUCTIONS:
        raise PreferenceError(f"Keep the instructions under {MAX_INSTRUCTIONS} characters.")
    if style not in ChatPreference.Style.values:
        style = ChatPreference.Style.BALANCED
    preference, _ = ChatPreference.objects.update_or_create(
        user=user,
        defaults={
            "about_me": about_me, "instructions": instructions, "style": style,
            "language": language, "enabled": bool(enabled),
        },
    )
    return preference


def save_defaults(user, *, default_model_key: str, daily_goal: int) -> ChatPreference:
    """The model new chats start on, and the daily goal. Neither changes a prompt."""
    from AI.services import wallet_service

    key = (default_model_key or "").strip()
    if key and not any(
        row["model"].key == key and row["model"].is_active and row["usable"]
        for row in wallet_service.overview(user)
    ):
        raise PreferenceError("That model is not available on your account.")
    if not 0 <= int(daily_goal) <= 200:
        raise PreferenceError("Choose a daily goal between 0 and 200 answers.")
    preference, _ = ChatPreference.objects.update_or_create(
        user=user, defaults={"default_model_key": key, "daily_goal": int(daily_goal)},
    )
    return preference


def system_prompt_for(user, base: str, conversation=None) -> str:
    """The platform prompt, then the student's preferences, then the folder's.

    Folder instructions belong to the conversation, so they apply whether or
    not personal preferences are switched on.
    """
    from AI.services import organise_service

    prompt = _with_preferences(user, base)
    folder_text = organise_service.folder_instructions_for(conversation)
    if folder_text:
        prompt = (
            f"{prompt}\n\nThis conversation is in a folder the student set up, with "
            "these instructions (they may change length, tone and format; the "
            f"rules on accuracy and honesty above still take priority):\n{folder_text}"
        )
    return prompt


def _with_preferences(user, base: str) -> str:
    if not enabled() or user is None:
        return base
    preference = ChatPreference.objects.filter(user=user, enabled=True).first()
    if preference is None or preference.is_blank:
        return base

    lines = []
    style = STYLE_TEXT.get(preference.style)
    if style:
        lines.append(f"- Answer style: {style}")
    if preference.language:
        lines.append(f"- Always answer in {preference.language}, unless asked for another language.")
    if preference.about_me:
        lines.append(f"- About the student, in their words: {preference.about_me}")
    if preference.instructions:
        lines.append(f"- How the student wants answers, in their words: {preference.instructions}")
    if not lines:
        return base

    # Their style wins over the default length and layout — a student who asked
    # for detailed, step-by-step answers must not get the default's one-liner
    # because "as few words as possible" was written first. What never yields
    # is accuracy and honesty.
    return (
        f"{base}\n\n"
        "The student has set these preferences. They override the default length "
        "and format above; the rules on accuracy and honesty above always take "
        "priority:\n" + "\n".join(lines)
    )
