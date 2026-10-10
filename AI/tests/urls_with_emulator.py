"""The site's URLs plus the payment emulator, for end-to-end gateway tests."""
from django.urls import include, path

from config.urls import urlpatterns as site_patterns

urlpatterns = [path("dev/payment-emulator/", include("AI.dev.payment_emulator"))] + list(site_patterns)
