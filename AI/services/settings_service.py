"""Operator-configurable application settings.

Business values (free-trial size, referral bonus, alert thresholds…) live in the
database so an administrator can tune them from the UI.  Secrets never do — they
stay in the environment.
"""
from __future__ import annotations

import json
from typing import Any

from django.core.cache import cache

from AI.models import AppSetting

CACHE_KEY = "app_settings:v1"
CACHE_TTL = 300

#: The assistant's standing instructions.
#:
#: Every word here is paid for on *every* request, and the answer it produces is
#: paid for at several times the input rate. So this prompt is deliberately
#: short, and most of what it does say is about length: matching the answer to
#: the question is the single biggest lever on cost per message, far bigger
#: than anything else on this screen.
#:
#: What it adds over "be brief" is what makes a short answer a *usable* one:
#: checking the result before giving it, stating an assumption instead of
#: guessing silently, and writing only what the chat can display — it renders
#: Markdown but not LaTeX, and a fraction that arrives as ``\frac{a}{b}`` is
#: noise to a student.
DEFAULT_SYSTEM_PROMPT = (
    "You are the EPC AI Assistant, a study partner for students.\n"
    "- Lead with the answer, in as few words as a complete answer needs. No "
    "preamble, recap or sign-off.\n"
    "- Be right before brief: check every calculation, unit and final answer. "
    "If information is missing, state your assumption. If the student is "
    "wrong, say so and correct it.\n"
    "- Say when unsure; never invent facts, sources, links or figures.\n"
    "- Maths and science: numbered steps, one per line, final answer in bold "
    "with units. Write maths in plain text with Unicode (x², √2, π, ≤, ×, "
    "(a+b)/c), never LaTeX.\n"
    "- Code: complete and runnable, in a fenced block with its language; for a "
    "fix, add one sentence on what was wrong.\n"
    "- Markdown renders; use lists, tables or ## headings only when they help, "
    "never nested lists.\n"
    "- Reply in the language the student wrote in.\n"
    # Without this the model answers from what it knows about itself — that it
    # is a language model and cannot draw — which is true of the model and
    # false of this product. The platform draws through a separate image
    # endpoint, so a request that reaches the model at all is one the request
    # detector did not recognise, and the useful reply is how to ask again.
    "- This platform draws pictures: never say it can't; tell the student to "
    "ask, e.g. \"create an image of a red bicycle\"."
)

