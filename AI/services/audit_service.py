"""Audit trail recording for every consequential action."""
from __future__ import annotations

import logging

from AI.models import AuditLog
from AI.utils.request import client_ip, user_agent

logger = logging.getLogger("AI.audit")


def record(
    action: str,
    *,
    actor=None,
    request=None,
    target=None,
    target_label: str = "",
    description: str = "",
    old_value: str = "",
    new_value: str = "",
) -> AuditLog | None:
    """Write one audit entry.

    Auditing must never break the operation it is recording, so failures are
    logged and swallowed.
    """
    try:
        target_type, target_id = "", ""
        if target is not None:
            target_type = target.__class__.__name__
            target_id = str(getattr(target, "pk", "") or "")
            if not target_label:
                target_label = str(target)[:140]

        return AuditLog.objects.create(
            actor=actor if (actor is not None and getattr(actor, "pk", None)) else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=target_label[:140],
            description=description[:255],
            old_value=str(old_value)[:2000],
            new_value=str(new_value)[:2000],
            ip_address=client_ip(request) if request else None,
            user_agent=user_agent(request) if request else "",
        )
    except Exception:  # pragma: no cover
        logger.exception("Failed to write audit log for action=%s", action)
        return None
