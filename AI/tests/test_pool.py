"""The admin token pools.

Everything an administrator gives a student comes out of a supply, and everything
taken back returns to it. These tests pin that arithmetic down, plus the two
guarantees that matter most: a pool can never be overdrawn, and each model draws
only from its own.

:class:`PoolServiceTests` and :class:`PoolModelTests` cover the original shared
pool, which still exists so historical data keeps its meaning. Live allocation
goes through the per-model pools exercised in :class:`PoolThroughTheConsoleTests`.
"""
from __future__ import annotations

from django.test import TestCase

from AI.models import (
    AccountStatus,
    AIModel,
    ModelTokenPool,
    PoolAction,
    PoolTransaction,
    Role,
    TokenPool,
    User,
)
from AI.services import (
    model_pool_service,
    pool_service,
    settings_service,
    token_service,
    wallet_service,
)
from AI.services.pool_service import InsufficientPoolTokens
from AI.tests.helpers import GPT, SONNET

PASSWORD = "Sup3rSecret!x"


def make_student(username: str) -> User:
    return User.objects.create_user(
        username=username, email=f"{username}@test.local", password=PASSWORD,
        is_verified=True, status=AccountStatus.ACTIVE,
    )


class PoolServiceTests(TestCase):
    def setUp(self):
        self.student = make_student("poolee")

    def test_a_new_pool_starts_empty(self):
        self.assertEqual(pool_service.balance(), 0)
        self.assertTrue(pool_service.summary()["never_funded"])

    def test_top_up_then_draw_moves_the_balance(self):
        pool_service.top_up(300_000)
        self.assertEqual(pool_service.balance(), 300_000)

        pool_service.draw(25_000, student=self.student)
        self.assertEqual(pool_service.balance(), 275_000)

        summary = pool_service.summary()
        self.assertEqual(summary["total_added"], 300_000)
        self.assertEqual(summary["total_distributed"], 25_000)
        self.assertEqual(summary["outstanding"], 25_000)

    def test_reclaimed_tokens_return_to_the_pool(self):
        pool_service.top_up(100_000)
        pool_service.draw(40_000, student=self.student)
        pool_service.give_back(15_000, student=self.student)

        self.assertEqual(pool_service.balance(), 75_000)
        summary = pool_service.summary()
        self.assertEqual(summary["total_reclaimed"], 15_000)
        self.assertEqual(summary["outstanding"], 25_000)
        # A reclaim is not new supply.
        self.assertEqual(summary["total_added"], 100_000)

    def test_the_pool_cannot_be_overdrawn(self):
        pool_service.top_up(10_000)

        with self.assertRaises(InsufficientPoolTokens) as caught:
            pool_service.draw(25_000, student=self.student)

        self.assertEqual(caught.exception.shortfall, 15_000)
        self.assertIn("15,000", caught.exception.friendly_message)
        self.assertEqual(pool_service.balance(), 10_000, "a refused draw must not move the balance")

    def test_a_downward_adjustment_is_clamped_at_zero(self):
        pool_service.top_up(5_000)
        pool_service.adjust(-999_999, description="Correction")
        self.assertEqual(pool_service.balance(), 0)

    def test_every_movement_is_recorded(self):
        pool_service.top_up(50_000, description="September")
        pool_service.draw(20_000, student=self.student)
        pool_service.give_back(5_000, student=self.student)

        actions = list(PoolTransaction.objects.order_by("id").values_list("action", flat=True))
        self.assertEqual(actions, [PoolAction.TOPUP, PoolAction.DISTRIBUTE, PoolAction.RECLAIM])
        self.assertEqual(
            list(PoolTransaction.objects.order_by("id").values_list("balance_after", flat=True)),
            [50_000, 30_000, 35_000],
        )