# key -> (default, type, group, label, help text)
DEFAULTS: dict[str, tuple[Any, str, str, str, str]] = {
    "free_trial_tokens": (
        3000, "int", "growth", "Free trial tokens",
        "Credited once, in the default model (GPT-5 Mini), when a new account is verified. "
        "Every other model starts at zero.",
    ),
    "free_trial_enabled": (
        True, "bool", "growth", "Enable free trial",
        "Turn the welcome grant on or off for new signups.",
    ),
    "referral_bonus_referrer": (
        10000, "int", "growth", "Referral bonus — referrer",
        "Tokens awarded to the person who shared the code.",
    ),
    "referral_bonus_referred": (
        5000, "int", "growth", "Referral bonus — new user",
        "Tokens awarded to the person who signed up with a code.",
    ),
    "referral_enabled": (True, "bool", "growth", "Enable referrals", "Allow referral codes at signup."),
    "payment_khr_rate": (
        4100, "int", "payments", "KHR exchange rate",
        "Riel per US dollar, used to price KHR payments (ABA PayWay, ACLEDA). "
        "Fixed on each order when it is created, so changing it never alters an "
        "order that is already open.",
    ),
    "payment_expiry_minutes": (
        15, "int", "payments", "Payment window (minutes)",
        "How long a student has to finish paying before the order expires and a "
        "new one has to be started. The gateways are told the same limit.",
    ),
    "payment_manual_review": (
        True, "bool", "payments", "Manual confirmation fallback",
        "When a gateway has no credentials yet, offer a manual transfer that an "
        "administrator confirms from the Payments page. Turn off to hide such "
        "methods entirely.",
    ),
    "low_token_threshold": (
        50, "int", "alerts", "Low token threshold",
        "At or below this balance the chat shows the upgrade prompt and an alert "
        "is sent. Applied per model, against that model's own balance.",
    ),
    "low_token_threshold_critical": (
        10, "int", "alerts", "Critical token threshold",
        "A second, more urgent alert at this balance.",
    ),
    "low_token_percent_thresholds": (
        [50, 25, 10, 5], "json", "alerts", "Low token warning points (%)",
        "E-mail the student when a model's balance falls to each of these "
        "percentages of what they were allocated. A percentage rather than a "
        "fixed count, so the same rule means the same thing on a 10,000-token "
        "package and a 200,000-token one. Each point is sent once per model "
        "until the student tops that model up.",
    ),
    "low_token_email_enabled": (
        True, "bool", "alerts", "E-mail low token warnings",
        "Send the low-balance warning to the student's registered e-mail "
        "address, through the same account verification codes use.",
    ),
    "expiry_reminder_days": (
        [7, 3, 1], "json", "alerts", "Expiry reminder days",
        "Send subscription reminders this many days before expiry.",
    ),
    "auto_switch_model": (
        True, "bool", "ai", "Switch model when one runs out",
        "When the chosen model has no room left — no tokens, or a daily, weekly "
        "or monthly ceiling reached — answer with another model the student "
        "already holds tokens for, instead of refusing. The switch is always "
        "shown in the conversation and the reply is billed to the model that "
        "actually answered.",
    ),
    "ai_context_token_budget": (
        1200, "int", "ai", "Context budget (tokens)",
        "How much conversation history is re-sent with each message. Older "
        "messages are replaced by a short summary rather than dropped.",
    ),
    "ai_summarize_history": (
        True, "bool", "ai", "Summarise long conversations",
        "When history outgrows the budget, fold the older part into a short "
        "summary instead of losing it. Costs one small call per few messages.",
    ),
    "otp_ttl_minutes": (10, "int", "security", "OTP lifetime (minutes)", "How long a verification code stays valid."),
    "otp_length": (6, "int", "security", "OTP length", "Number of digits in a verification code."),
    "require_verification": (
        True, "bool", "security", "Require verification",
        "New students must verify by e-mail or Telegram before chatting.",
    ),
    "login_otp_students_required": (
        False, "bool", "security", "Sign-in code for every student",
        "Every student enters a one-time code after their password, whatever their own setting says. "
        "Needs working e-mail (SMTP) for students who have not connected Telegram.",
    ),
    "otp_send_everywhere": (
        True, "bool", "security", "Send codes to Telegram and e-mail",
        "Each sign-in and password-reset code goes to the student's private Telegram chat and their e-mail "
        "at the same time. Off: Telegram when connected, otherwise e-mail.",
    ),
    "single_session": (
        True, "bool", "security", "One active session",
        "Signing in on a new device signs the account out everywhere else.",
    ),
    "app_name": ("EPC AI Assistant", "str", "general", "Application name", "Shown in the UI and e-mails."),
    "support_email": ("support@epc-ai.local", "str", "general", "Support e-mail", "Where students should write."),
    "support_telegram": ("", "str", "general", "Support Telegram", "Optional @handle for support."),
    "ai_system_prompt": (
        DEFAULT_SYSTEM_PROMPT,
        "str", "ai", "System prompt", "Sets the assistant's behaviour for every conversation.",
    ),
    "ai_followups_enabled": (
        True, "bool", "ai", "Suggest follow-up questions",
        "Each answer ends with three one-tap follow-up questions. They are "
        "written in the same reply (no extra request), adding roughly 30-50 "
        "output tokens per answer.",
    ),
    "allow_chat_sharing": (
        True, "bool", "ai", "Allow sharing conversations",
        "Students can publish a read-only link to a conversation. Turning this "
        "off hides the Share button and makes every existing link stop working.",
    ),
    "custom_instructions_enabled": (
        True, "bool", "ai", "Allow custom instructions",
        "Students can tell the assistant about themselves and how they like "
        "answers written. Their text is added to every request they send.",
    ),
    "ai_max_context_messages": (
        8, "int", "ai", "Context window (messages)",
        "How many previous messages are sent with each request. Every one of them "
        "is re-sent, and paid for, on every message — raise it only if the "
        "assistant starts losing the thread.",
    ),
    "ai_vision_enabled": (True, "bool", "ai", "Enable image input", "Allow students to attach images in chat."),
    "signup_enabled": (True, "bool", "general", "Public signup", "Allow students to register themselves."),
    "maintenance_note": ("", "str", "general", "Dashboard banner", "Optional message shown on student dashboards."),
    "telegram_alerts_enabled": (
        True, "bool", "telegram", "Telegram alerts",
        "Send students' alerts to their linked Telegram chat. Off: alerts stay in the app (and e-mail); "
        "sign-in codes still go to Telegram for students who chose it.",
    ),
    "telegram_send_payment": (
        True, "bool", "telegram", "Payments",
        "Payment confirmed, failed, expired or refunded.",
    ),
    "telegram_send_tokens": (
        True, "bool", "telegram", "Tokens",
        "Low-balance warnings and tokens added by an administrator.",
    ),
    "telegram_send_expiry": (
        True, "bool", "telegram", "Plan expiry",
        "Reminders before a plan ends, and when it has ended.",
    ),
    "telegram_send_system": (
        True, "bool", "telegram", "Support and messages",
        "Support replies, messages from the team and account changes.",
    ),
    "telegram_send_referral": (
        True, "bool", "telegram", "Referral rewards",
        "Tokens earned by inviting friends.",
    ),
    "chat_streaming": (
        True, "bool", "general", "Live answers (word by word)",
        "Show answers as they are written. Off: each answer appears when it is complete.",
    ),
    "semantic_search_enabled": (
        True, "bool", "general", "Search by meaning",
        "Let students find past answers by what they mean, not only by exact words. "
        "Needs an OpenAI-compatible embeddings key (EMBEDDING_API_KEY or OPENAI_API_KEY).",
    ),
    "chat_paused": (
        False, "bool", "general", "Pause all chat (maintenance)",
        "Stops new messages to every model. Students can still read, search and export "
        "their chats, and see the note below. Administrators are not affected.",
    ),
    "chat_paused_note": (
        "", "str", "general", "Maintenance message",
        "Shown to students while chat is paused, e.g. \"Back by 9:00 pm — we're upgrading.\"",
    ),
    "admin_digest_enabled": (
        False, "bool", "general", "Daily admin digest",
        "Every morning (after 8 am Phnom Penh) each administrator gets the last 24 hours — "
        "sign-ups, payments, tokens, errors, tickets, model status — on Telegram and by e-mail. "
        "Sent by the hourly send_reminders job.",
    ),

    # -- usage windows ----------------------------------------------------
    # The one place the metering policy is decided. Per-package ceilings live
    # on the package itself; these govern the system around them.
    "minimum_limit_package": (
        50000, "int", "limits", "Minimum metered package",
        "Packages at or above this size are sold with daily, weekly and monthly "
        "ceilings. Smaller grants are governed by the balance alone.",
    ),
    "usage_warning_thresholds": (
        [70, 80, 90], "json", "limits", "Warning thresholds (%)",
        "Percentages of a window at which the student is warned. 100% always blocks.",
    ),
    "usage_meter_visible": (
        True, "bool", "limits", "Show the usage meter",
        "Display daily, weekly and monthly progress in the chat sidebar.",
    ),

    # -- support ----------------------------------------------------------
    "support_whatsapp": (
        "+923162211830", "str", "support", "WhatsApp number",
        "Full international number. Used for the Help page button and its QR code.",
    ),
    "support_whatsapp_message": (
        "Hello! I need help with my EPC AI Assistant account.",
        "str", "support", "WhatsApp opening message",
        "Pre-filled into the chat when somebody taps the button.",
    ),
    "support_hours": (
        "Every day, 9am — 9pm", "str", "support", "Support hours",
        "Shown on the Help page so nobody waits on a reply that is not coming tonight.",
    ),

    # -- image generation --------------------------------------------------
    # None of the chat models draws. These configure the dedicated endpoint that
    # does, so an administrator can point the product at a different vendor or
    # turn drawing off entirely without a deploy.
    "image_enabled": (
        True, "bool", "images", "Enable image generation",
        "Turn picture generation on or off for every student at once.",
    ),
    "image_provider": (
        "openai", "str", "images", "Image provider",
        "Which vendor draws. Must be one registered in AI.services.image_service.",
    ),
    "image_model": (
        "", "str", "images", "Image model",
        "Model id sent to the vendor, e.g. gpt-image-1. Blank uses the server default.",
    ),
    "image_api_key": (
        "", "secret", "images", "Image API key",
        "Stored server-side and never sent to a browser. Leave blank to use the "
        "key already configured in the server environment.",
    ),
    "image_size": (
        "1024x1024", "str", "images", "Default size",
        "One of 1024x1024, 1024x1536, 1536x1024, auto.",
    ),
    "image_quality": (
        "medium", "str", "images", "Quality", "low, medium or high. Higher costs more.",
    ),
    "image_token_cost": (
        0, "int", "images", "Tokens per image",
        "What one picture costs a student. 0 uses the per-quality table from the "
        "server configuration.",
    ),
}

