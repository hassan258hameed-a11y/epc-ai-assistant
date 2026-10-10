"""Image generation.

None of the three chat models in the catalogue draws pictures.  Telling a
student "I am a text-based AI assistant" when they ask for one is not an honest
description of the *product* — the platform can draw, just not with that model —
so a request for a picture is recognised in the chat runtime and routed here, to
a dedicated image endpoint, and the picture comes back into the same
conversation.

Two things are deliberately kept out of the chat provider layer:

* **the wire format** — image endpoints take a prompt and return bytes, not a
  turn list and a usage block, so this shares nothing with
  :mod:`AI.services.providers`;
* **the billing** — images are priced per picture, not per token, so the charge
  is a fixed conversion into the one currency students actually see.

One vendor serves this whatever model the student is talking to, so the balance
that pays is the model they are *using* — see
:func:`AI.services.chat_service.companion_model`.  Someone working in Claude can
draw without first being sent to find GPT tokens; the ledger entry names both
the service and the wallet, so the two are never confused in the accounts.

Everything configurable here is read from the database first and the server
environment second, so an administrator can re-point the vendor, change the
model, resize, re-price or switch drawing off entirely without a deploy.  The
credential is the exception in one direction only: it can be *set* from the
console but is never rendered back to one.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

from AI.services import imaging, settings_service
from AI.services.imaging import GeneratedImage
from AI.services.providers.base import AIError

logger = logging.getLogger("AI.image")

#: Re-exported so callers keep reading these from one place, even though it is
#: the vendors that now own the wire format behind them.
VALID_SIZES = set(imaging.VALID_SIZES)
VALID_QUALITIES = imaging.VALID_QUALITIES


# ---------------------------------------------------------------------------
# Intent
#
# Getting this wrong is expensive in both directions. Drawing a picture nobody
# asked for spends a student's tokens on something they did not want; refusing
# to draw one they did ask for is the bug this whole module exists to fix. So
# the decision is made in three tiers of decreasing certainty, and the last one
# is fenced in by everything that reliably means "this is not a picture".
# ---------------------------------------------------------------------------

#: Verbs that ask for something to be *made*.
_MAKE = (
    r"(?:generate|create|make|draw|paint|sketch|illustrate|render|design|"
    r"produce|imagine|visuali[sz]e)"
)

#: Nouns that mean "a picture".
_NOUN = (
    r"(?:image|images|picture|pictures|photo|photos|photograph|artwork|"
    r"illustration|illustrations|drawing|drawings|painting|poster|logo|"
    r"wallpaper|icon|graphic|sketch|render|portrait|avatar|thumbnail|"
    r"banner|mockup|scene|comic|cartoon)"
)

#: A generation verb close to a picture noun. The gap is capped at a few words
#: so "explain how to create a program that compresses an image" does not match
#: — there, the verb belongs to "program", not to "image".
_ASK = re.compile(
    rf"\b{_MAKE}\b(?:\s+(?:me|us|a|an|the|some|new|another|nice|good|cool|quick|simple|"
    rf"detailed|realistic|high[- ]quality|beautiful|\d+)){{0,4}}\s+{_NOUN}\b",
    re.IGNORECASE,
)

#: "draw a cat" — the verb alone is enough when it can only mean drawing.
_ASK_BARE = re.compile(
    r"^\s*(?:please\s+)?(?:draw|sketch|paint|illustrate|render)\s+(?:me\s+)?(?:a|an|the|some)\s+\S+",
    re.IGNORECASE,
)

#: A picture noun followed by what it is *of*. This is the half of the language
#: that has no creation verb at all — "a photo of a cat", "show me a picture of
#: Paris", "I want an image of a lion". Nine of the twenty-five ways people
#: were observed asking for a picture look like this, and every one of them used
#: to fall through to the chat model, which then apologised that it was
#: text-only — a thing that is not true of this product.
#:
#: Safe because it demands the noun be followed by "of" (or an equivalent) and
#: then a subject: "picture of X" is a request for one. Questions are excluded
#: separately, which is what keeps "is this a picture of a cat?" out.
_ASK_NOUN_OF = re.compile(
    rf"^\s*(?:please\s+)?"
    rf"(?:(?:can|could|would|will|may)\s+)?"
    rf"(?:(?:i|you)\s+)?"
    rf"(?:(?:please|just)\s+)?"
    rf"(?:(?:show|give|send|get|make|want|need|like|share|have)\s+)?"
    rf"(?:(?:me|us)\s+)?"
    rf"(?:(?:a|an|the|some|another)\s+)?"
    rf"(?:\w+\s+){{0,2}}?"
    rf"{_NOUN}\s+(?:of|showing|depicting|featuring)\s+\S+",
    re.IGNORECASE,
)

#: Questions *about* images rather than requests *for* one. Checked first, so
#: "how are images generated?" is answered by the chat model, as it should be.
_ABOUT = re.compile(
    r"\b(?:how|why|what|when|where|which|who|whether|explain|describe|compare|"
    r"difference|meaning|define|definition|tell me about|can you tell)\b",
    re.IGNORECASE,
)

#: The explicit escape hatch, for when the heuristic is not sure enough.
_SLASH = re.compile(r"^\s*/(?:image|img|draw|imagine)\b[:\s]*", re.IGNORECASE)

#: "Create a …" opening the message, capturing what is to be created. Only ever
#: consulted at the start of a short message: an instruction buried in the
#: middle of a paragraph is almost always about something else.
_MAKE_SUBJECT = re.compile(
    rf"^\s*(?:please\s+)?(?:can|could|would|will)?\s*(?:you\s+)?(?:please\s+)?{_MAKE}\s+"
    r"(?:me\s+|us\s+|for\s+me\s+)?(?:a|an|the|some|two|three|\d+)?\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)

#: Things that are made but are *not* pictures. This is the whole safety net
#: for the third tier: "create a monkey eating a banana" is a drawing, "create a
#: function that reverses a string" is not, and the only difference a machine
#: can see cheaply is the noun. Listed as head words, matched anywhere in the
#: subject, because "a Python function that draws a chart" must not be drawn.
_NOT_A_PICTURE = {
    # software
    "account", "algorithm", "api", "app", "application", "array", "backend",
    "branch", "bug", "build", "class", "cli", "code", "command", "commit",
    "component", "config", "configuration", "container", "controller", "css",
    "dashboard", "database", "dataset", "dependency", "deployment", "dict",
    "dictionary", "docker", "endpoint", "environment", "exception", "feature",
    "file", "folder", "form", "framework", "function", "git", "graph", "hook",
    "html", "index", "installer", "instance", "interface", "javascript", "job",
    "json", "key", "library", "loop", "method", "middleware", "migration",
    "model", "module", "mysql", "network", "object", "package", "page",
    "password", "patch", "pipeline", "plugin", "program", "project",
    "query", "regex", "repo", "repository", "request", "response", "route",
    "schema", "script", "server", "service", "session", "setup", "sheet",
    "site", "software", "solution", "spreadsheet", "sql", "string", "struct",
    "system", "table", "template", "test", "thread", "token", "type",
    "user", "variable", "view", "website", "workflow", "xml", "yaml",
    # writing and planning
    "abstract", "agenda", "analysis", "answer", "argument", "article",
    "assignment", "bibliography", "blog", "brief", "budget", "caption",
    "checklist", "conclusion", "cv", "definition", "description", "draft",
    "email", "essay", "example", "excuse", "exercise", "explanation",
    "flashcard", "formula", "guide", "headline", "introduction", "itinerary",
    "joke", "letter", "list", "meal", "memo", "message", "note", "notes",
    "outline", "paragraph", "paper", "plan", "playlist", "poem", "post",
    "presentation", "proof", "proposal", "question", "questions", "quiz",
    "recipe", "reply", "report", "resume", "review", "revision", "routine",
    "rubric", "schedule", "sentence", "song", "speech", "story", "strategy",
    "study", "summary", "syllabus", "test", "text", "thesis", "timetable",
    "tweet", "workout",
}

#: Words that make a picture near-certain even next to an ambiguous noun.
_VISUAL_HINT = re.compile(
    r"\b(?:realistic|photorealistic|cartoon|anime|3d|watercolou?r|oil painting|"
    r"pixel art|digital art|concept art|cinematic|sunset|sunrise|landscape|"
    r"portrait|wallpaper|background|logo|sticker|studio ghibli|van gogh|"
    r"in the style of|hyper[- ]?realistic|4k|8k)\b",
    re.IGNORECASE,
)

#: Beyond this a message is prose, not a prompt. An image request is short by
#: nature; a long one is nearly always a question that happens to contain a verb.
_MAX_PROMPT_CHARS = 240


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def config() -> dict:
    """The live image settings: database first, server environment second.

    Read on every call rather than cached in a module global, because an
    administrator changing the vendor or the model expects the next picture to
    use it, not the next restart.
    """
    slug = str(settings_service.get("image_provider", imaging.DEFAULT_SLUG) or "").strip().lower()
    try:
        provider_class = imaging.get_class(slug)
    except imaging.UnknownImageProvider:
        # A slug nobody implements would otherwise take drawing down silently.
        logger.warning("Unknown image provider %r configured; falling back", slug[:32])
        provider_class = imaging.get_class(imaging.DEFAULT_SLUG)

    model = str(settings_service.get("image_model", "") or "").strip()
    size = str(settings_service.get("image_size", "") or "").strip()
    quality = str(settings_service.get("image_quality", "") or "").strip().lower()

    # The credential can be set in the console; failing that it comes from the
    # environment variable this vendor reads. Setting one per vendor is what
    # lets an operator switch between them without retyping a key each time.
    key = settings_service.secret("image_api_key")
    if not key:
        key = str(getattr(settings, provider_class.env_key, "") or "")

    return {
        "provider": provider_class.slug,
        "provider_name": provider_class.name,
        "enabled": bool(settings_service.get("image_enabled", True))
        and bool(getattr(settings, "AI_IMAGE_ENABLED", True)),
        "model": model or getattr(settings, "AI_IMAGE_MODEL", "") or provider_class.default_model,
        "size": size if size in VALID_SIZES else (getattr(settings, "AI_IMAGE_SIZE", "") or "1024x1024"),
        "quality": quality if quality in VALID_QUALITIES else (getattr(settings, "AI_IMAGE_QUALITY", "") or "medium"),
        "api_key": key,
        "base_url": str(getattr(settings, provider_class.env_base, "") or ""),
        "timeout": int(getattr(settings, "AI_IMAGE_TIMEOUT", 180)),
        "supports_edit": provider_class.supports_edit,
        "supports_quality": provider_class.supports_quality,
    }


def provider():
    """The configured vendor, ready to call."""
    cfg = config()
    return imaging.build(
        cfg["provider"],
        imaging.ImageConfig(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            size=cfg["size"],
            quality=cfg["quality"],
            timeout=cfg["timeout"],
        ),
    )


def is_available() -> bool:
    """Whether an image request can be served at all, right now."""
    cfg = config()
    return bool(cfg["enabled"] and cfg["api_key"])


def is_configured() -> bool:
    """Credentials present, regardless of whether drawing is switched on."""
    return bool(config()["api_key"])


def can_edit() -> bool:
    """Whether the configured vendor can change an existing picture."""
    cfg = config()
    return bool(cfg["enabled"] and cfg["api_key"] and cfg["supports_edit"])


def status() -> dict:
    """A connection summary for the admin console. Never contains the key."""
    cfg = config()
    if not cfg["api_key"]:
        state, tone, detail = "Not connected", "danger", "No API key has been set."
    elif not cfg["enabled"]:
        state, tone, detail = "Switched off", "neutral", "Configured, but disabled for students."
    else:
        state, tone, detail = "Connected", "success", f"Drawing with {cfg['model']}."
    return {
        "state": state,
        "tone": tone,
        "detail": detail,
        "provider": cfg["provider_name"],
        "provider_slug": cfg["provider"],
        "model": cfg["model"],
        "size": cfg["size"],
        "quality": cfg["quality"],
        "enabled": cfg["enabled"],
        "has_key": bool(cfg["api_key"]),
        "supports_edit": cfg["supports_edit"],
        "catalogue": imaging.catalogue(),
    }


def token_cost(quality: str = "") -> int:
    """What one picture costs the student, in platform tokens.

    An administrator can set one flat price; leaving it at zero falls back to
    the per-quality table, so a deployment that turns the quality down pays the
    vendor less and charges the student less rather than a flat rate for
    whatever they happen to get.
    """
    override = int(settings_service.get("image_token_cost", 0) or 0)
    if override > 0:
        return override
    quality = (quality or config()["quality"] or "medium").strip().lower()
    table = getattr(settings, "AI_IMAGE_TOKENS_BY_QUALITY", {})
    return max(1, int(table.get(quality, getattr(settings, "AI_IMAGE_TOKENS_PER_IMAGE", 1200))))



# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def strip_command(text: str) -> str:
    """Remove a leading ``/image`` so the prompt is only the description."""
    return _SLASH.sub("", text or "").strip()


def is_explicit_command(text: str) -> bool:
    return bool(_SLASH.match(text or ""))


def _subject_of(text: str) -> str:
    match = _MAKE_SUBJECT.match(text.strip())
    return (match.group(1) if match else "").strip()


def _subject_is_text_artifact(subject: str) -> bool:
    """Whether what is being asked for is a document, not a drawing."""
    words = re.findall(r"[a-z]+", subject.lower())
    return any(word in _NOT_A_PICTURE for word in words)


def looks_like_image_request(text: str) -> bool:
    """Whether this message is asking for a picture to be drawn.

    Three tiers, most certain first:

    1. an explicit ``/image`` command;
    2. a generation verb sitting next to a picture noun — "make an image of a
       stadium", "draw a cat";
    3. a picture noun and the thing it is of, with no verb at all — "a photo of
       a cat", "show me a picture of Paris";
    4. a short, imperative "create/generate/make a <something>" whose something
       is not one of the many things that get made and are not pictures.

    Tier four is what makes "Create a monkey eating a banana" work, which is
    how people actually ask. It is fenced in hard: the message must be short,
    must not open with a question word, must not contain code, and its subject
    must not name a document, a program or a plan.
    """
    text = (text or "").strip()
    if not text:
        return False
    if is_explicit_command(text):
        return True
    # Code in the message means the conversation is about code.
    if "```" in text:
        return False
    # "How do I generate an image with Python?" — a question, not an order.
    if _ABOUT.match(text):
        return False
    if _ASK.search(text) or _ASK_BARE.match(text):
        return True
    # "a photo of a cat" — a picture noun and its subject, with no verb asking
    # for it. A question mark turns the same words into a discussion of a
    # picture rather than a request for one ("is this a picture of a cat?"),
    # so this tier declines to guess when one is present.
    if "?" not in text and len(text) <= _MAX_PROMPT_CHARS and _ASK_NOUN_OF.match(text):
        return True

    if len(text) > _MAX_PROMPT_CHARS:
        return False
    subject = _subject_of(text)
    if not subject:
        return False
    # A question mark anywhere turns "create a monkey" into a discussion of
    # whether one could be created.
    if "?" in text:
        return False
    if _subject_is_text_artifact(subject):
        return False
    if _VISUAL_HINT.search(subject):
        return True
    # A bare subject of one or two words is more likely a command than a
    # description of a picture — "create an account", "make a note" are caught
    # above, but this keeps the tier honest about how much it is guessing.
    return len(subject.split()) >= 2


def build_prompt(text: str) -> str:
    """The description to draw, taken from what the student typed.

    Strips the request wrapped around the subject — "please can you generate an
    image of …" — so the endpoint is given the picture, not the asking.
    """
    prompt = strip_command(text)
    prompt = re.sub(
        rf"^(?:please\s+)?(?:can|could|would)?\s*(?:you\s+)?(?:please\s+)?{_MAKE}\s+"
        rf"(?:me\s+|us\s+)?(?:a|an|the|some\s+)?(?:\w+\s+){{0,3}}?{_NOUN}\s+"
        r"(?:of|showing|depicting|with|that shows)\s+",
        "",
        prompt,
        flags=re.IGNORECASE,
    ).strip()
    return prompt or strip_command(text)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def _guard(prompt: str, cfg: dict) -> None:
    """The three refusals that are ours rather than the vendor's."""
    if not cfg["enabled"]:
        raise AIError(
            "Image generation is switched off at the moment.",
            code="disabled", retryable=False,
        )
    if not cfg["api_key"]:
        # An operator problem, phrased as one the student can act on — and
        # never naming which credential is missing or where it lives.
        raise AIError(
            "Image generation is not available right now. Please contact support.",
            code="not_configured", retryable=False,
        )
    if not prompt:
        raise AIError(
            "Tell me what to draw \u2014 for example, \u201can image of a lighthouse at dawn\u201d.",
            code="empty_prompt", retryable=False,
        )