class PoolThroughTheConsoleTests(TestCase):
    """The behaviour an administrator actually sees, per model.

    Supply is no longer one shared pot: each model has its own, so every case
    here also asserts that the *other* models were left completely alone.
    """

    def setUp(self):
        settings_service.bootstrap()
        self.admin = User.objects.create_superuser(
            username="poolboss", email="poolboss@test.local", password=PASSWORD
        )
        self.client.force_login(self.admin)
        self.a = make_student("student.a")
        self.b = make_student("student.b")

        self.models = {model.key: model for model in AIModel.objects.all()}
        self.gpt = self.models[GPT]
        self.sonnet = self.models[SONNET]

        # Migration 0013 seeds every pool with an opening supply so the platform
        # is usable on day one. These tests are about the arithmetic, so they
        # start from a clean zero and put in exactly what they mean to.
        for model in self.models.values():
            model_pool_service.get_pool(model)
        ModelTokenPool.objects.update(
            balance=0, total_added=0, total_distributed=0, total_reclaimed=0
        )

    # -- helpers -----------------------------------------------------------
    def top_up(self, amount: int, model=None):
        model_pool_service.top_up(model or self.gpt, amount, created_by=self.admin)

    def _assign(self, student, action, amount, model=None):
        return self.client.post(
            "/admin/ai-tokens/distribute/apply/",
            {
                "user_id": student.pk,
                "model_id": (model or self.gpt).pk,
                "action": action,
                "amount": amount,
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def held(self, student, model=None) -> int:
        return wallet_service.balance_of(student, model or self.gpt)

    def supply(self, model=None) -> int:
        return model_pool_service.balance(model or self.gpt)

    # -- tests -------------------------------------------------------------
    def test_assigning_takes_the_tokens_out_of_that_models_pool(self):
        """300,000 in the GPT pool, give a student 25,000, 275,000 left."""
        self.top_up(300_000)

        data = self._assign(self.a, "set", 25_000).json()

        self.assertTrue(data["ok"])
        self.assertEqual(data["balance"], 25_000)
        self.assertEqual(data["pool"], 275_000)
        self.assertEqual(self.held(self.a), 25_000)
        # No other model's supply moved.
        self.assertEqual(self.supply(self.sonnet), 0)

    def test_each_student_draws_separately_from_the_same_pool(self):
        self.top_up(300_000)

        self._assign(self.a, "set", 50_000)
        self._assign(self.b, "set", 100_000)

        self.assertEqual(self.held(self.a), 50_000)
        self.assertEqual(self.held(self.b), 100_000)
        self.assertEqual(self.supply(), 150_000)

    def test_each_model_draws_from_its_own_pool(self):
        """The property the whole design exists for."""
        self.top_up(100_000, self.gpt)
        self.top_up(100_000, self.sonnet)

        self._assign(self.a, "set", 40_000, self.gpt)
        self._assign(self.a, "set", 10_000, self.sonnet)

        self.assertEqual(self.held(self.a, self.gpt), 40_000)
        self.assertEqual(self.held(self.a, self.sonnet), 10_000)
        self.assertEqual(self.supply(self.gpt), 60_000)
        self.assertEqual(self.supply(self.sonnet), 90_000)

    def test_assigning_only_draws_the_difference(self):
        """Topping a student up to 50,000 costs the pool what they were short."""
        self.top_up(100_000)
        self._assign(self.a, "set", 30_000)
        self.assertEqual(self.supply(), 70_000)

        self._assign(self.a, "set", 50_000)

        self.assertEqual(self.held(self.a), 50_000)
        self.assertEqual(self.supply(), 50_000, "only the 20,000 shortfall should be drawn")

    def test_lowering_an_allocation_returns_the_difference(self):
        self.top_up(100_000)
        self._assign(self.a, "set", 60_000)

        self._assign(self.a, "set", 20_000)

        self.assertEqual(self.held(self.a), 20_000)
        self.assertEqual(self.supply(), 80_000)

    def test_removing_tokens_puts_them_back(self):
        self.top_up(100_000)
        self._assign(self.a, "set", 40_000)

        self._assign(self.a, "remove", 15_000)

        self.assertEqual(self.held(self.a), 25_000)
        self.assertEqual(self.supply(), 75_000)

    def test_removing_more_than_a_student_holds_returns_only_what_was_taken(self):
        self.top_up(100_000)
        self._assign(self.a, "set", 10_000)

        self._assign(self.a, "remove", 999_999)

        self.assertEqual(self.held(self.a), 0)
        self.assertEqual(self.supply(), 100_000, "the pool must not gain tokens out of nowhere")

    def test_an_assignment_the_pool_cannot_cover_is_refused_entirely(self):
        self.top_up(10_000)

        response = self._assign(self.a, "set", 25_000)

        self.assertEqual(response.json()["code"], "pool_empty")
        self.assertEqual(self.held(self.a), 0, "the student must not be credited")
        self.assertEqual(self.supply(), 10_000, "the pool must not move")

    def test_usage_does_not_return_tokens_to_the_pool(self):
        """A student spending tokens on AI consumes them; they are not reclaimed."""
        self.top_up(100_000)
        self._assign(self.a, "set", 50_000)

        wallet_service.consume(self.a, self.gpt, 7_650, reason="chat")

        self.assertEqual(self.held(self.a), 42_350)
        self.assertEqual(self.supply(), 50_000)

    def test_topping_up_a_models_pool_from_the_console(self):
        self.client.post(
            "/admin/ai-tokens/pool/",
            {"model_id": self.gpt.pk, "amount": 300_000, "note": "September"},
        )
        self.assertEqual(self.supply(), 300_000)

        # A negative amount corrects it downwards.
        self.client.post("/admin/ai-tokens/pool/", {"model_id": self.gpt.pk, "amount": -50_000})
        self.assertEqual(self.supply(), 250_000)

        # And it never touched another model.
        self.assertEqual(self.supply(self.sonnet), 0)

    def test_a_bulk_run_stops_when_the_pool_runs_out(self):
        self.top_up(60_000)
        for index in range(4):
            make_student(f"bulk.{index}")

        self.client.post(
            "/admin/ai-tokens/distribute/bulk/",
            {"model_id": self.gpt.pk, "target": "active", "action": "set", "amount": 25_000},
        )

        # Two students could be covered; the rest were left alone rather than
        # half-funded, and nothing was overdrawn.
        self.assertEqual(self.supply(), 10_000)
        credited = [
            user for user in User.objects.filter(role=Role.STUDENT) if self.held(user) > 0
        ]
        self.assertEqual(len(credited), 2)

    def test_students_cannot_reach_the_pool(self):
        student = make_student("nosy")
        self.client.force_login(student)

        response = self.client.post(
            "/admin/ai-tokens/pool/", {"model_id": self.gpt.pk, "amount": 1_000_000}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.supply(), 0)

    def test_the_retired_shared_assign_endpoint_moves_nothing(self):
        """An old open tab must not silently fund the wrong model."""
        self.top_up(100_000)

        response = self.client.post(
            "/admin/tokens/assign/",
            {"user_id": self.a.pk, "action": "set", "amount": 25_000},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

        self.assertEqual(response.json()["code"], "moved")
        self.assertEqual(self.held(self.a), 0)
        self.assertEqual(self.supply(), 100_000)


class PoolModelTests(TestCase):
    def test_the_pool_is_a_singleton(self):
        first = pool_service.get_pool()
        second = pool_service.get_pool()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(TokenPool.objects.count(), 1)

    def test_percentages_are_safe_on_an_empty_pool(self):
        pool = pool_service.get_pool()
        self.assertEqual(pool.usage_percent, 0)
        self.assertEqual(pool.outstanding, 0)
        self.assertTrue(pool.is_empty)
