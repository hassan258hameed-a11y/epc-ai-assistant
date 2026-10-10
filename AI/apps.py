from django.apps import AppConfig


class AIConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "AI"
    verbose_name = "EPC AI Assistant"

    def ready(self):
        from AI import signals  # noqa: F401  (registers signal handlers)