def generate(prompt: str, *, size: str = "", quality: str = "") -> GeneratedImage:
    """Draw one picture, or raise :class:`AIError` with a message a student can read."""
    prompt = (prompt or "").strip()
    cfg = config()
    _guard(prompt, cfg)

    if size in VALID_SIZES:
        cfg["size"] = size
    if quality in VALID_QUALITIES:
        cfg["quality"] = quality

    client = imaging.build(
        cfg["provider"],
        imaging.ImageConfig(
            api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
            size=cfg["size"], quality=cfg["quality"], timeout=cfg["timeout"],
        ),
    )
    return client.generate(prompt)


def edit(prompt: str, image: bytes, *, mime: str = "image/png") -> GeneratedImage:
    """Change an existing picture according to ``prompt``.

    Refused up front when the configured vendor cannot do it, so a student is
    never sent through a request that was always going to fail.
    """
    prompt = (prompt or "").strip()
    cfg = config()
    _guard(prompt, cfg)

    if not cfg["supports_edit"]:
        raise AIError(
            f"{cfg['provider_name']} cannot change an existing image. "
            "Try describing a new one instead.",
            code="edit_unsupported", retryable=False,
        )
    if not image:
        raise AIError("There is no image to change.", code="no_source", retryable=False)

    client = imaging.build(
        cfg["provider"],
        imaging.ImageConfig(
            api_key=cfg["api_key"], base_url=cfg["base_url"], model=cfg["model"],
            size=cfg["size"], quality=cfg["quality"], timeout=cfg["timeout"],
        ),
    )
    return client.edit(prompt, image, mime=mime)


def client_hints() -> dict:
    """The detection rules, for the browser's waiting label.

    The client has to guess whether a message is going to produce a picture, so
    it can say "Creating your image…" rather than "Thinking…" while it waits.
    That guess must agree with the decision this module actually makes, and the
    only way to keep two copies of a heuristic in step is to not have two — so
    the patterns are handed to the page from here.

    Cosmetic only: the answer that comes back is authoritative either way, and
    nothing about billing or routing depends on what the browser concluded.
    """
    return {
        "make": _MAKE,
        "noun": _NOUN,
        # Taken from the compiled pattern itself rather than retyped, so there
        # is genuinely one copy of the rule.
        "slash": _SLASH.pattern,
        "about": _ABOUT.pattern,
        "notPicture": sorted(_NOT_A_PICTURE),
        "maxChars": _MAX_PROMPT_CHARS,
    }



__all__ = [
    "GeneratedImage",
    "can_edit",
    "edit",
    "provider",
    "VALID_QUALITIES",
    "VALID_SIZES",
    "build_prompt",
    "client_hints",
    "config",
    "generate",
    "is_available",
    "is_configured",
    "looks_like_image_request",
    "is_explicit_command",
    "status",
    "strip_command",
    "token_cost",
]
