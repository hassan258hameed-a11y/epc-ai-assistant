"""Root URL configuration.

The console at ``/admin/`` owns the whole administrative job — students, tokens,
models, conversations, subscriptions, payments and settings all live inside the
product itself.

Django's stock admin is **not mounted by default**. It exists only as a break-
glass tool: set ``DJANGO_ADMIN_ENABLED=True`` to reach it at ``/django-admin/``
if the console is ever unusable.
"""
from django.conf import settings
from django.conf.urls.static import static
from django.urls import include, path
from django.views.decorators.csrf import csrf_exempt

from AI.urls import (
    auth_patterns,
    console_patterns,
    payment_patterns,
    public_patterns,
    referral_patterns,
    student_patterns,
)
from AI.views import webhooks
from AI.urls import push_patterns  # browser notifications (Web Push)

urlpatterns = [
    # Machine-to-machine callbacks (each authenticates its own caller).
    path("webhooks/payments/<str:method>/", csrf_exempt(webhooks.payment_webhook), name="payment_webhook"),
    path("webhooks/telegram/", csrf_exempt(webhooks.telegram_webhook), name="telegram_webhook"),

    # The admin console.
    path("admin/", include(console_patterns)),

    # Short referral links, e.g. /r/AB12CD34/ — printable, and countable
    # before the visitor decides whether to sign up.
    path("", include(referral_patterns)),

    # Browser notifications: /sw.js, /manifest.webmanifest and /api/push/*.
    path("", include(push_patterns)),

    # Product surfaces.
    path("", include(auth_patterns)),
    path("", include(student_patterns)),
    path("", include(payment_patterns)),
    path("", include(public_patterns)),
]

handler404 = "AI.views.public.error_404"
handler500 = "AI.views.public.error_500"
handler403 = "AI.views.public.error_403"

if settings.DJANGO_ADMIN_ENABLED:  # break-glass only; off by default
    from django.contrib import admin

    urlpatterns.insert(2, path("django-admin/", admin.site.urls))

if settings.PAYMENT_EMULATOR:  # DEBUG only: fake gateways for end-to-end testing
    urlpatterns.insert(0, path("dev/payment-emulator/", include("AI.dev.payment_emulator")))

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.BASE_DIR / "static")
