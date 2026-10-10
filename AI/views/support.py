"""Help & Support — the student's side.

The FAQ and the request form are a panel inside the chat shell (see
:mod:`AI.views.panels`); the FAQ content itself still lives here, because it
is support copy rather than presentation. Ticket threads keep pages of their
own: a thread is a document with a URL you can send someone.

Ownership is re-checked on every ticket view, so a reference number belonging
to someone else is simply not found.
"""
from __future__ import annotations

import logging

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from AI.forms import TicketReplyForm
from AI.models import SupportTicket
from AI.services import support_service
from AI.utils.decorators import login_required_student

logger = logging.getLogger("AI.views.support")

#: The questions students actually ask, grouped the way they think about them.
FAQ = [
    {
        "group": "Getting started",
        "items": [
            {
                "q": "How do I start a conversation?",
                "a": "Open the chat and type your question. Ask the way you would ask a tutor — "
                     "give the subject, the level, and what you have already tried. The more "
                     "context you give, the more useful the answer.",
            },
            {
                "q": "What can the assistant help with?",
                "a": "Explaining concepts, working through maths and science step by step, "
                     "reviewing and debugging code, planning essays, and preparing for exams. "
                     "It shows its working rather than only giving answers.",
            },
            {
                "q": "Can I keep separate chats for different subjects?",
                "a": "Yes. Use New chat for each topic, then rename it so you can find it later. "
                     "Pin the ones you come back to and they stay at the top.",
            },
        ],
    },
    {
        "group": "Tokens and usage",
        "items": [
            {
                "q": "What is a token?",
                "a": "Tokens are how AI usage is measured — roughly four characters of English. "
                     "Both your question and the reply count. A short question costs a few "
                     "hundred; a long, detailed answer costs more.",
            },
            {
                "q": "Where do I see my balance?",
                "a": "In the chat sidebar, and on the Billing page. It shows what you have left "
                     "out of your current allocation, and how much you have used.",
            },
            {
                "q": "What happens when I run out?",
                "a": "The assistant stops replying and the chat shows an upgrade prompt. Your "
                     "conversations are kept — nothing is deleted. Add a plan and you can carry "
                     "on where you left off.",
            },
            {
                "q": "How do I use fewer tokens?",
                "a": "Ask focused questions, start a new chat when you change topic (a long "
                     "history is re-sent with every message), and say when a short answer will do.",
            },
        ],
    },
    {
        "group": "Usage limits",
        "items": [
            {
                "q": "What are the daily, weekly and monthly limits?",
                "a": "Every package sets how many of its tokens you can spend in a day, a week "
                     "and a month. They keep the service quick and reliable for everybody, and "
                     "they stop a single long session from using up a package in an afternoon. "
                     "You can see all three in the sidebar and on the Usage page.",
            },
            {
                "q": "I have tokens left but I cannot send a message.",
                "a": "Then you have reached one of your usage limits rather than run out. "
                     "Nothing has been lost — the message will say which limit it was and when "
                     "it resets, and the sidebar counts down to it. Your tokens are still there "
                     "waiting.",
            },
            {
                "q": "When exactly does a limit reset?",
                "a": "Each one runs from when your package was activated, not from midnight. "
                     "The daily limit clears every 24 hours, the weekly every 7 days, and the "
                     "monthly on the same date each month. The exact time is shown next to each "
                     "one — you do not need to refresh the page for it to come back.",
            },
            {
                "q": "Can I have my limit raised?",
                "a": "Buying a larger package raises all three limits, and packages add together "
                     "if you hold more than one. If you need something different from what is on "
                     "the Pricing page, message us on WhatsApp.",
            },
            {
                "q": "Do the limits apply to every model separately?",
                "a": "Yes. Each model has its own tokens and its own limits, so reaching your "
                     "daily limit on one model does not stop you using another one you hold "
                     "tokens for.",
            },
        ],
    },
    {
        "group": "Images",
        "items": [
            {
                "q": "Can the assistant create images?",
                "a": "Yes. Just ask for one in plain English — \u201ccreate a monkey eating a "
                     "banana\u201d, \u201cdraw a diagram of the water cycle\u201d — and the "
                     "picture appears in the conversation. There is no special command to learn, "
                     "though you can type /image if you want to be certain.",
            },
            {
                "q": "How much does an image cost?",
                "a": "A picture costs a fixed number of tokens from the model you are using, and "
                     "the amount is shown against the message like any other. If the picture "
                     "fails to generate you are not charged for it.",
            },
            {
                "q": "Can I change an image I have been given?",
                "a": "Under every picture there are buttons to generate it again, make a "
                     "variation, copy the prompt, or attach it to your next message so you can "
                     "ask about it.",
            },
        ],
    },
    {
        "group": "Files and voice",
        "items": [
            {
                "q": "Can I attach a photo of a problem?",
                "a": "Yes, where the model supports images. Use the + button beside the message "
                     "box. Clear, well-lit photos of the whole question work best.",
            },
            {
                "q": "Why was my file rejected?",
                "a": "Files are checked by their actual content, not their name, and there is a "
                     "size limit. Programs, scripts and web pages are never accepted. If a valid "
                     "file is refused, raise a ticket and include it.",
            },
            {
                "q": "How does voice input work?",
                "a": "Press the microphone, speak, then press it again to stop. What you said "
                     "appears in the message box for you to check and edit before sending. The "
                     "recording is transcribed on our servers and uses a few tokens; your "
                     "browser will ask for microphone permission the first time.",
            },
        ],
    },
    {
        "group": "Billing and account",
        "items": [
            {
                "q": "How do I upgrade?",
                "a": "Open Pricing, choose a plan and follow the checkout. Tokens are credited "
                     "as soon as the payment is confirmed.",
            },
            {
                "q": "Where are my invoices?",
                "a": "On the Billing page. Every payment has a receipt you can open and print — "
                     "to keep a PDF, choose \"Save as PDF\" in your browser's print dialog.",
            },
            {
                "q": "My payment has not been credited.",
                "a": "Bank transfers are confirmed by an administrator and can take a little "
                     "time. If it has been more than a day, raise a ticket under Payment issue "
                     "and include the reference.",
            },
            {
                "q": "I was signed out unexpectedly.",
                "a": "Only one device can be signed in at a time. Signing in elsewhere ends the "
                     "previous session. If that was not you, change your password immediately.",
            },
        ],
    },
]