#: Settings whose value must never be rendered back to a browser. They are
#: writable from the admin UI and readable only by server code — the settings
#: page shows whether one is set, never what it is.
SECRET_KEYS = {"image_api_key"}


def is_secret(key: str) -> bool:
    return key in SECRET_KEYS


def _cast(raw: str, value_type: str) -> Any:
    if value_type == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return 0
    if value_type == "bool":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if value_type == "json":
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return []
    return raw


def _serialise(value: Any, value_type: str) -> str:
    if value_type == "json":
        return json.dumps(value)
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


def all_settings(force: bool = False) -> dict[str, Any]:
    """Every setting merged over its default, cached for a few minutes."""
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached

    resolved = {key: spec[0] for key, spec in DEFAULTS.items()}
    try:
        for row in AppSetting.objects.all():
            spec = DEFAULTS.get(row.key)
            value_type = row.value_type or (spec[1] if spec else "str")
            resolved[row.key] = _cast(row.value, value_type)
    except Exception:  # pragma: no cover — table missing during first migrate
        pass

    cache.set(CACHE_KEY, resolved, CACHE_TTL)
    return resolved


def get(key: str, default: Any = None) -> Any:
    values = all_settings()
    if key in values:
        return values[key]
    spec = DEFAULTS.get(key)
    return spec[0] if spec else default


