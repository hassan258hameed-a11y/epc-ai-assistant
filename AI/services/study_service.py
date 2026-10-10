"""Study tools: flashcards and quizzes from a conversation, and a daily goal.

A set is written by the model the student chose and billed like any reply
(through ``chat_service.run_tool``), then kept — revising it again costs
nothing. The model is asked for strict JSON and every item is checked before
anything is saved or charged: a quiz question with no right answer, or with the
right answer pointing at an option that does not exist, is worse than none.
"""
from __future__ import annotations

import json
import random
import re
from datetime import timedelta

from django.db.models.functions import TruncDate
from django.utils import timezone

from AI.models import AIUsageLog, Conversation, MessageRole, MessageStatus, StudyKind, StudySet

COUNTS = (5, 8, 12)
DEFAULT_COUNT = 8
#: How much of the conversation the model reads: the newest part, which is
#: what the student was just working on.
SOURCE_CHARS = 9000
MAX_SETS = 200

FRONT_MAX, BACK_MAX = 160, 360
QUESTION_MAX, OPTION_MAX, WHY_MAX = 240, 160, 300

SYSTEM = {
    StudyKind.FLASHCARDS: (
        "You turn a tutoring conversation into study flashcards. Reply with JSON only, no prose and no "
        "code fence: {{\"title\": \"...\", \"cards\": [{{\"front\": \"...\", \"back\": \"...\"}}]}} with "
        "exactly {count} cards. Front: a question or term, under 20 words. Back: the answer in one or two "
        "sentences. Cover the key facts, definitions, formulas and steps from the conversation, never "
        "the conversation itself. No duplicates. Plain text: maths with Unicode symbols (x², √2, π), "
        "never LaTeX or Markdown. Write in the language the conversation uses. The title names the "
        "topic in under 8 words."
    ),
    StudyKind.QUIZ: (
        "You write a multiple-choice quiz from a tutoring conversation. Reply with JSON only, no prose "
        "and no code fence: {{\"title\": \"...\", \"questions\": [{{\"question\": \"...\", \"options\": "
        "[\"...\", \"...\", \"...\", \"...\"], \"answer\": 0, \"why\": \"...\"}}]}} with exactly {count} "
        "questions. Each has exactly 4 options and exactly one correct option; \"answer\" is the 0-based "
        "index of the correct option, and its position varies between questions. Wrong options must be "
        "plausible. \"why\" explains the right answer in one sentence. Test understanding of the ideas, "
        "not memory of the wording. Check every answer before replying. Plain text: maths with Unicode "
        "symbols (x², √2, π), never LaTeX or Markdown. Write in the language the conversation uses. The "
        "title names the topic in under 8 words."
    ),
}


class StudyError(Exception):
    def __init__(self, message: str, code: str = "study"):
        self.friendly_message = message
        self.code = code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Making a set
# ---------------------------------------------------------------------------
def source_text(conversation: Conversation) -> str:
    """The conversation as the model should read it: newest last, trimmed from the front."""
    rows = list(
        conversation.messages.filter(status=MessageStatus.OK, role__in=[MessageRole.USER, MessageRole.ASSISTANT])
        .order_by("-created_at", "-id").values_list("role", "content")[:60]
    )
    parts, used = [], 0
    for role, content in rows:
        text = (content or "").strip()
        if not text:
            continue
        chunk = f"{'Student' if role == MessageRole.USER else 'Tutor'}: {text}"
        if used + len(chunk) > SOURCE_CHARS:
            chunk = chunk[: max(0, SOURCE_CHARS - used)]
            if chunk:
                parts.append(chunk)
            break
        parts.append(chunk)
        used += len(chunk)
    return "\n\n".join(reversed(parts))


