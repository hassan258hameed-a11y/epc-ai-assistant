"""Signal handlers that keep derived records in step with the user table."""
from django.db.models.signals import post_save
from django.dispatch import receiver

from AI.models import NotificationPreference, TokenAccount, User


@receiver(post_save, sender=User)
def ensure_user_side_tables(sender, instance, created, **kwargs):
    """Every account owns a token ledger account and notification preferences."""
    if created:
        TokenAccount.objects.get_or_create(user=instance)
        NotificationPreference.objects.get_or_create(user=instance)
