"""Entry point for cPanel's "Setup Python App" (Phusion Passenger).

Passenger imports this file from the application root and serves the
``application`` object it finds here. Everything else — settings, the .env
file next to manage.py — is the same as for ``manage.py runserver``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
# Shared hosting limits how many threads one account may start; numpy's
# maths library would otherwise try to start one per CPU and fail.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

from config.wsgi import application  # noqa: E402,F401

# Note: cPanel's "Create Application" overwrites this file with a template
# that loads 'passenger_wsgi.py' — itself — and recurses until it crashes.
# If the site shows a 500 right after creating or editing the app, put this
# version back.
