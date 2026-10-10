"""Public marketing and legal pages."""
from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal
from urllib.parse import quote

from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from AI.models import AIUsageLog, PaymentMethod, Plan
from AI.services import referral_service, settings_service, share_service

#: Roughly what one question and its worked answer costs. Published in one
#: place so the "≈ N questions" on a plan card and the number quoted in the
#: FAQ can never drift apart.
TOKENS_PER_QUESTION = 200

#: How much you study, smallest first. The switcher above the plan cards
#: recommends the plan whose size matches the answer.
INTENSITIES = ("Light", "Regular", "Heavy")


def plan_cards(plans: list[Plan]) -> list[dict]:
    """The three cards the landing page draws, everything derived here.

    A card's "fit" is a rank rather than a threshold: the smallest plan on
    the page is the light one and the largest is the heavy one, whatever the
    catalogue happens to contain — so the switcher keeps working when the
    plans are repriced or replaced.
    """
    plans = list(plans)
    if not plans:
        return []

    by_size = sorted(plans, key=lambda plan: plan.tokens)
    biggest = by_size[-1].tokens or 1
    smallest = by_size[0].tokens
    last = len(by_size) - 1
    fits: dict[int, str] = {}
    for index, plan in enumerate(by_size):
        share = (index / last) if last else 0.5
        fits[plan.pk] = INTENSITIES[0] if share < 1 / 3 else (INTENSITIES[2] if share > 2 / 3 else INTENSITIES[1])

    cards = []
    for index, plan in enumerate(plans):
        if plan.is_featured:
            tag = "MOST POPULAR"
        elif plan.tokens == biggest and biggest != smallest:
            tag = "BIGGEST BALANCE"
        elif plan.tokens == smallest:
            tag = "STARTER"
        else:
            tag = "PLAN"
        amount = Decimal(plan.price or 0)
        cards.append({
            "plan": plan,
            "tag": tag,
            "ribbon": "MOST POPULAR" if plan.is_featured else "BEST FIT",
            "fit": fits[plan.pk],
            "fit_key": fits[plan.pk].lower(),
            "price": f"${amount:,.2f}" if plan.currency == "USD" else f"{amount:,.2f} {plan.currency}",
            "questions": f"{plan.tokens // TOKENS_PER_QUESTION:,}",
            # Against the biggest plan on the page, so the widest meter is
            # always full and the rest read as a share of it.
            "meter": round(plan.tokens * 100 / biggest, 1),
            "perks": plan.feature_list,
            "delay": f"{index * 0.08:.2f}s",
        })
    return cards