@login_required_student
def ticket_list(request):
    tickets = SupportTicket.objects.filter(user=request.user)
    return render(
        request,
        "student/tickets.html",
        {"nav_active": "help", "tickets": tickets},
    )


@login_required_student
def ticket_detail(request, reference: str):
    """One ticket and its thread. Internal notes are never included."""
    ticket = get_object_or_404(SupportTicket, reference=reference, user=request.user)
    return render(
        request,
        "student/ticket_detail.html",
        {
            "nav_active": "help",
            "ticket": ticket,
            "replies": support_service.visible_replies(ticket, for_staff=False),
            "form": TicketReplyForm(),
        },
    )


@login_required_student
@require_POST
def ticket_reply(request, reference: str):
    ticket = get_object_or_404(SupportTicket, reference=reference, user=request.user)

    if ticket.status == "closed":
        messages.warning(request, "That ticket is closed. Open a new request if you still need help.")
        return redirect("student:ticket_detail", reference=reference)

    form = TicketReplyForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, next(iter(form.errors.values()))[0])
        return redirect("student:ticket_detail", reference=reference)

    support_service.add_reply(
        ticket, request.user, form.cleaned_data["body"], from_staff=False,
        attachment=form.cleaned_data.get("attachment"),
    )
    messages.success(request, "Your message was added to the ticket.")
    return redirect("student:ticket_detail", reference=reference)


@login_required_student
@require_POST
def ticket_close(request, reference: str):
    """The student says it is sorted, so it stops sitting in the team's queue."""
    ticket = get_object_or_404(SupportTicket, reference=reference, user=request.user)
    if ticket.is_closed:
        messages.info(request, "That ticket is already closed.")
    else:
        support_service.close_by_student(ticket, request.user)
        messages.success(request, "Ticket closed. Thanks for letting us know it is sorted.")
    return redirect("student:ticket_detail", reference=reference)