def set_value(key: str, value: Any, *, actor=None) -> Any:
    spec = DEFAULTS.get(key)
    value_type = spec[1] if spec else "str"
    label = spec[3] if spec else key.replace("_", " ").title()
    group = spec[2] if spec else "general"
    help_text = spec[4] if spec else ""

    AppSetting.objects.update_or_create(
        key=key,
        defaults={
            "value": _serialise(value, value_type),
            "value_type": value_type,
            "group": group,
            "label": label,
            "help_text": help_text,
            "updated_by": actor,
        },
    )
    cache.delete(CACHE_KEY)
    return value


def bootstrap() -> None:
    """Materialise defaults into the database so the settings UI has rows."""
    existing = set(AppSetting.objects.values_list("key", flat=True))
    to_create = [
        AppSetting(
            key=key,
            value=_serialise(spec[0], spec[1]),
            value_type=spec[1],
            group=spec[2],
            label=spec[3],
            help_text=spec[4],
        )
        for key, spec in DEFAULTS.items()
        if key not in existing
    ]
    if to_create:
        AppSetting.objects.bulk_create(to_create)
    cache.delete(CACHE_KEY)


def grouped() -> dict[str, list[dict[str, Any]]]:
    """Settings arranged by group for the admin settings page."""
    bootstrap()
    values = all_settings(force=True)
    out: dict[str, list[dict[str, Any]]] = {}
    for key, spec in DEFAULTS.items():
        default, value_type, group, label, help_text = spec
        value = values.get(key, default)
        secret = is_secret(key)
        out.setdefault(group, []).append(
            {
                "key": key,
                # A secret is reported as configured-or-not, never as itself.
                # Nothing downstream can leak what never reaches the template.
                "value": "" if secret else value,
                "type": value_type,
                "label": label,
                "help_text": help_text,
                "raw": "" if secret else _serialise(value, value_type),
                "is_secret": secret,
                "is_set": bool(str(value).strip()) if secret else False,
            }
        )
    return out


def secret(key: str) -> str:
    """Read a secret, falling back to the server environment.

    Server-side callers only. A value typed into the admin console wins over the
    environment, so a deployment can be re-pointed without a redeploy, but an
    empty box means "keep using the environment" rather than "unset it".
    """
    from django.conf import settings as django_settings

    value = str(get(key, "") or "").strip()
    if value:
        return value
    return str(getattr(django_settings, ENV_FALLBACK.get(key, ""), "") or "")


#: Where a blank secret falls back to, per key.
ENV_FALLBACK = {"image_api_key": "OPENAI_API_KEY"}