def _clean(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def _load_json(text: str):
    """The JSON object in a reply, tolerating a stray code fence or preamble."""
    text = (text or "").strip()
    text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def parse(kind: str, text: str, count: int) -> tuple[str, list[dict]]:
    """``(title, items)`` from a reply, or ``ValueError`` if nothing usable."""
    try:
        data = _load_json(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"unreadable JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    title = _clean(data.get("title"), 120)

    items: list[dict] = []
    seen: set[str] = set()
    if kind == StudyKind.FLASHCARDS:
        for card in data.get("cards") or []:
            if not isinstance(card, dict):
                continue
            front, back = _clean(card.get("front"), FRONT_MAX), _clean(card.get("back"), BACK_MAX)
            if not front or not back or front.lower() in seen:
                continue
            seen.add(front.lower())
            items.append({"front": front, "back": back})
    else:
        for row in data.get("questions") or []:
            if not isinstance(row, dict):
                continue
            question = _clean(row.get("question"), QUESTION_MAX)
            options = [_clean(o, OPTION_MAX) for o in (row.get("options") or []) if _clean(o, OPTION_MAX)]
            answer = row.get("answer")
            # Tolerate "B" or the option's own text as well as an index.
            if isinstance(answer, str) and not answer.strip().isdigit():
                letter = answer.strip().upper()
                if len(letter) == 1 and "A" <= letter <= "Z":
                    answer = ord(letter) - ord("A")
                elif _clean(answer, OPTION_MAX) in options:
                    answer = options.index(_clean(answer, OPTION_MAX))
            try:
                answer = int(answer)
            except (TypeError, ValueError):
                continue
            if (not question or question.lower() in seen or not 2 <= len(options) <= 6
                    or len(set(o.lower() for o in options)) != len(options) or not 0 <= answer < len(options)):
                continue
            seen.add(question.lower())
            items.append({"question": question, "options": options, "answer": answer,
                          "why": _clean(row.get("why"), WHY_MAX)})

    if not items:
        raise ValueError("no valid items")
    return title, items[:count]


def generate(user, conversation: Conversation, kind: str, *, count: int = DEFAULT_COUNT, model_key: str = "") -> StudySet:
    """Write, check, bill and save one set from ``conversation``."""
    from AI.services import chat_service

    if kind not in StudyKind.values:
        raise StudyError("Choose flashcards or a quiz.", code="bad_kind")
    if conversation.user_id != user.pk:
        raise StudyError("That conversation is not available.", code="not_found")
    count = count if count in COUNTS else DEFAULT_COUNT
    if StudySet.objects.filter(user=user).count() >= MAX_SETS:
        raise StudyError(f"You have {MAX_SETS} study sets. Delete a few old ones to make more.", code="too_many")

    source = source_text(conversation)
    if len(source) < 80 or not conversation.messages.filter(role=MessageRole.ASSISTANT, status=MessageStatus.OK).exists():
        raise StudyError("Chat about the topic a little first — there is not enough here to study from yet.",
                         code="too_short")

    if not model_key:
        last = conversation.messages.filter(role=MessageRole.ASSISTANT).exclude(model_key="").order_by("-id").first()
        model_key = last.model_key if last else ""

    (title, items), result, charged, ai_model = chat_service.run_tool(
        user, conversation,
        prompt=f"Conversation:\n\n{source}",
        system_prompt=SYSTEM[kind].format(count=count),
        model_key=model_key,
        max_output=2600 if kind == StudyKind.QUIZ else 2000,
        label="Flashcards" if kind == StudyKind.FLASHCARDS else "Quiz",
        accept=lambda text: parse(kind, text, count),
    )
    return StudySet.objects.create(
        user=user, conversation=conversation, kind=kind,
        title=title or conversation.title[:120] or "Study set",
        items=items, model_key=ai_model.key, tokens_used=charged,
    )


# ---------------------------------------------------------------------------
# Using a set
# ---------------------------------------------------------------------------
def sets_for(user):
    return StudySet.objects.filter(user=user).select_related("conversation")


def record_attempt(study_set: StudySet, score: int) -> StudySet:
    """A finished pass: quizzes keep their best score, both keep when."""
    study_set.attempts += 1
    study_set.last_studied_at = timezone.now()
    fields = ["attempts", "last_studied_at"]
    if study_set.kind == StudyKind.QUIZ:
        score = max(0, min(100, int(score)))
        if study_set.best_score is None or score > study_set.best_score:
            study_set.best_score = score
            fields.append("best_score")
    study_set.save(update_fields=fields)
    return study_set


def payload(study_set: StudySet, *, shuffle_options: bool = False) -> dict:
    items = study_set.items or []
    if shuffle_options and study_set.kind == StudyKind.QUIZ:
        # Options move between attempts so a pass tests the idea, not the
        # position. The right answer is re-pointed to wherever it lands.
        shuffled = []
        for row in items:
            order = list(range(len(row["options"])))
            random.shuffle(order)
            shuffled.append({**row, "options": [row["options"][i] for i in order],
                             "answer": order.index(row["answer"])})
        items = shuffled
    return {
        "id": study_set.pk,
        "kind": study_set.kind,
        "title": study_set.title,
        "items": items,
        "size": len(study_set.items or []),
        "best_score": study_set.best_score,
        "attempts": study_set.attempts,
        "conversation_id": study_set.conversation_id,
        "model": study_set.model_key,
        "tokens": study_set.tokens_used,
    }


# ---------------------------------------------------------------------------
# The daily goal and the streak
# ---------------------------------------------------------------------------
def _active_days(user, days: int = 120) -> set:
    since = timezone.now() - timedelta(days=days)
    return set(
        AIUsageLog.objects.filter(user=user, success=True, created_at__gte=since)
        .annotate(day=TruncDate("created_at", tzinfo=timezone.get_current_timezone()))
        .values_list("day", flat=True).distinct()
    )


def streak(user) -> int:
    """Days in a row with at least one answer, ending today — or yesterday,
    so the streak is not shown as broken before today's first question."""
    days = _active_days(user)
    today = timezone.localdate()
    cursor = today if today in days else today - timedelta(days=1)
    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def today(user, *, goal: int = 0) -> dict:
    """Answers received today against the student's goal, plus the streak.

    Counted from the usage log rather than from messages, so deleting a chat
    does not take back the work that went into it.
    """
    start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    asked = AIUsageLog.objects.filter(user=user, success=True, created_at__gte=start).count()
    goal = max(0, int(goal or 0))
    return {
        "asked": asked,
        "goal": goal,
        "percent": min(100, round(asked * 100 / goal)) if goal else 0,
        "met": bool(goal) and asked >= goal,
        "remaining": max(0, goal - asked),
        "streak": streak(user),
    }
