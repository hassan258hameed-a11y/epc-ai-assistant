"""Populate the database with development data.

Creates a demo admin, demo students, plans, conversations and notifications so
every screen can be reviewed with realistic content.

Passwords are generated at random and printed once — no credentials are
committed to the repository.
"""
from __future__ import annotations

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from AI.models import (
    AccountSource,
    AccountStatus,
    AIUsageLog,
    Conversation,
    Message,
    MessageRole,
    Notification,
    NotificationCategory,
    NotificationLevel,
    Payment,
    PaymentMethod,
    Plan,
    Role,
    User,
    generate_password,
)
from AI.services import payment_service, referral_service, settings_service, token_service

PLANS = [
    {
        "name": "Starter", "tokens": 500_000, "price": Decimal("3.00"), "duration_days": 30,
        "description": "Enough for daily homework help.", "display_order": 1,
        "features": "500,000 tokens\nAll subjects\nImage questions\n30 days access",
    },
    {
        "name": "Student", "tokens": 2_000_000, "price": Decimal("10.00"), "duration_days": 60,
        "description": "Our most popular plan for a full term.", "display_order": 2, "is_featured": True,
        "features": "2,000,000 tokens\nAll subjects\nImage questions\nPriority support\n60 days access",
    },
    {
        "name": "Scholar", "tokens": 6_000_000, "price": Decimal("25.00"), "duration_days": 120,
        "description": "For heavy research and coding work.", "display_order": 3,
        "features": "6,000,000 tokens\nAll subjects\nImage questions\nPriority support\n120 days access",
    },
]

SAMPLE_CHATS = [
    (
        "Quadratic equations",
        "Solve 2x² - 7x + 3 = 0 and explain each step.",
        "Let's factorise it.\n\n**Step 1 — find two numbers** that multiply to `2 × 3 = 6` and add to `-7`. "
        "Those are `-6` and `-1`.\n\n**Step 2 — split the middle term**\n\n```\n2x² - 6x - x + 3 = 0\n```\n\n"
        "**Step 3 — factor in pairs**\n\n```\n2x(x - 3) - 1(x - 3) = 0\n(2x - 1)(x - 3) = 0\n```\n\n"
        "**Step 4 — solve each bracket**\n\n- `2x - 1 = 0` → **x = 0.5**\n- `x - 3 = 0` → **x = 3**\n\n"
        "So the roots are `x = 0.5` and `x = 3`. Substitute them back to check — both give 0.",
    ),
    (
        "Python list comprehension",
        "What is a list comprehension in Python and when should I use one?",
        "A list comprehension builds a list in a single expression.\n\n```python\n"
        "# Instead of this\nsquares = []\nfor n in range(10):\n    if n % 2 == 0:\n        squares.append(n ** 2)\n\n"
        "# Write this\nsquares = [n ** 2 for n in range(10) if n % 2 == 0]\n```\n\n"
        "**Use one when** the transformation is short and readable on one line.\n\n"
        "**Avoid one when** the logic needs multiple statements, several nested loops, or a `try/except` — "
        "a normal loop is clearer there.",
    ),
    (
        "Essay structure help",
        "How should I structure a five-paragraph essay on renewable energy?",
        "Here is a reliable structure.\n\n## 1. Introduction\nOpen with a fact, narrow to your topic, and end with a "
        "clear thesis such as: *“Renewable energy is the most practical route to energy security because it is "
        "increasingly cheap, domestically produced and low-carbon.”*\n\n"
        "## 2–4. Body paragraphs\nOne argument each:\n\n| Paragraph | Argument | Evidence to find |\n"
        "|---|---|---|\n| 2 | Falling cost | Price per kWh over time |\n| 3 | Energy independence | Import figures |\n"
        "| 4 | Emissions | Lifecycle CO₂ comparison |\n\n"
        "## 5. Conclusion\nRestate the thesis in new words, summarise the three arguments, and finish with the "
        "wider implication — do not introduce new evidence here.",
    ),
]


