"""Authentication backend allowing sign-in with either username or e-mail."""
from __future__ import annotations

from django.contrib.auth.backends import ModelBackend
from django.db.models import Q

from AI.models import User


class EmailOrUsernameBackend(ModelBackend):
    def authenticate(self, request, username=None, password=None, **kwargs):
        identifier = (username or kwargs.get("email") or "").strip()
        if not identifier or not password:
            return None

        candidates = list(
            User.objects.filter(Q(username__iexact=identifier) | Q(email__iexact=identifier))[:2]
        )
        if not candidates:
            # Equalise timing so a missing account is not distinguishable.
            User().set_password(password)
            return None

        # Prefer an exact username match when an address is shared.
        user = next((u for u in candidates if u.username.lower() == identifier.lower()), candidates[0])

        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user):
        """Disabled accounts are rejected at the backend, not just in the UI.

        A *pending* account may authenticate so it can reach the verification
        step; the decorators and middleware still keep it out of the product
        until it becomes active.
        """
        from AI.models import AccountStatus

        if not user or not user.is_active:
            return False
        return user.status in {AccountStatus.ACTIVE, AccountStatus.PENDING}
