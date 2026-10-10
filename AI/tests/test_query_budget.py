"""Pages must not get slower as the database gets bigger.

An N+1 does not fail a functional test — every page still renders, every number
is still right — it just quietly issues one more query per row until the console
is unusable at a few hundred students. So the property under test is not a
number of queries but a *shape*: the count must not move when the data grows.

The absolute ceilings are deliberately loose. They exist to catch a change that
doubles the work on a hot page, not to freeze today's figure.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from AI.models import AccountStatus, AIModel, Plan, Role, User
from AI.services import chat_service, settings_service, wallet_service
from AI.services.providers.base import AIResponse

PASSWORD = "Sup3rSecret!x"
COMPLETE = "AI.services.providers.openai_provider.OpenAICompatibleProvider.complete"


def reply(inp=10, out=10):
    return AIResponse(
        content="x", input_tokens=inp, output_tokens=out, total_tokens=inp + out,
        model="gpt-5-mini", request_id="q", latency_ms=1,
    )


class QueryBudgetTests(TestCase):
    """One student's pages, then the same pages with far more data behind them."""

    #: Generous upper bounds — a tripwire for a regression, not a target.
    CEILINGS = {
        "/chat/": 40,
        "/app/pricing/": 70,
        "/app/usage/": 60,
        "/admin/ai-tokens/": 45,
        "/admin/usage-limits/": 30,
        "/admin/users/": 30,
    }

    def setUp(self):
        settings_service.bootstrap()
        self.model = AIModel.objects.get(key="gpt-5-mini")
        self.plan = Plan.objects.get(slug="gpt-5-mini-100k")

        self.student = self._student("solo")
        self.admin = User.objects.create_superuser(
            username="root", email="root@test.local", password=PASSWORD
        )
        self.admin.role = Role.ADMIN
        self.admin.save()

    def _student(self, name):
        user = User.objects.create_user(
            username=name, email=f"{name}@test.local", password=PASSWORD,
            is_verified=True, status=AccountStatus.ACTIVE,
        )
        wallet_service.credit(
            user, self.model, self.plan.tokens, transaction_type="plan_purchase",
            reason=self.plan.name, plan=self.plan,
        )
        return user

    def _counts(self) -> dict[str, int]:
        """One measurement per page, each warmed first so caches do not skew it."""
        counts = {}
        for url in self.CEILINGS:
            actor = self.admin if url.startswith("/admin/") else self.student
            self.client.force_login(actor)
            User.objects.filter(pk=actor.pk).update(
                active_session_key=self.client.session.session_key
            )
            self.client.get(url)                      # warm caches and sessions
            with CaptureQueriesContext(connection) as captured:
                response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            counts[url] = len(captured)
        return counts

    def _add_data(self, students: int = 12):
        for index in range(students):
            user = self._student(f"bulk{index}")
            conversation = chat_service.create_conversation(user)
            with patch(COMPLETE, return_value=reply()):
                chat_service.send_message(user, conversation, "hi")

    def test_no_page_issues_more_queries_as_the_data_grows(self):
        before = self._counts()
        self._add_data()
        after = self._counts()

        for url, first in before.items():
            with self.subTest(url=url):
                # A couple of extra queries is ordinary — a count that tracks
                # the number of rows is the bug.
                self.assertLessEqual(
                    after[url], first + 3,
                    f"{url}: {first} -> {after[url]} queries as data grew — likely an N+1",
                )

    def test_no_page_is_extravagant_to_begin_with(self):
        for url, count in self._counts().items():
            with self.subTest(url=url):
                self.assertLessEqual(
                    count, self.CEILINGS[url],
                    f"{url} took {count} queries, over its budget of {self.CEILINGS[url]}",
                )