class Command(BaseCommand):
    help = "Create demo data for local development (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=6, help="How many demo students to create.")
        parser.add_argument("--reset", action="store_true", help="Delete existing demo accounts first.")

    @transaction.atomic
    def handle(self, *args, **options):
        settings_service.bootstrap()

        if options["reset"]:
            deleted, _ = User.objects.filter(username__startswith="demo.").delete()
            User.objects.filter(username="admin.demo").delete()
            self.stdout.write(self.style.WARNING(f"Removed {deleted} demo record(s)."))

        credentials = []

        # ---------- Plans ----------
        for spec in PLANS:
            plan, created = Plan.objects.get_or_create(name=spec["name"], defaults=spec)
            if created:
                self.stdout.write(f"  plan     {plan.name} — {plan.tokens:,} tokens / ${plan.price}")

        plans = list(Plan.objects.filter(is_active=True).order_by("display_order"))

        # ---------- Admin ----------
        admin = User.objects.filter(username="admin.demo").first()
        if admin is None:
            password = generate_password(14)
            admin = User.objects.create_superuser(
                username="admin.demo",
                email="admin.demo@example.com",
                password=password,
                full_name="Demo Administrator",
            )
            credentials.append(("Admin console", "admin.demo", password))
            self.stdout.write("  admin    admin.demo")

        # ---------- Students ----------
        names = [
            ("Sokha Ny", AccountSource.WEB), ("Dara Kim", AccountSource.TELEGRAM),
            ("Chanthy Sok", AccountSource.TIKTOK), ("Vichea Lim", AccountSource.MESSENGER),
            ("Sreyneang Pov", AccountSource.INSTAGRAM), ("Rithy Chan", AccountSource.ADMIN),
            ("Nita Meas", AccountSource.WEB), ("Pisey Hong", AccountSource.IMPORT),
        ]

        students = []
        for index in range(min(options["students"], len(names))):
            full_name, source = names[index]
            username = f"demo.{full_name.split()[0].lower()}"
            student = User.objects.filter(username=username).first()

            if student is None:
                password = generate_password(12)
                student = User.objects.create_user(
                    username=username,
                    email=f"{username}@example.com",
                    password=password,
                    full_name=full_name,
                    source=source,
                    is_verified=True,
                    status=AccountStatus.ACTIVE,
                    role=Role.STUDENT,
                )
                student.date_joined = timezone.now() - timedelta(days=random.randint(1, 60))
                student.last_seen_at = timezone.now() - timedelta(hours=random.randint(0, 72))
                student.save(update_fields=["date_joined", "last_seen_at"])

                if index == 0:
                    credentials.append(("Student", username, password))

                token_service.grant_free_trial(student, int(settings_service.get("free_trial_tokens", 20000)))
                if index % 3 != 2:
                    token_service.grant(
                        student, random.choice([250_000, 500_000, 1_000_000]),
                        description="Demo allocation", created_by=admin,
                    )
                self.stdout.write(f"  student  {username}")

            students.append(student)

        # One disabled and one empty-balance account so those states are visible.
        if len(students) >= 4:
            drained = students[3]
            account = token_service.get_account(drained)
            if account.balance > 0:
                token_service.deduct(drained, account.balance, description="Demo — drained balance", created_by=admin)
            if len(students) >= 5:
                blocked = students[4]
                blocked.status = AccountStatus.DISABLED
                blocked.is_active = False
                blocked.disabled_reason = "Demo — suspended account"
                blocked.save(update_fields=["status", "is_active", "disabled_reason"])

        # ---------- Referrals ----------
        if len(students) >= 3:
            referral_service.register_referral(students[2], students[0])
            referral_service.reward_referral(students[2])

        # ---------- Conversations ----------
        for student in students[:4]:
            if Conversation.objects.filter(user=student).exists():
                continue
            for title, question, answer in SAMPLE_CHATS:
                conversation = Conversation.objects.create(user=student, title=title)
                created_at = timezone.now() - timedelta(days=random.randint(0, 12), hours=random.randint(0, 20))

                Message.objects.create(conversation=conversation, role=MessageRole.USER, content=question)
                input_tokens = random.randint(80, 260)
                output_tokens = random.randint(280, 900)
                reply = Message.objects.create(
                    conversation=conversation,
                    role=MessageRole.ASSISTANT,
                    content=answer,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens,
                    model="gpt-4o-mini",
                    latency_ms=random.randint(700, 2600),
                )
                Conversation.objects.filter(pk=conversation.pk).update(
                    created_at=created_at, updated_at=created_at,
                    message_count=2, total_tokens=input_tokens + output_tokens,
                )

                usage = AIUsageLog.objects.create(
                    user=student, conversation=conversation, message=reply, model="gpt-4o-mini",
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    total_tokens=input_tokens + output_tokens, tokens_charged=input_tokens + output_tokens,
                    success=True, latency_ms=reply.latency_ms,
                )
                AIUsageLog.objects.filter(pk=usage.pk).update(created_at=created_at)

                token_service.consume(
                    student, input_tokens + output_tokens,
                    description="gpt-4o-mini — demo conversation", source="gpt-4o-mini",
                    conversation=conversation, message=reply,
                )

        # ---------- Payments ----------
        if plans and len(students) >= 2 and not Payment.objects.exists():
            for student, plan, method in (
                (students[0], plans[1], PaymentMethod.ABA_PAYWAY),
                (students[1], plans[0], PaymentMethod.ACLEDA_KHQR),
                (students[2] if len(students) > 2 else students[0], plans[2], PaymentMethod.BINANCE),
            ):
                payment = payment_service.create_payment(student, plan, method, note="Demo order")
                payment_service.settle(payment, provider_txn_id=f"DEMO{random.randint(10000, 99999)}",
                                       source="seed", actor=admin, note="Seeded demo payment")

            pending = payment_service.create_payment(students[0], plans[0], PaymentMethod.ACLEDA_KHQR,
                                                     note="Awaiting confirmation")
            self.stdout.write(f"  payment  {pending.reference} left pending for review")

        # ---------- Notifications ----------
        for student in students[:3]:
            if Notification.objects.filter(user=student).exists():
                continue
            Notification.objects.create(
                user=student, title="Welcome to EPC AI Assistant",
                body="Your free tokens are ready. Ask your first question whenever you like.",
                level=NotificationLevel.SUCCESS, category=NotificationCategory.SYSTEM,
                action_url="/chat/", action_label="Start chatting",
            )
            Notification.objects.create(
                user=student, title="Tip: attach a photo of your question",
                body="Stuck on a textbook problem? Take a picture and ask about it directly in chat.",
                level=NotificationLevel.INFO, category=NotificationCategory.ANNOUNCEMENT,
            )

        # ---------- Summary ----------
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        if credentials:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING("  Save these now — they are shown only once:"))
            for label, username, password in credentials:
                self.stdout.write(f"    {label:<14} {username:<16} {password}")
        else:
            self.stdout.write("  Demo accounts already existed; passwords unchanged.")
        self.stdout.write("")
        self.stdout.write("  Student area : http://127.0.0.1:8000/login/")
        self.stdout.write("  Admin console: http://127.0.0.1:8000/admin/login/")
