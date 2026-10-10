"""The model selector: does it filter, persist, and add up?

The property that matters is arithmetic. If an administrator selects each model
in turn, notes the numbers and adds them, the result has to be the number the
console shows under "All models" — otherwise one of the five views is lying and
there is no way to tell which.

That holds only where a record belongs to exactly one model, which is most of
them: a payment has one plan, a plan has one model, a wallet has one model, a
package has one model. It deliberately does not hold for students or
conversations, where one record legitimately touches several models, and those
cases are pinned here too so that nobody later "fixes" them into agreement.
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.utils import timezone

from AI.models import (
    AIModel,
    AIProvider,
    AIUsageLog,
    Conversation,
    Message,
    MessageRole,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Plan,
    Role,
    Subscription,
    UserAIWallet,
)
from AI.services import analytics_service, model_scope

User = get_user_model()


class ModelScopeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.vendor = AIProvider.objects.create(slug="acme", name="Acme")
        cls.alpha = AIModel.objects.create(
            key="scope-alpha", name="Scope Alpha", provider="openai",
            provider_ref=cls.vendor, sort_order=1,
        )
        cls.beta = AIModel.objects.create(
            key="scope-beta", name="Scope Beta", provider="openai",
            provider_ref=cls.vendor, sort_order=2,
        )

        cls.plan_a = Plan.objects.create(
            name="Alpha plan", slug="alpha-plan", tokens=1000,
            price=Decimal("10.00"), ai_model=cls.alpha,
        )
        cls.plan_b = Plan.objects.create(
            name="Beta plan", slug="beta-plan", tokens=2000,
            price=Decimal("25.00"), ai_model=cls.beta,
        )

        cls.ann = User.objects.create_user(
            username="scope_ann", email="ann@scope.test", password="x", role=Role.STUDENT
        )
        cls.bob = User.objects.create_user(
            username="scope_bob", email="bob@scope.test", password="x", role=Role.STUDENT
        )

        # Ann holds both currencies; Bob holds only beta. Ann is the reason the
        # student count is not a partition.
        UserAIWallet.objects.create(
            user=cls.ann, ai_model=cls.alpha, balance=100,
            allocated_tokens=500, used_tokens=400,
            lifetime_allocated=500, lifetime_used=400,
        )
        UserAIWallet.objects.create(
            user=cls.ann, ai_model=cls.beta, balance=200,
            allocated_tokens=900, used_tokens=700,
            lifetime_allocated=900, lifetime_used=700,
        )
        UserAIWallet.objects.create(
            user=cls.bob, ai_model=cls.beta, balance=50,
            allocated_tokens=300, used_tokens=250,
            lifetime_allocated=300, lifetime_used=250,
        )

        now = timezone.now()
        for user, plan, amount in (
            (cls.ann, cls.plan_a, "10.00"),
            (cls.bob, cls.plan_b, "25.00"),
            (cls.ann, cls.plan_b, "25.00"),
        ):
            payment = Payment.objects.create(
                reference=f"SCOPE-{user.pk}-{plan.pk}", user=user, plan=plan,
                amount=Decimal(amount), method=PaymentMethod.ABA_PAYWAY,
                status=PaymentStatus.PAID, paid_at=now,
            )
            Subscription.objects.create(
                user=user, plan=plan, payment=payment,
                ends_at=now + timezone.timedelta(days=30),
            )

    def scope_for(self, model):
        return model_scope.ModelScope(model, tuple(model_scope.catalogue()))

    # -- the arithmetic ----------------------------------------------------
    def test_payment_revenue_parts_sum_to_the_whole(self):
        """Money is a partition: one payment, one plan, one model."""
        whole = analytics_service.overview()["revenue_total"]
        parts = sum(
            analytics_service.overview(self.scope_for(m))["revenue_total"]
            for m in AIModel.objects.all()
        )
        self.assertEqual(
            parts, whole,
            "per-model revenue must add up to the all-models figure; a payment "
            "belongs to exactly one plan and a plan to exactly one model",
        )

    def test_wallet_balances_parts_sum_to_the_whole(self):
        whole = analytics_service.overview()["tokens_remaining"]
        parts = sum(
            analytics_service.overview(self.scope_for(m))["tokens_remaining"]
            for m in AIModel.objects.all()
        )
        self.assertEqual(parts, whole)

    def test_consumption_parts_sum_to_the_whole(self):
        whole = analytics_service.overview()["tokens_consumed"]
        parts = sum(
            analytics_service.overview(self.scope_for(m))["tokens_consumed"]
            for m in AIModel.objects.all()
        )
        self.assertEqual(parts, whole)

    def test_subscription_counts_partition(self):
        whole = Subscription.objects.count()
        parts = sum(
            self.scope_for(m).subscriptions(Subscription.objects.all()).count()
            for m in AIModel.objects.all()
        )
        self.assertEqual(parts, whole)

    def test_every_plan_belongs_to_exactly_one_model(self):
        """A plan with no model makes its revenue unattributable.

        Migration 0024 gave the plans that predate per-model pricing the model
        they were already crediting. A new one without a model would silently
        reopen the gap between the all-models revenue and the sum of the parts.
        """
        self.assertFalse(
            Plan.objects.filter(ai_model__isnull=True).exists(),
            "a plan with no ai_model credits the default model's wallet but "
            "reports no model's revenue — set one, as migration 0024 did",
        )

    # -- the deliberate non-partitions -------------------------------------
    def test_students_may_appear_under_several_models(self):
        """A student is not divided between models, and must not be.

        Ann holds two currencies, so she is a student of both. The per-model
        counts therefore over-count against the total on purpose — the fix for
        which is never to make them agree, but to know that they do not.
        """
        alpha = self.scope_for(self.alpha).students(User.objects.filter(role=Role.STUDENT)).count()
        beta = self.scope_for(self.beta).students(User.objects.filter(role=Role.STUDENT)).count()
        self.assertEqual(alpha, 1)
        self.assertEqual(beta, 2)
        self.assertGreater(alpha + beta, User.objects.filter(role=Role.STUDENT).count())

    def test_a_conversation_spanning_models_is_counted_once_per_model(self):
        conversation = Conversation.objects.create(user=self.ann, title="Mixed")
        for key in (self.alpha.key, self.alpha.key, self.beta.key):
            Message.objects.create(
                conversation=conversation, role=MessageRole.ASSISTANT,
                content="hi", model_key=key,
            )
        alpha = self.scope_for(self.alpha).conversations(Conversation.objects.all())
        # Two alpha messages, but one conversation — `distinct` is doing its job.
        self.assertEqual(alpha.count(), 1)
        self.assertEqual(
            self.scope_for(self.beta).conversations(Conversation.objects.all()).count(), 1
        )

    def test_usage_that_matches_no_model_is_reported_not_hidden(self):
        """A retired model's history cannot be re-attributed, so it is named."""
        Conversation.objects.create(user=self.ann, title="Old")
        AIUsageLog.objects.create(user=self.ann, model_key="retired-model", total_tokens=42)
        AIUsageLog.objects.create(user=self.ann, model_key=self.alpha.key, total_tokens=8)

        gap = analytics_service.unattributed_usage()
        self.assertEqual(gap["requests"], 1)
        self.assertEqual(gap["tokens"], 42)
        self.assertIn("retired-model", gap["keys"])

    def test_unattributed_keys_are_deduplicated(self):
        """`values().distinct()` is defeated by a model's default ordering."""
        for _ in range(3):
            AIUsageLog.objects.create(user=self.ann, model_key="retired-model", total_tokens=1)
        self.assertEqual(analytics_service.unattributed_usage()["keys"].count("retired-model"), 1)

    # -- resolution --------------------------------------------------------
    def test_an_unknown_model_key_falls_back_to_all_models(self):
        """A stale bookmark shows a full page, never an error."""
        request = RequestFactory().get("/admin/", {"model": "no-such-model"})
        request.session = {}
        scope = model_scope.resolve(request)
        self.assertTrue(scope.is_all)
        # And the bad key is not remembered, or it would be re-applied forever.
        self.assertEqual(request.session.get(model_scope.SESSION_KEY), model_scope.ALL)

    def test_the_choice_is_remembered_and_can_be_cleared(self):
        request = RequestFactory().get("/admin/", {"model": self.beta.key})
        request.session = {}
        self.assertEqual(model_scope.resolve(request).key, self.beta.key)
        self.assertEqual(request.session[model_scope.SESSION_KEY], self.beta.key)

        # A later request with no parameter keeps it …
        follow_up = RequestFactory().get("/admin/payments/")
        follow_up.session = request.session
        self.assertEqual(model_scope.resolve(follow_up).key, self.beta.key)

        # … and an explicit "all" releases it.
        clearing = RequestFactory().get("/admin/", {"model": model_scope.ALL})
        clearing.session = request.session
        self.assertTrue(model_scope.resolve(clearing).is_all)
        follow_up2 = RequestFactory().get("/admin/payments/")
        follow_up2.session = request.session
        self.assertTrue(model_scope.resolve(follow_up2).is_all)

    def test_all_models_narrowing_is_the_query_without_the_filter(self):
        """The aggregate must not be a separately written query."""
        every = model_scope.ModelScope(None, ())
        base = Payment.objects.all()
        self.assertEqual(list(every.payments(base)), list(base))
        self.assertEqual(list(every.wallets(UserAIWallet.objects.all())),
                         list(UserAIWallet.objects.all()))

    def test_inactive_models_stay_in_the_selector(self):
        """Switching a model off must not hide the tokens people still hold."""
        self.beta.is_active = False
        self.beta.save(update_fields=["is_active"])
        keys = {m.key for m in model_scope.catalogue()}
        self.assertIn(self.beta.key, keys)

    def test_querystring_keeps_the_scope_and_drops_all(self):
        one = self.scope_for(self.alpha)
        self.assertEqual(one.querystring(), f"?model={self.alpha.key}")
        self.assertEqual(one.querystring(page=2), f"?model={self.alpha.key}&page=2")
        self.assertEqual(model_scope.ModelScope(None, ()).querystring(), "")