def typical_reply() -> dict:
    """How long an answer actually takes, measured rather than claimed.

    The median of recent successful requests, rounded up to the second the
    page can honestly stand behind. With too little history to be a median
    it falls back to the published expectation and says so.
    """
    cached = cache.get("landing_typical_reply")
    if cached:
        return cached

    window = timezone.now() - timedelta(days=90)
    logs = AIUsageLog.objects.filter(success=True, latency_ms__gt=0, created_at__gte=window)
    total = logs.count()
    if total < 10:
        answer = {"value": "<5s", "sub": "straight to the working", "measured": False}
    else:
        median = logs.order_by("latency_ms").values_list("latency_ms", flat=True)[total // 2]
        answer = {
            "value": f"<{max(1, math.ceil(median / 1000))}s",
            "sub": f"median of {total:,} answers",
            "measured": True,
        }
    cache.set("landing_typical_reply", answer, 600)
    return answer


def home(request):
    plans = list(Plan.objects.filter(is_active=True).order_by("display_order", "price")[:3])
    free_trial = int(settings_service.get("free_trial_tokens", 0))
    methods = [label for _value, label in PaymentMethod.choices]
    example = max((plan.tokens for plan in plans), default=100_000)

    return render(
        request,
        "public/home.html",
        {
            "plans": plans,
            "plan_cards": plan_cards(plans),
            "intensities": INTENSITIES,
            "signup_enabled": settings_service.get("signup_enabled", True),
            "free_trial_tokens": free_trial,
            "free_trial_model": referral_service.welcome_model_name(),
            "payment_methods": methods,
            "hero_reply": typical_reply(),
            "steps": [
                {
                    "title": "Create your account",
                    "body": "Sign up with an e-mail address, verify with a code, and free tokens land in "
                            "your balance straight away.",
                    "chips": ["30 seconds", f"{free_trial:,} free {referral_service.welcome_model_name()} tokens"
                              .replace("  ", " ")] if free_trial else ["30 seconds"],
                },
                {
                    "title": "Ask anything",
                    "body": "Type a question or attach a photo. Follow up as many times as you need until "
                            "it genuinely makes sense.",
                    "chips": ["Text or photo", "Unlimited follow-ups"],
                },
                {
                    "title": "Top up when you need to",
                    "body": f"Pay with {', '.join(methods[:-1])} or {methods[-1]}. Tokens are credited the "
                            "moment payment is confirmed.",
                    "chips": [f"{len(methods)} methods", "Instant credit"],
                },
            ],
            "faqs": [
                {
                    "q": "What is a token?",
                    "a": "A token is a small piece of text — roughly three quarters of a word. Every question "
                         "you ask and every answer you get back uses some, so a short question costs very "
                         f"little and a long worked solution costs more. About {TOKENS_PER_QUESTION} tokens "
                         f"covers a typical question and its answer, which makes a {example:,}-token plan "
                         f"roughly {example // TOKENS_PER_QUESTION:,} questions. Your balance is always "
                         "visible in the dashboard.",
                },
                {
                    "q": "Do my tokens expire?",
                    "a": "No. While your plan is active your tokens stay in your balance, so nothing is "
                         "wasted if you take a week off between assignments.",
                },
                {
                    "q": "Can I upload a photo of a question?",
                    "a": "Yes. Photograph a textbook page, a worksheet or your own handwriting and ask about "
                         "it directly — the assistant reads the image and works through it with you.",
                },
                {
                    "q": "Which payment methods do you accept?",
                    "a": f"{', '.join(methods[:-1])} and {methods[-1]}. Once a payment is confirmed your "
                         "tokens are credited to your balance automatically.",
                },
                {
                    "q": "Is my work private?",
                    "a": "Your conversations are yours. They are stored so you can search and revisit them "
                         "before an exam, and they are never shown to other students.",
                },
                {
                    "q": "What if something goes wrong with a payment?",
                    "a": "Send us the reference from your receipt and we will trace it. If a payment was "
                         "taken without tokens being credited, we credit them manually or refund it.",
                },
            ],
        },
    )


def plans(request):
    """One canonical /plans/ URL.

    Visitors and admins see the marketing page; a signed-in student sees their
    billing view (current plan, payment history, purchase actions).
    """
    user = request.user
    if user.is_authenticated and user.is_student and user.is_active_account:
        # A student's plans are a region of their own shell, not a marketing
        # page, so this URL leads them there — with whatever they were asking
        # for still attached.
        query = request.META.get("QUERY_STRING", "")
        target = reverse("student:pricing")
        return redirect(f"{target}?{query}" if query else target)

    return render(
        request,
        "public/plans.html",
        {
            "plans": Plan.objects.filter(is_active=True).order_by("display_order", "price"),
            "free_trial_tokens": int(settings_service.get("free_trial_tokens", 0)),
            "free_trial_model": referral_service.welcome_model_name(),
        },
    )


def refund_policy(request):
    return render(request, "public/refund_policy.html")


def privacy(request):
    return render(request, "public/privacy.html")


def terms(request):
    return render(request, "public/terms.html")


def error_404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def error_500(request):
    return render(request, "errors/500.html", status=500)


def error_403(request, exception=None):
    return render(request, "errors/403.html", status=403)


def referral_landing(request, code: str):
    """``/r/<CODE>/`` — the link an administrator hands out.

    Records the visit, then sends the person to signup with the code already
    filled in. It exists as its own URL rather than as a query parameter on the
    signup page for two reasons: it is short enough to print, and a click can be
    counted before anyone decides whether to sign up, which is the difference
    between "nobody saw the poster" and "everybody saw it and walked away".

    An unknown or finished code is not an error page. Someone holding an expired
    flyer should still land on a working signup form; the code simply does not
    fill itself in, and the form would reject it anyway if they typed it.
    """
    from AI.services import referral_campaign_service
    from AI.utils.request import client_ip

    campaign = referral_campaign_service.find(code)
    if campaign is None:
        return redirect("auth:signup")

    referral_campaign_service.record_click(
        campaign,
        ip=client_ip(request),
        agent=request.META.get("HTTP_USER_AGENT", ""),
    )
    if not campaign.is_usable:
        return redirect("auth:signup")
    return redirect(f"{reverse('auth:signup')}?ref={quote(campaign.code)}")


def shared_chat(request, token: str):
    """A conversation a student chose to publish, read-only.

    Anyone with the link can read it; nobody can reply, and nothing about the
    student beyond their first name is shown. An unknown, revoked or disabled
    link is a plain 404, so a link that stopped working says nothing about why.
    """
    from django.http import Http404

    link, messages = share_service.open_link(token)
    if link is None:
        raise Http404("This shared conversation is not available.")
    owner = link.conversation.user
    first_name = ((owner.display_name or owner.username or "").split() or ["A student"])[0]
    transcript = [
        {"role": m.role, "content": m.content, "model": m.model_key or ""}
        for m in messages
    ]
    return render(
        request,
        "public/shared_chat.html",
        {
            "link": link,
            "conversation": link.conversation,
            "owner_name": first_name,
            "transcript": transcript,
            "message_count": len(transcript),
        },
    )
